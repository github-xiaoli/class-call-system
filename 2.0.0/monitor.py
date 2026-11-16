# -*- coding: utf-8 -*-
"""
远程监控与对讲：
- 摄像头 MJPEG 流（网页查看时才占用摄像头，停止/断开即释放）
- 麦克风 PCM 流（可选所有输入设备，实时回传到网页）
- 浏览器对讲：手机麦克风 PCM 上传，电脑端实时播放，音量随叫号音量设置
仅使用开源库：opencv-python-headless、sounddevice、numpy。
"""
import json
import queue
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
BLOCK_SIZE = 1200  # 25ms @48k，降低声音延迟

# ---------------------------------------------------------------------------
# 摄像头
# ---------------------------------------------------------------------------
_CAM_LOCK = threading.Lock()
_CAMS_IN_USE = set()


@monitor_bp.route("/api/monitor/cameras")
def api_monitor_cameras():
    err = check_access()
    if err:
        return err
    cams = []
    if HAS_CV2:
        for i in range(8):
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


@monitor_bp.route("/api/monitor/mics")
def api_monitor_mics():
    err = check_access()
    if err:
        return err
    mics = []
    if HAS_SD:
        try:
            for i, d in enumerate(sd.query_devices()):
                if int(d.get("max_input_channels", 0)) > 0:
                    mics.append({"index": i,
                                 "name": str(d.get("name", "麦克风 %d" % (i + 1)))})
        except Exception:
            pass
    if not mics:
        mics.append({"index": -1, "name": "系统默认麦克风"})
    return jsonify({"ok": True, "mics": mics})


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
    q = queue.Queue(maxsize=8)  # 8 x 25ms ≈ 200ms，保持低延迟
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


def _close_out_stream():
    global _OUT_STREAM
    try:
        if _OUT_STREAM is not None:
            _OUT_STREAM.stop()
            _OUT_STREAM.close()
    except Exception:
        pass
    _OUT_STREAM = None


@monitor_bp.route("/api/monitor/intercom", methods=["POST"])
def api_monitor_intercom():
    global _OUT_STREAM, _OUT_LAST
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
        with _OUT_LOCK:
            # 空闲超时关闭
            if _OUT_STREAM is not None and time.time() - _OUT_LAST > 20:
                _close_out_stream()
            if _OUT_STREAM is None:
                _OUT_STREAM = sd.OutputStream(samplerate=SAMPLE_RATE,
                                              channels=CHANNELS, dtype="int16",
                                              latency="low")
                _OUT_STREAM.start()
            _OUT_LAST = time.time()
            stream = _OUT_STREAM
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if factor != 1.0:
            samples = samples * factor
            np.clip(samples, -32768, 32767, out=samples)
        stream.write(samples.astype(np.int16))  # sounddevice 需要 numpy 数组
        return jsonify({"ok": True})
    except Exception as exc:
        with _OUT_LOCK:
            _close_out_stream()
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
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        with _CAM_LOCK:
            _CAMS_IN_USE.discard(index)
        _ws_fail(ws, "无法打开摄像头 %d" % (index + 1))
        return
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 20)
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 55])
            if ok:
                try:
                    ws.send(buf.tobytes())
                except Exception:
                    break
            time.sleep(0.04)
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
    q = queue.Queue(maxsize=8)  # 8 x 25ms ≈ 200ms，保持低延迟
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
                with _OUT_LOCK:
                    if _OUT_STREAM is not None and time.time() - _OUT_LAST > 20:
                        _close_out_stream()
                    if _OUT_STREAM is None:
                        _OUT_STREAM = sd.OutputStream(samplerate=SAMPLE_RATE,
                                                      channels=CHANNELS, dtype="int16",
                                                      latency="low")
                        _OUT_STREAM.start()
                    _OUT_LAST = time.time()
                    stream = _OUT_STREAM
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                if factor != 1.0:
                    samples = samples * factor
                    np.clip(samples, -32768, 32767, out=samples)
                stream.write(samples.astype(np.int16))
            except Exception:
                with _OUT_LOCK:
                    _close_out_stream()
    finally:
        pass
