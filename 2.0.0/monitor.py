# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
远程监控与对讲：
- 摄像头 MJPEG 流（网页查看时才占用摄像头，停止/断开即释放）
- 麦克风 PCM 流（可选所有输入设备，实时回传到网页）
- 浏览器对讲：手机麦克风 PCM 上传，电脑端实时播放，音量随叫号音量设置
仅使用开源库：opencv-python-headless、sounddevice、numpy。
"""
import json
import queue
import subprocess
import threading
import time
import uuid

from flask import Blueprint, Response, jsonify, request

from auth import check_access

monitor_bp = Blueprint("monitor", __name__)

try:
    import cv2
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

try:
    import numpy as np
    import sounddevice as sd
    HAS_SD = True
except Exception:
    np = None
    sd = None
    HAS_SD = False

_config_getter = None


def set_config_getter(fn):
    global _config_getter
    _config_getter = fn


def _cfg():
    try:
        return (_config_getter or (lambda: {}))()
    except Exception:
        return {}


SAMPLE_RATE = 48000
CHANNELS = 1
BLOCK_SIZE = 2400  # 50ms @48k，块大一点更稳，仍保持低延迟

# ---------------------------------------------------------------------------
# 摄像头
# ---------------------------------------------------------------------------
_CAM_LOCK = threading.Lock()
_CAMS_IN_USE = set()
_CAM_NAMES_CACHE = {"names": [], "time": 0.0}
_MIC_CACHE = {"list": [], "time": 0.0}


def _dshow_camera_names():
    """用 DirectShow 设备枚举器取摄像头名称。

    关键：返回顺序与 OpenCV CAP_DSHOW 的 index 完全一致，
    这样下拉框里的名字和实际打开的设备才对应得上。
    """
    import ctypes
    from ctypes import POINTER, byref, c_ulong, c_wchar_p

    import comtypes
    from comtypes import (CLSCTX_INPROC_SERVER, COMMETHOD, GUID, HRESULT,
                          IUnknown, CoCreateInstance)
    from comtypes.automation import VARIANT

    clsid_sys_dev_enum = GUID("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
    clsid_video_input = GUID("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")
    iid_property_bag = GUID("{55272A00-42CB-11CE-8135-00AA004BB851}")

    class IPropertyBag(IUnknown):
        _iid_ = iid_property_bag
        _methods_ = [
            COMMETHOD([], HRESULT, "Read",
                      (["in"], c_wchar_p, "pszPropName"),
                      (["in"], POINTER(VARIANT), "pVar"),
                      (["in"], POINTER(IUnknown), "pErrorLog")),
            COMMETHOD([], HRESULT, "Write",
                      (["in"], c_wchar_p, "pszPropName"),
                      (["in"], POINTER(VARIANT), "pVar")),
        ]

    class IMoniker(IUnknown):
        _iid_ = GUID("{0000000F-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetClassID"),
            COMMETHOD([], HRESULT, "IsDirty"),
            COMMETHOD([], HRESULT, "Load"),
            COMMETHOD([], HRESULT, "Save"),
            COMMETHOD([], HRESULT, "GetSizeMax"),
            COMMETHOD([], HRESULT, "BindToObject"),
            COMMETHOD([], HRESULT, "BindToStorage",
                      (["in"], ctypes.c_void_p, "pbc"),
                      (["in"], ctypes.c_void_p, "pmkToLeft"),
                      (["in"], POINTER(GUID), "riid"),
                      (["in"], POINTER(ctypes.c_void_p), "ppvObj")),
            COMMETHOD([], HRESULT, "Reduce"),
            COMMETHOD([], HRESULT, "ComposeWith"),
            COMMETHOD([], HRESULT, "Enum"),
            COMMETHOD([], HRESULT, "IsEqual"),
            COMMETHOD([], HRESULT, "Hash"),
            COMMETHOD([], HRESULT, "IsRunning"),
            COMMETHOD([], HRESULT, "GetTimeOfLastChange"),
            COMMETHOD([], HRESULT, "Inverse"),
            COMMETHOD([], HRESULT, "RelativePathTo"),
            COMMETHOD([], HRESULT, "GetDisplayName"),
        ]

    class IEnumMoniker(IUnknown):
        _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Next",
                      (["in"], c_ulong, "celt"),
                      (["in"], POINTER(POINTER(IMoniker)), "rgelt"),
                      (["in"], POINTER(c_ulong), "pceltFetched")),
            COMMETHOD([], HRESULT, "Skip", (["in"], c_ulong, "celt")),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "Clone"),
        ]

    class ICreateDevEnum(IUnknown):
        _iid_ = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
        _methods_ = [
            COMMETHOD([], HRESULT, "CreateClassEnumerator",
                      (["in"], POINTER(GUID), "pType"),
                      (["in"], POINTER(POINTER(IEnumMoniker)), "ppEnumMoniker"),
                      (["in"], c_ulong, "dwFlags")),
        ]

    def friendly_name(moniker):
        raw = ctypes.c_void_p()
        moniker.BindToStorage(None, None, byref(iid_property_bag), byref(raw))
        if not raw.value:
            return ""
        bag = ctypes.cast(raw, POINTER(IPropertyBag))
        for key in ("FriendlyName", "Description"):
            var = VARIANT()
            try:
                bag.Read(key, byref(var), None)
                if var.value:
                    return str(var.value)
            except Exception:
                continue
        return ""

    comtypes.CoInitialize()
    dev_enum = CoCreateInstance(clsid_sys_dev_enum, interface=ICreateDevEnum,
                                clsctx=CLSCTX_INPROC_SERVER)
    enum_moniker = POINTER(IEnumMoniker)()
    dev_enum.CreateClassEnumerator(byref(clsid_video_input), byref(enum_moniker), 0)
    names = []
    if not enum_moniker:
        return names
    while True:
        try:
            moniker = POINTER(IMoniker)()
            fetched = c_ulong(0)
            enum_moniker.Next(1, byref(moniker), byref(fetched))
        except Exception:
            break
        if not moniker or not fetched.value:
            break
        try:
            names.append(friendly_name(moniker) or ("摄像头 %d" % (len(names) + 1)))
        except Exception:
            names.append("摄像头 %d" % (len(names) + 1))
    return names


def _pnp_camera_names():
    """摄像头名称（DirectShow 顺序，缓存 60 秒；失败时退回 PnP 名称）。"""
    now = time.time()
    if _CAM_NAMES_CACHE["names"] and now - _CAM_NAMES_CACHE["time"] < 60:
        return list(_CAM_NAMES_CACHE["names"])
    names = []
    try:
        names = _dshow_camera_names()
    except Exception as exc:
        print("[监控] DirectShow 枚举失败:", exc)
    if not names:
        try:
            ps = ("Get-PnpDevice -Class Camera -Status OK -ErrorAction SilentlyContinue | "
                  "Select-Object -ExpandProperty FriendlyName")
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", ps],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000,
                check=False)
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line:
                    names.append(line)
        except Exception as exc:
            print("[监控] 获取摄像头名称失败:", exc)
    if names:
        _CAM_NAMES_CACHE["names"] = names
        _CAM_NAMES_CACHE["time"] = now
    return list(names)


def warm_devices():
    """启动后在后台预热设备列表，避免第一次打开监控页等很久。"""
    def work():
        try:
            _pnp_camera_names()
        except Exception:
            pass
        try:
            _list_mics()
        except Exception:
            pass
    threading.Thread(target=work, daemon=True, name="monitor-warmup").start()


@monitor_bp.route("/api/monitor/cameras")
def api_monitor_cameras():
    err = check_access()
    if err:
        return err
    names = _pnp_camera_names()
    cams = [{"index": i, "name": n} for i, n in enumerate(names)]
    if not cams and HAS_CV2:
        # 枚举失败时的兜底：探测前 4 个索引
        for i in range(4):
            cap = None
            try:
                cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                if cap.isOpened():
                    cams.append({"index": i, "name": "摄像头 %d" % (i + 1)})
            except Exception:
                pass
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
    return jsonify({"ok": True, "cameras": cams})


def _gen_camera(index):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError("无法打开摄像头 %d" % (index + 1))
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 15)
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            ok, buf = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, 62])
            if not ok:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                   str(len(buf)).encode() + b"\r\n\r\n" + buf.tobytes() +
                   b"\r\n")
            time.sleep(0.06)
    finally:
        try:
            cap.release()
        except Exception:
            pass


@monitor_bp.route("/api/monitor/video")
def api_monitor_video():
    err = check_access()
    if err:
        return err
    if not HAS_CV2:
        return jsonify({"ok": False, "error": "未安装摄像头组件"}), 500
    try:
        index = int(request.args.get("cam", "0"))
    except Exception:
        index = 0
    with _CAM_LOCK:
        if index in _CAMS_IN_USE:
            return jsonify({"ok": False, "error": "该摄像头正在被占用"}), 409
        _CAMS_IN_USE.add(index)

    def stream():
        try:
            yield from _gen_camera(index)
        finally:
            with _CAM_LOCK:
                _CAMS_IN_USE.discard(index)

    resp = Response(stream(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# 麦克风（电脑 -> 网页）
# ---------------------------------------------------------------------------
_AUDIO_LOCK = threading.Lock()
_AUDIO_CLIENTS = {}
_MIC_STREAM = None
_MIC_DEVICE = None


def _audio_callback(indata, frames, t, status):
    data = indata.tobytes()
    with _AUDIO_LOCK:
        clients = list(_AUDIO_CLIENTS.items())
    for _cid, q in clients:
        try:
            q.put_nowait(data)
        except queue.Full:
            # 队列满：丢弃最旧的一帧，保留最新，保证低延迟
            try:
                q.get_nowait()
                q.put_nowait(data)
            except Exception:
                pass


def _stop_mic_stream():
    global _MIC_STREAM, _MIC_DEVICE
    try:
        if _MIC_STREAM is not None:
            _MIC_STREAM.stop()
            _MIC_STREAM.close()
    except Exception:
        pass
    _MIC_STREAM = None
    _MIC_DEVICE = None


def _ensure_mic_stream(device):
    global _MIC_STREAM, _MIC_DEVICE
    with _AUDIO_LOCK:
        if _MIC_STREAM is not None and _MIC_DEVICE == device:
            return True
        _stop_mic_stream()
        try:
            kwargs = dict(samplerate=SAMPLE_RATE, channels=CHANNELS,
                          dtype="int16", blocksize=BLOCK_SIZE,
                          latency="low", callback=_audio_callback)
            if device >= 0:
                kwargs["device"] = device
            _MIC_STREAM = sd.InputStream(**kwargs)
            _MIC_STREAM.start()
            _MIC_DEVICE = device
            return True
        except Exception as exc:
            print("[监控] 打开麦克风失败:", exc)
            _stop_mic_stream()
            return False


def _list_mics():
    """麦克风列表（缓存 30 秒）。

    PortAudio 会把同一支麦克风按不同主机接口（WASAPI / DirectSound / MME / WDM-KS）
    重复列出来，还有“主声音捕获驱动程序”“Microsoft Sound Mapper”等虚拟设备。
    这里只保留真实设备（优先 WASAPI），并按名称去重，与系统显示保持一致。
    """
    now = time.time()
    if _MIC_CACHE["list"] and now - _MIC_CACHE["time"] < 30:
        return list(_MIC_CACHE["list"])
    mics = []
    if HAS_SD:
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()

            def api_name(dev):
                try:
                    return str(hostapis[int(dev.get("hostapi", 0))].get("name", ""))
                except Exception:
                    return ""

            # 优先 WASAPI（与系统“声音设置”一致的那一套）
            preferred = None
            for i, ha in enumerate(hostapis):
                if "wasapi" in str(ha.get("name", "")).lower():
                    preferred = i
                    break

            pseudo = ("sound mapper", "主声音", "primary sound",
                      "声音映射器", "microsoft sound mapper")

            def usable(dev):
                if int(dev.get("max_input_channels", 0)) <= 0:
                    return False
                name = str(dev.get("name", "")).strip()
                if not name:
                    return False
                low = name.lower()
                return not any(p in low for p in pseudo)

            pool = []
            if preferred is not None:
                pool = [(i, d) for i, d in enumerate(devices)
                        if d.get("hostapi") == preferred and usable(d)]
            if not pool:  # 没有 WASAPI 时退回全部接口，再去重
                pool = [(i, d) for i, d in enumerate(devices) if usable(d)]

            default_in = None
            try:
                default_in = sd.default.device[0]
            except Exception:
                pass

            seen = set()
            for i, d in pool:
                name = str(d.get("name", "")).strip()
                if name in seen:
                    continue
                seen.add(name)
                mics.append({"index": i, "name": name})

            marked = False
            for m in mics:
                if default_in is not None and m["index"] == default_in:
                    m["name"] = m["name"] + "（默认）"
                    marked = True
                    break
            if not marked and mics:
                mics[0]["name"] = mics[0]["name"] + "（默认）"
        except Exception as exc:
            print("[监控] 枚举麦克风失败:", exc)
    if not mics:
        mics.append({"index": -1, "name": "系统默认麦克风"})
    _MIC_CACHE["list"] = mics
    _MIC_CACHE["time"] = now
    return list(mics)


@monitor_bp.route("/api/monitor/mics")
def api_monitor_mics():
    err = check_access()
    if err:
        return err
    return jsonify({"ok": True, "mics": _list_mics()})


@monitor_bp.route("/api/monitor/audio")
def api_monitor_audio():
    err = check_access()
    if err:
        return err
    if not HAS_SD:
        return jsonify({"ok": False, "error": "未安装音频组件"}), 500
    try:
        device = int(request.args.get("mic", "-1"))
    except Exception:
        device = -1
    cid = uuid.uuid4().hex
    q = queue.Queue(maxsize=6)  # 6 x 50ms ≈ 300ms，平滑抖动
    with _AUDIO_LOCK:
        _AUDIO_CLIENTS[cid] = q
    if not _ensure_mic_stream(device):
        with _AUDIO_LOCK:
            _AUDIO_CLIENTS.pop(cid, None)
        return jsonify({"ok": False, "error": "无法打开麦克风"}), 500

    def gen():
        try:
            while True:
                try:
                    yield q.get(timeout=1.0)
                except queue.Empty:
                    yield b""
        finally:
            with _AUDIO_LOCK:
                _AUDIO_CLIENTS.pop(cid, None)
                if not _AUDIO_CLIENTS:
                    _stop_mic_stream()

    resp = Response(gen(), mimetype="application/octet-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ---------------------------------------------------------------------------
# 对讲（网页 -> 电脑）
# ---------------------------------------------------------------------------
_OUT_LOCK = threading.Lock()
_OUT_STREAM = None
_OUT_LAST = 0.0
_OUT_QUEUE = queue.Queue(maxsize=12)  # 抖动缓冲：约 12 x 100ms，满了丢最旧


def _close_out_stream():
    global _OUT_STREAM
    try:
        if _OUT_STREAM is not None:
            _OUT_STREAM.stop()
            _OUT_STREAM.close()
    except Exception:
        pass
    _OUT_STREAM = None


def _ensure_out_stream():
    """打开/复用播放流（空闲 20 秒自动关闭）。"""
    global _OUT_STREAM, _OUT_LAST
    with _OUT_LOCK:
        if _OUT_STREAM is not None and time.time() - _OUT_LAST > 20:
            _close_out_stream()
        if _OUT_STREAM is None:
            _OUT_STREAM = sd.OutputStream(samplerate=SAMPLE_RATE,
                                          channels=CHANNELS, dtype="int16",
                                          latency="low")
            _OUT_STREAM.start()
        _OUT_LAST = time.time()
        return _OUT_STREAM


def _enqueue_out(samples):
    """把音频块交给播放线程；缓冲满时丢最旧，保证低延迟。"""
    try:
        _OUT_QUEUE.put_nowait(samples)
    except queue.Full:
        try:
            _OUT_QUEUE.get_nowait()
            _OUT_QUEUE.put_nowait(samples)
        except Exception:
            pass


def _playout_worker():
    """对讲播放线程：串行写入，网络抖动时输出短静音填充，避免断续。"""
    global _OUT_STREAM
    silence = np.zeros(BLOCK_SIZE, dtype=np.int16)
    idle_since = None
    while True:
        try:
            samples = _OUT_QUEUE.get(timeout=0.05)
            idle_since = None
        except queue.Empty:
            samples = None
        if samples is None:
            with _OUT_LOCK:
                if _OUT_STREAM is None:
                    continue
                if idle_since is None:
                    idle_since = time.time()
                if time.time() - idle_since > 20:
                    _close_out_stream()
                    idle_since = None
                    continue
                stream = _OUT_STREAM
            try:
                stream.write(silence)
            except Exception:
                with _OUT_LOCK:
                    _close_out_stream()
            continue
        try:
            stream = _ensure_out_stream()
            stream.write(samples)
        except Exception:
            with _OUT_LOCK:
                _close_out_stream()


if HAS_SD:
    threading.Thread(target=_playout_worker, daemon=True, name="intercom-playout").start()


@monitor_bp.route("/api/monitor/intercom", methods=["POST"])
def api_monitor_intercom():
    err = check_access()
    if err:
        return err
    if not HAS_SD:
        return jsonify({"ok": False, "error": "未安装音频组件"}), 500
    raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"ok": True})
    cfg = _cfg()
    factor = 1.0
    if cfg.get("volume_enabled", True):
        try:
            factor = max(0.0, min(1.0, int(cfg.get("volume", 100)) / 100.0))
        except Exception:
            factor = 1.0
    try:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if factor != 1.0:
            samples = samples * factor
            np.clip(samples, -32768, 32767, out=samples)
        _enqueue_out(samples.astype(np.int16))  # sounddevice 需要 numpy 数组
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False, "error": "播放失败"}), 500


def monitor_shutdown():
    _stop_mic_stream()
    with _OUT_LOCK:
        _close_out_stream()


# ---------------------------------------------------------------------------
# WebSocket 实时通道
# ---------------------------------------------------------------------------
def _ws_fail(ws, msg):
    try:
        ws.send(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    except Exception:
        pass
    try:
        ws.close()
    except Exception:
        pass


def ws_monitor_camera(ws):
    err = check_access()
    if err:
        _ws_fail(ws, "需要密码激活后才能使用该功能")
        return
    if not HAS_CV2:
        _ws_fail(ws, "未安装摄像头组件")
        return
    try:
        index = int(request.args.get("cam", "0"))
    except Exception:
        index = 0
    with _CAM_LOCK:
        if index in _CAMS_IN_USE:
            _ws_fail(ws, "该摄像头正在被占用")
            return
        _CAMS_IN_USE.add(index)

    def open_cam():
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return None
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 20)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 只保留最新一帧，避免越读越旧
        except Exception:
            pass
        return cap

    cap = open_cam()
    if cap is None:
        with _CAM_LOCK:
            _CAMS_IN_USE.discard(index)
        _ws_fail(ws, "无法打开摄像头 %d" % (index + 1))
        return

    quality = 55
    slow = 0
    fails = 0
    try:
        while True:
            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                fails += 1
                if fails == 20:
                    # 长时间读不到画面：重开一次（USB 相机偶发掉帧）
                    try:
                        cap.release()
                    except Exception:
                        pass
                    time.sleep(0.3)
                    cap = open_cam()
                    if cap is None:
                        break
                time.sleep(0.05)
                continue
            fails = 0
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                try:
                    ws.send(buf.tobytes())
                except Exception:
                    break
            # 等客户端“已渲染”回执：客户端慢就自然降帧，防止积压导致越来越卡
            try:
                ws.receive(timeout=0.4)
            except Exception:
                pass
            cost = (time.time() - t0) * 1000
            if cost > 180:
                slow += 1
                if slow >= 12 and quality > 35:
                    quality -= 10
                    slow = 0
            elif cost < 80:
                slow = 0
                if quality < 55:
                    quality += 5
            time.sleep(0.01)
    finally:
        try:
            cap.release()
        except Exception:
            pass
        with _CAM_LOCK:
            _CAMS_IN_USE.discard(index)


def ws_monitor_audio(ws):
    err = check_access()
    if err:
        _ws_fail(ws, "需要密码激活后才能使用该功能")
        return
    if not HAS_SD:
        _ws_fail(ws, "未安装音频组件")
        return
    try:
        device = int(request.args.get("mic", "-1"))
    except Exception:
        device = -1
    cid = uuid.uuid4().hex
    q = queue.Queue(maxsize=6)  # 6 x 50ms ≈ 300ms，平滑抖动
    with _AUDIO_LOCK:
        _AUDIO_CLIENTS[cid] = q
    if not _ensure_mic_stream(device):
        with _AUDIO_LOCK:
            _AUDIO_CLIENTS.pop(cid, None)
        _ws_fail(ws, "无法打开麦克风")
        return
    try:
        while True:
            try:
                chunk = q.get(timeout=1.0)
            except queue.Empty:
                chunk = b""
            try:
                ws.send(chunk)
            except Exception:
                break
    finally:
        with _AUDIO_LOCK:
            _AUDIO_CLIENTS.pop(cid, None)
            if not _AUDIO_CLIENTS:
                _stop_mic_stream()


def ws_monitor_intercom(ws):
    global _OUT_STREAM, _OUT_LAST
    err = check_access()
    if err:
        _ws_fail(ws, "需要密码激活后才能使用该功能")
        return
    if not HAS_SD:
        _ws_fail(ws, "未安装音频组件")
        return
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            if isinstance(data, str):
                continue
            raw = bytes(data)
            if not raw:
                continue
            cfg = _cfg()
            factor = 1.0
            if cfg.get("volume_enabled", True):
                try:
                    factor = max(0.0, min(1.0, int(cfg.get("volume", 100)) / 100.0))
                except Exception:
                    factor = 1.0
            try:
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                if factor != 1.0:
                    samples = samples * factor
                    np.clip(samples, -32768, 32767, out=samples)
                _enqueue_out(samples.astype(np.int16))
            except Exception:
                with _OUT_LOCK:
                    _close_out_stream()
    finally:
        pass
