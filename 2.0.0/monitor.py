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

import abr

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

# 监控画质预设：设置页里选「流畅 / 标准 / 高清」
QUALITY_PRESETS = {
    "smooth":   {"width": 640,  "height": 480,  "fps": 15, "quality": 65},
    "standard": {"width": 1280, "height": 720,  "fps": 20, "quality": 75},
    "hd":       {"width": 1920, "height": 1080, "fps": 20, "quality": 82},
}
DEFAULT_QUALITY_PRESET = "standard"


def _quality_params():
    """按配置返回当前画质参数（分辨率 / 帧率 / JPEG 质量）。"""
    cfg = _cfg() or {}
    key = str(cfg.get("monitor_quality", "") or DEFAULT_QUALITY_PRESET).strip().lower()
    params = dict(QUALITY_PRESETS.get(key) or QUALITY_PRESETS[DEFAULT_QUALITY_PRESET])
    params["preset"] = key if key in QUALITY_PRESETS else DEFAULT_QUALITY_PRESET
    try:
        q = int(cfg.get("monitor_jpeg_quality") or 0)
        if 30 <= q <= 95:
            params["quality"] = q
    except Exception:
        pass
    return params


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
        qp = _quality_params()
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, qp["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, qp["height"])
        cap.set(cv2.CAP_PROP_FPS, qp["fps"])
        interval = 1.0 / max(1, qp["fps"])
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            ok, buf = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, qp["quality"]])
            if not ok:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                   str(len(buf)).encode() + b"\r\n\r\n" + buf.tobytes() +
                   b"\r\n")
            time.sleep(interval)
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
_MIC_RATE = SAMPLE_RATE     # 麦克风实际采样率（可能降级）


def _audio_callback(indata, frames, t, status):
    try:
        if indata.ndim > 1 and indata.shape[1] > 1:
            # 多声道设备（立体声麦克风阵列）取第一声道，避免浏览器听到交错数据
            data = indata[:, 0].astype("int16").tobytes()
        else:
            data = indata.reshape(-1).astype("int16").tobytes()
    except Exception:
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


def _mic_attempts(device):
    """按设备真实能力枚举 (采样率, 声道) 组合，尽量一次就开起来。"""
    combos = []
    info = None
    try:
        info = sd.query_devices(device if device >= 0 else None, "input")
    except Exception:
        info = None
    maxch = int((info or {}).get("max_input_channels") or 2)
    default_rate = int((info or {}).get("default_samplerate") or 0)
    rates = []
    for r in (default_rate, SAMPLE_RATE, 44100, 16000, 8000, None):
        if r not in rates:
            rates.append(r)
    chans = []
    for c in (1, 2, 3, 4, maxch):
        if c and c not in chans and c <= max(1, maxch):
            chans.append(c)
    for r in rates:
        for c in chans:
            combos.append((r, c))
    return combos, (info or {})


def _ensure_mic_stream(device):
    global _MIC_STREAM, _MIC_DEVICE, _MIC_RATE
    with _AUDIO_LOCK:
        if _MIC_STREAM is not None and _MIC_DEVICE == device:
            return True
        _stop_mic_stream()

        # 先用用户选的设备；全部失败再退回系统默认输入设备
        tries = []
        if device is not None and device >= 0:
            tries.append(device)
        tries.append(-1)

        for dev in tries:
            combos, info = _mic_attempts(dev)
            dev_name = info.get("name") or ("默认输入" if dev < 0 else ("设备%d" % dev))
            for rate, ch in combos:
                try:
                    kwargs = dict(channels=ch, dtype="int16", blocksize=BLOCK_SIZE,
                                  latency="low", callback=_audio_callback)
                    if rate:
                        kwargs["samplerate"] = rate
                    if dev >= 0:
                        kwargs["device"] = dev
                    stream = sd.InputStream(**kwargs)
                    stream.start()
                    real_rate = int(rate or stream.samplerate or SAMPLE_RATE)
                    _MIC_STREAM = stream
                    _MIC_DEVICE = device
                    _MIC_RATE = real_rate
                    print("[监控] 麦克风已打开：%s 采样率=%d 声道=%d（请求设备=%s）"
                          % (dev_name, real_rate, ch, device))
                    return True
                except Exception as exc:
                    print("[监控] 打开失败 设备=%s(%s) rate=%s ch=%d: %s"
                          % (dev, dev_name, rate, ch, exc))
        _stop_mic_stream()
        print("[监控] 所有输入设备都打不开（请求设备=%s）" % device)
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
_OUT_RATE = SAMPLE_RATE        # 播放流实际采样率
_OUT_WANT_RATE = SAMPLE_RATE   # 本次对讲希望使用的采样率
_OUT_LAST = 0.0
_OUT_QUEUE = queue.Queue(maxsize=4)  # 抖动缓冲：约 12 x 100ms，满了丢最旧


def _close_out_stream():
    global _OUT_STREAM
    try:
        if _OUT_STREAM is not None:
            _OUT_STREAM.stop()
            _OUT_STREAM.close()
    except Exception:
        pass
    _OUT_STREAM = None


def _resample_i16(samples, src_rate, dst_rate):
    """int16 样本线性重采样（src_rate -> dst_rate）。"""
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    n_out = max(1, int(round(len(samples) * float(dst_rate) / float(src_rate))))
    src_idx = np.linspace(0, len(samples) - 1, n_out)
    out = np.interp(src_idx, np.arange(len(samples)), samples.astype(np.float32))
    return np.clip(out, -32768, 32767).astype(np.int16)


def _ensure_out_stream(rate=None):
    """打开/复用播放流；采样率变了就重开。"""
    global _OUT_STREAM, _OUT_LAST, _OUT_RATE
    want = int(rate or SAMPLE_RATE)
    with _OUT_LOCK:
        if _OUT_STREAM is not None and _OUT_RATE != want:
            _close_out_stream()
        if _OUT_STREAM is not None and time.time() - _OUT_LAST > 20:
            _close_out_stream()
        if _OUT_STREAM is None:
            _OUT_STREAM = sd.OutputStream(samplerate=want, channels=CHANNELS,
                                          dtype="int16", latency="low")
            _OUT_STREAM.start()
            _OUT_RATE = want
            print("[对讲] 打开播放流 采样率=%d" % want)
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
            stream = _ensure_out_stream(_OUT_WANT_RATE)
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


def _acquire_cam(index, timeout=2.0):
    """占用某个摄像头索引。

    上一个会话刚断开时，DirectShow 驱动往往要再过一会儿才真正放开设备，
    这段时间里新请求会看到"还在占用"。手机用户点「停止→开始」几乎必然撞上，
    所以这里等一下再判断，而不是立刻报"正在被占用"。
    """
    end = time.time() + max(0.0, float(timeout))
    while True:
        with _CAM_LOCK:
            if index not in _CAMS_IN_USE:
                _CAMS_IN_USE.add(index)
                return True
        if time.time() >= end:
            return False
        time.sleep(0.12)


def _release_cam(index):
    with _CAM_LOCK:
        _CAMS_IN_USE.discard(index)


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
    if not _acquire_cam(index, 2.0):
        _ws_fail(ws, "该摄像头正在被占用")
        return

    qp = _quality_params()      # 画质参数在整条推流里都要用，提到函数作用域
    cfg = _cfg() or {}
    # 延迟预算（监控默认 5 秒 = 画质优先）：此时按 720p 采集，宁可排队也不降画质
    budget = abr.budget_of(cfg, "monitor")
    qf = abr.is_quality_first(budget)
    cap_w = max(qp["width"], 1280) if qf else qp["width"]
    cap_h = max(qp["height"], 720) if qf else qp["height"]

    def open_cam(tries=3):
        """打开摄像头；驱动刚释放时常失败，重试几次（每次 0.35s）。"""
        cap = None
        for _i in range(max(1, tries)):
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if cap.isOpened():
                try:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cap_w)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cap_h)
                    cap.set(cv2.CAP_PROP_FPS, qp["fps"])
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 只留最新一帧
                except Exception:
                    pass
                return cap
            try:
                cap.release()
            except Exception:
                pass
            time.sleep(0.35)
        return None

    cap = open_cam()
    if cap is None:
        _release_cam(index)
        _ws_fail(ws, "无法打开摄像头 %d" % (index + 1))
        return

    # 握手：新客户端连上先发 "hi2"，表示支持「序号前缀帧 + 回执 + 自适应码率」。
    # 老客户端（标签页没刷新、缓存了旧 JS）什么都不发 → 继续走 2.0.31 的停等
    # 协议，保证不会因为前端没更新就黑屏。
    seq_mode = False
    try:
        hello = ws.receive(timeout=1.5)
    except Exception:
        hello = None
    if isinstance(hello, str) and hello.strip() == "hi2":
        seq_mode = True
    try:
        abr.tune_socket(ws)      # 关 Nagle：小帧/控制消息不再等凑包
    except Exception:
        pass

    abr_on = abr.enabled(cfg)
    ctrl = None
    acks = None
    if seq_mode:
        _lv = abr.levels_for_mode(qp["width"], qp["quality"], qp["fps"], "cam", budget)
        ctrl = abr.register(abr.Controller(
            "cam%d" % index, _lv,
            window=cfg.get("video_window", abr.DEFAULT_WINDOW),
            start=(len(_lv) - 1) if qf else 1, budget=budget))
        # 回执由独立线程收：时间戳准，发送线程被 send 挡住时也能及时降档
        acks = abr.AckReader(ws, ctrl, auto=abr_on)
        acks.start()

    # 摄像头读取线程：DShow 的 read() 会阻塞 40~110ms（暗光下更久）。
    # 放在独立线程里，发送线程才能按目标帧率走，帧 RTT 也不会被自己的耗时污染。
    # 摄像头的打开/释放都由这个线程负责（read 进行中释放会直接把进程带崩）。
    state = {"frame": None, "ts": 0.0, "stop": False, "cap": cap}

    def cam_reader():
        fails = 0
        try:
            while not state["stop"]:
                c = state["cap"]
                if c is None:
                    time.sleep(0.05)
                    continue
                try:
                    ok, frame = c.read()
                except Exception:
                    ok, frame = False, None
                if not ok:
                    fails += 1
                    if fails >= 20:
                        # 长时间读不到画面：重开一次（USB 相机偶发掉帧）
                        try:
                            c.release()
                        except Exception:
                            pass
                        time.sleep(0.3)
                        state["cap"] = open_cam()
                        fails = 0
                    time.sleep(0.02)
                    continue
                fails = 0
                state["frame"] = frame
                state["ts"] = time.time()
        finally:
            c = state.get("cap")
            if c is not None:
                try:
                    c.release()
                except Exception:
                    pass
            state["cap"] = None

    cam_th = threading.Thread(target=cam_reader, daemon=True, name="cam-reader")
    cam_th.start()

    quality = qp["quality"]                 # 旧通道的初始质量（跟用户选择走）
    qmax = qp["quality"]
    qmin = max(45, qp["quality"] - 30)      # 最低也不至于糊成马赛克
    slow = 0
    paused = False
    seq = 0
    t_next = 0.0
    last_log = 0.0
    last_frame_ts = 0.0
    try:
        while True:
            now = time.time()

            # ---------------- 新通道：回执由独立读线程收（不阻塞） ----------------
            if seq_mode:
                for _ts, m in acks.drain():
                    if m == "p":
                        paused = True        # 页面切后台：暂停推流
                    elif m == "r":
                        paused = False
                if acks.closed:
                    break                    # 对端已断开
                if ctrl is not None:
                    if abr_on and ctrl.starved(now):
                        ctrl.fuse(now)           # 收不到回执 → 压到最低档探活
                    if abr_on:
                        lvl = ctrl.tick(now)
                    else:
                        lvl = ctrl.level()
                else:
                    lvl = {"width": qp["width"], "quality": quality,
                           "fps": qp["fps"], "window": 2}
                if paused:
                    time.sleep(0.03)
                    continue
                if ctrl is not None and ctrl.pause_left(now) > 0:
                    time.sleep(0.05)             # 排空暂停：别往积压队列里再塞帧
                    continue
                if ctrl is not None and ctrl.window_used() >= int(lvl.get("window") or 1):
                    time.sleep(0.004)            # 窗口已满：等回执，不积压
                    continue
                if now < t_next:
                    time.sleep(0.004)
                    continue
            else:
                lvl = {"width": qp["width"], "quality": quality,
                       "fps": qp["fps"], "window": 1}

            frame = state["frame"]
            fts = state["ts"]
            if frame is None:
                time.sleep(0.02)                 # 摄像头还没出第一帧
                continue
            if fts <= last_frame_ts:
                time.sleep(0.004)                # 没有新画面：别发重复帧占带宽
                continue
            last_frame_ts = fts

            t0 = time.time()
            # 降档时先缩图再编码：分辨率是可调的，不能只调 JPEG 质量
            target_w = int(lvl.get("width") or 0)
            try:
                if target_w and frame.shape[1] > target_w:
                    th = max(1, int(round(frame.shape[0] * target_w /
                                          float(frame.shape[1]))))
                    frame = cv2.resize(frame, (target_w, th),
                                       interpolation=cv2.INTER_AREA)
            except Exception:
                pass
            ok, buf = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, int(lvl["quality"])])
            if ok:
                data = buf.tobytes()
                if seq_mode:
                    seq = (seq + 1) & 0xFFFFFFFF
                    if ctrl is not None:
                        ctrl.note_send(seq, len(data))
                    try:
                        ws.send(seq.to_bytes(4, "big") + data)
                    except Exception:
                        break
                    # 节流：帧率上限 + 速率预算两道闸（隧道/校园网上行很窄，
                    # 推超了会把回执一起堵在后面，延迟直接涨到几百毫秒）
                    if ctrl is not None and abr_on:
                        gap = ctrl.pace_interval(len(data) / 1024.0,
                                                 lvl.get("base_fps") or lvl["fps"])
                    else:
                        gap = 1.0 / max(1, int(lvl.get("base_fps") or lvl["fps"]))
                    t_next = t0 + gap
                else:
                    try:
                        ws.send(data)
                    except Exception:
                        break
                    # 旧客户端：等“已渲染”回执（2.0.31 行为，保持兼容）
                    try:
                        ws.receive(timeout=0.4)
                    except Exception:
                        pass
                    cost = (time.time() - t0) * 1000
                    if cost > 180:
                        slow += 1
                        if slow >= 12 and quality > qmin:
                            quality = max(qmin, quality - 10)
                            slow = 0
                    elif cost < 80:
                        slow = 0
                        if quality < qmax:
                            quality = min(qmax, quality + 5)
                    time.sleep(0.01)

            if seq_mode and ctrl is not None and now - last_log >= 3.0:
                last_log = now
                ctrl.log()                   # 每 3 秒一条，网页 /logs 可查
    finally:
        state["stop"] = True
        if acks is not None:
            acks.stop = True
        if ctrl is not None:
            abr.unregister(ctrl.name)
        try:
            cam_th.join(timeout=3.0)     # 等读线程退出并自己释放摄像头
        except Exception:
            pass
        _release_cam(index)


def _decimate_s16(data, src_rate, dst_rate):
    """int16 PCM 降采样（先 k 点平滑再抽取，语音够用，代价可忽略）。"""
    if not data or np is None:
        return data
    try:
        step = int(round(float(src_rate) / float(dst_rate)))
    except Exception:
        return data
    if step <= 1:
        return data
    try:
        a = np.frombuffer(data, dtype=np.int16)
        if len(a) <= step * 2:
            return b""
        if step > 2:
            k = np.ones(step, dtype=np.float32) / float(step)
            sm = np.convolve(a.astype(np.float32), k, mode="same")
        else:
            sm = a.astype(np.float32)
        return np.clip(sm[::step], -32768, 32767).astype(np.int16).tobytes()
    except Exception:
        return data


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
    cfg = _cfg() or {}
    # 语音用 16k 足够，裸 PCM 从 768kbps 降到 256kbps，把上行让给视频
    # （隧道/校园网的上行是共享的，音频多吃一口视频就少一口）
    try:
        want = int(cfg.get("monitor_audio_rate") or 16000)
    except Exception:
        want = 16000
    if want not in (8000, 12000, 16000, 24000, 32000, 48000):
        want = 16000
    cid = uuid.uuid4().hex
    q = queue.Queue(maxsize=3)   # 3 x 50ms ≈ 150ms 抖动缓冲（原来 6 = 300ms）
    with _AUDIO_LOCK:
        _AUDIO_CLIENTS[cid] = q
    if not _ensure_mic_stream(device):
        with _AUDIO_LOCK:
            _AUDIO_CLIENTS.pop(cid, None)
        _ws_fail(ws, "无法打开麦克风")
        return
    try:
        abr.tune_socket(ws)
    except Exception:
        pass
    try:
        send_rate = int(_MIC_RATE or SAMPLE_RATE)
        if want < send_rate:
            send_rate = want
        # 先告诉浏览器真实采样率，避免它用 48k 去解 16k 的数据
        try:
            ws.send(json.dumps({"ok": True, "type": "hello", "rate": int(send_rate),
                                "channels": CHANNELS, "format": "s16le"},
                               ensure_ascii=False))
        except Exception:
            pass
        silent = 0
        while True:
            try:
                chunk = q.get(timeout=1.0)
            except queue.Empty:
                continue          # 没数据就不发空包
            if not chunk:
                continue
            src = int(_MIC_RATE or SAMPLE_RATE)
            rate = min(want, src)
            if rate != send_rate:
                send_rate = rate
                try:
                    ws.send(json.dumps({"ok": True, "type": "rate",
                                        "rate": int(send_rate)},
                                       ensure_ascii=False))
                except Exception:
                    break
            try:
                payload = _decimate_s16(chunk, src, send_rate)
            except Exception:
                payload = chunk
            if not payload:
                continue
            try:
                ws.send(payload)
            except Exception:
                break
            try:
                rms = float(np.abs(np.frombuffer(payload, dtype=np.int16)).mean())
            except Exception:
                rms = -1.0
            if 0 <= rms < 30:
                silent += 1
                if silent == 40:
                    print("[监控] 麦克风电平很低(rms=%.0f)，可能静音或选错设备" % rms)
            else:
                silent = 0
    finally:
        with _AUDIO_LOCK:
            _AUDIO_CLIENTS.pop(cid, None)
            if not _AUDIO_CLIENTS:
                _stop_mic_stream()


def ws_monitor_intercom(ws):
    global _OUT_STREAM, _OUT_LAST, _OUT_WANT_RATE
    err = check_access()
    if err:
        _ws_fail(ws, "需要密码激活后才能使用该功能")
        return
    if not HAS_SD:
        _ws_fail(ws, "未安装音频组件")
        return

    # 采样率：URL 参数（老客户端）或第一条文本 hello（新客户端）
    try:
        src_rate = int(request.args.get("rate", SAMPLE_RATE) or SAMPLE_RATE)
    except Exception:
        src_rate = SAMPLE_RATE
    if src_rate < 8000 or src_rate > 96000:
        src_rate = SAMPLE_RATE
    play_rate = [None]          # 延迟到真正有音频时再开播放流
    peak = 0.0
    frames = 0

    def ensure_rate(rate):
        """按上报采样率开播放流；失败退回 48k，并返回实际播放采样率。"""
        global _OUT_WANT_RATE
        _OUT_WANT_RATE = rate
        try:
            _ensure_out_stream(rate)
        except Exception as exc:
            print("[对讲] 无法按 %d 打开播放流，退回 48k: %s" % (rate, exc))
            _OUT_WANT_RATE = SAMPLE_RATE
            try:
                _ensure_out_stream(SAMPLE_RATE)
            except Exception as exc2:
                print("[对讲] 播放设备打开失败:", exc2)
                return SAMPLE_RATE
        print("[对讲] 手机上报=%d 播放=%d" % (rate, int(_OUT_RATE or SAMPLE_RATE)))
        return int(_OUT_RATE or SAMPLE_RATE)

    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            if isinstance(data, str):
                # 握手：{"type":"hello","rate":48000}
                try:
                    obj = json.loads(data)
                    r = int(obj.get("rate") or 0)
                    if 8000 <= r <= 96000:
                        src_rate = r
                except Exception:
                    pass
                if play_rate[0] is None:
                    play_rate[0] = ensure_rate(src_rate)
                continue
            raw = bytes(data)
            if not raw:
                continue
            if play_rate[0] is None:
                play_rate[0] = ensure_rate(src_rate)
            cfg = _cfg()
            factor = 1.0
            if cfg.get("volume_enabled", True):
                try:
                    factor = max(0.0, min(1.0, int(cfg.get("volume", 100)) / 100.0))
                except Exception:
                    factor = 1.0
            try:
                samples = np.frombuffer(raw, dtype=np.int16)
                if len(samples) == 0:
                    continue
                frames += 1
                peak = max(peak, float(np.abs(samples).mean()))
                if play_rate[0] and play_rate[0] != src_rate:
                    samples = _resample_i16(samples, src_rate, play_rate[0])
                if factor != 1.0:
                    f = samples.astype(np.float32) * factor
                    samples = np.clip(f, -32768, 32767).astype(np.int16)
                _enqueue_out(samples)
            except Exception as exc:
                print("[对讲] 播放失败:", exc)
                with _OUT_LOCK:
                    _close_out_stream()
                play_rate[0] = None
    finally:
        if frames == 0:
            print("[对讲] 本次没有收到任何音频帧（浏览器可能没采到声音）")
        elif peak < 30:
            print("[对讲] 收到 %d 帧但电平很低(峰值 rms=%.0f)，手机麦克风可能静音" % (frames, peak))
        else:
            print("[对讲] 收到 %d 帧，峰值 rms=%.0f，播放正常" % (frames, peak))
