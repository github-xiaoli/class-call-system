# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
远程控制：
- 屏幕 MJPEG 流（手机端画面即触控板）
- 鼠标移动/点击/滚轮、键盘输入（SendInput 注入）
- 控制期间隐藏本机鼠标，结束时恢复
- 控制期间右下角弹窗显示“%teacher%正在远程控制电脑”，可关闭，远程结束自动关闭
仅使用开源库：Pillow（屏幕捕获）。
"""
import ctypes
import io
import json
import os
import subprocess
import threading
import time

from flask import Blueprint, Response, jsonify, request

from auth import check_access

remote_bp = Blueprint("remote", __name__)

try:
    from PIL import ImageGrab
    HAS_PIL = True
except Exception:
    HAS_PIL = False

_desktop = None


def set_desktop(d):
    global _desktop
    _desktop = d


_LOCK = threading.Lock()
_remote_active = False
_remote_teacher = ""
_last_activity = 0.0

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000
_osk_opened = False  # 是否由远程控制打开过屏幕键盘（避免误关用户自己开的键盘）


def _osk_running():
    """检测 Windows 屏幕键盘进程（TabTip / osk）是否在运行。"""
    try:
        for name in ("TabTip.exe", "osk.exe"):
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq %s" % name, "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW, check=False)
            if name in proc.stdout:
                return True
    except Exception:
        pass
    return False


def _open_osk():
    """打开 Windows 屏幕键盘（osk.exe）。"""
    global _osk_opened
    try:
        os.startfile("osk.exe")
    except Exception:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "osk.exe"],
                             creationflags=_CREATE_NO_WINDOW)
        except Exception:
            return "无法启动屏幕键盘"
    _osk_opened = True
    return None


def _close_osk(force=False):
    """关闭屏幕键盘（强制结束 TabTip/osk 进程，避免 UI 注入无法点掉的情况）。
    force=False 时只关闭由远程控制打开的键盘，避免误关用户自己开的键盘。"""
    global _osk_opened
    if not force and not _osk_opened:
        return
    for name in ("TabTip.exe", "osk.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           capture_output=True, timeout=10,
                           creationflags=_CREATE_NO_WINDOW, check=False)
        except Exception:
            pass
    _osk_opened = False


def _toggle_osk():
    """OSK 开关：再点一次即关闭，解决“屏幕键盘弹出后关不掉”的问题。"""
    if _osk_running():
        _close_osk(force=True)
    else:
        return _open_osk()
    return None

# ---------------------------------------------------------------------------
# Win32 常量与结构
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_MAP = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "delete": 0x2E,
}
# 数字键 0-9
for _i in range(10):
    VK_MAP[str(_i)] = 0x30 + _i
# 字母键 A-Z
for _i in range(26):
    VK_MAP[chr(ord("a") + _i)] = 0x41 + _i
# F1-F12
for _i in range(1, 13):
    VK_MAP["f%d" % _i] = 0x70 + (_i - 1)
# 常用符号（VK）
VK_MAP.update({
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
})
MOD_MAP = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
           "win": 0x5B, "meta": 0x5B}

CURSOR_IDS = [32512, 32513, 32649, 32650, 32515, 32642, 32644, 32645, 32646]
_orig_cursors = {}
_invis_cursor = None


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _send(inputs):
    n = len(inputs)
    if not n:
        return 0
    arr = (INPUT * n)(*inputs)
    return _user32.SendInput(n, arr, ctypes.sizeof(INPUT))


# ---------------------------------------------------------------------------
# Windows 触摸注入（InitializeTouchInjection / InjectTouchInput）
# ---------------------------------------------------------------------------
TOUCH_FEEDBACK_DEFAULT = 0x1
PT_TOUCH = 2
POINTER_FLAG_INRANGE = 0x2
POINTER_FLAG_INCONTACT = 0x4
POINTER_FLAG_DOWN = 0x10000
POINTER_FLAG_UPDATE = 0x20000
POINTER_FLAG_UP = 0x40000


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", ctypes.c_uint32),
        ("hwndTarget", ctypes.c_void_p),
        ("ptPixelLocation", _POINT),
        ("ptHimetricLocation", _POINT),
        ("ptPixelLocationRaw", _POINT),
        ("ptHimetricLocationRaw", _POINT),
        ("dwTime", ctypes.c_uint32),
        ("historyCount", ctypes.c_uint32),
        ("inputData", ctypes.c_uint32),
        ("dwKeyStates", ctypes.c_uint32),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", _POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", _RECT),
        ("rcContactRaw", _RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


_touch_ready = False
_touch_init_tried = False


def _init_touch():
    global _touch_ready, _touch_init_tried
    if _touch_init_tried:
        return _touch_ready
    _touch_init_tried = True
    try:
        _touch_ready = bool(_user32.InitializeTouchInjection(10, TOUCH_FEEDBACK_DEFAULT))
    except Exception:
        _touch_ready = False
    return _touch_ready


def inject_touch(x, y, action, pointer_id=1):
    """注入单指触摸。action: down | move | up；x,y 为屏幕像素坐标。
    注意：仅设置必要的字段，pressure/himetric/raw/rcContact 等字段会导致
    InjectTouchInput 返回 ERROR_INVALID_PARAMETER(87)。"""
    if not _init_touch():
        return False
    try:
        info = _POINTER_TOUCH_INFO()
        info.pointerInfo.pointerType = PT_TOUCH
        info.pointerInfo.pointerId = pointer_id
        flags = POINTER_FLAG_INRANGE
        if action == "down":
            flags |= POINTER_FLAG_DOWN | POINTER_FLAG_INCONTACT
        elif action == "move":
            flags |= POINTER_FLAG_UPDATE | POINTER_FLAG_INCONTACT
        elif action == "up":
            flags |= POINTER_FLAG_UP
        info.pointerInfo.pointerFlags = flags
        info.pointerInfo.ptPixelLocation = _POINT(int(x), int(y))
        info.orientation = 90
        info.touchFlags = 0
        info.touchMask = 0
        arr = (_POINTER_TOUCH_INFO * 1)(info)
        return bool(_user32.InjectTouchInput(1, arr))
    except Exception:
        return False


def _mouse(dx=0, dy=0, flags=0, data=0):
    mi = MOUSEINPUT()
    mi.dx, mi.dy, mi.mouseData, mi.dwFlags = dx, dy, data, flags
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = mi
    return inp


def _key(vk, flags=0, scan=0):
    ki = KEYBDINPUT()
    ki.wVk, ki.wScan, ki.dwFlags = vk, scan, flags
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = ki
    return inp


def move_mouse(dx, dy):
    _send([_mouse(int(dx), int(dy), MOUSEEVENTF_MOVE)])


def move_absolute(x, y):
    """绝对定位：把屏幕坐标 (x,y) 映射到 0..65535 归一化坐标，不受鼠标加速影响。"""
    sw = _user32.GetSystemMetrics(0)   # SM_CXSCREEN
    sh = _user32.GetSystemMetrics(1)   # SM_CYSCREEN
    if sw <= 0 or sh <= 0:
        sw, sh = 1920, 1080
    nx = max(0, min(65535, int(round(float(x) / sw * 65535))))
    ny = max(0, min(65535, int(round(float(y) / sh * 65535))))
    _send([_mouse(nx, ny, MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE)])


def get_screen_size():
    sw = _user32.GetSystemMetrics(0)
    sh = _user32.GetSystemMetrics(1)
    if sw <= 0 or sh <= 0:
        sw, sh = 1920, 1080
    return sw, sh


def mouse_button(button, down):
    fmap = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    d, u = fmap.get(button, fmap["left"])
    _send([_mouse(flags=d if down else u)])


def scroll_mouse(delta):
    _send([_mouse(data=int(delta) * 120, flags=MOUSEEVENTF_WHEEL)])


def send_text(text):
    inputs = []
    for ch in text:
        inputs.append(_key(ord(ch), KEYEVENTF_UNICODE, ord(ch)))
        inputs.append(_key(ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, ord(ch)))
    _send(inputs)


def tap_vk(vk):
    _send([_key(vk, 0), _key(vk, KEYEVENTF_KEYUP)])


# ---------------------------------------------------------------------------
# 鼠标隐藏/恢复
# ---------------------------------------------------------------------------
def _copy_cursor(h):
    try:
        return _user32.CopyImage(h, 2, 0, 0, 0)  # IMAGE_CURSOR=2
    except Exception:
        return None


def hide_cursor():
    global _invis_cursor
    with _LOCK:
        if _invis_cursor:
            return
        try:
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            and_mask = (ctypes.c_ubyte * 1)(0xFF)
            xor_mask = (ctypes.c_ubyte * 1)(0x00)
            inv = _user32.CreateCursor(hinst, 0, 0, 1, 1, and_mask, xor_mask)
            if not inv:
                return
            _invis_cursor = inv
            for cid in CURSOR_IDS:
                try:
                    orig = _copy_cursor(_user32.LoadCursorW(None, cid))
                    if orig:
                        _orig_cursors[cid] = orig
                        _user32.SetSystemCursor(_copy_cursor(inv), cid)
                except Exception:
                    pass
        except Exception:
            pass


def restore_cursor():
    global _invis_cursor
    with _LOCK:
        for cid, h in _orig_cursors.items():
            try:
                _user32.SetSystemCursor(_copy_cursor(h), cid)
            except Exception:
                pass
        _orig_cursors.clear()
        if _invis_cursor:
            try:
                _user32.DestroyCursor(_invis_cursor)
            except Exception:
                pass
            _invis_cursor = None


# ---------------------------------------------------------------------------
# 屏幕流
# ---------------------------------------------------------------------------
def _gen_screen():
    global _last_activity
    while True:
        with _LOCK:
            _last_activity = time.time()
        t0 = time.time()
        data = b""
        try:
            img = ImageGrab.grab(all_screens=False)
            w, h = img.size
            scale = 1280.0 / max(w, 1)
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=45)
            data = buf.getvalue()
        except Exception:
            pass
        if data:
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                   str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
        dt = time.time() - t0
        time.sleep(max(0.03, 0.1 - dt))


def _grab_frame():
    """抓取一帧屏幕，返回 JPEG bytes（失败返回 b""）。"""
    try:
        img = ImageGrab.grab(all_screens=False)
        w, h = img.size
        scale = 1280.0 / max(w, 1)
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=45)
        return buf.getvalue()
    except Exception:
        return b""


def handle_input(data):
    """处理一条输入指令，成功返回 None，失败返回错误字符串。"""
    action = str(data.get("action", ""))
    if action == "move":
        move_mouse(int(round(float(data.get("dx", 0)))),
                   int(round(float(data.get("dy", 0)))))
    elif action == "absmove":
        move_absolute(int(round(float(data.get("x", 0)))),
                      int(round(float(data.get("y", 0)))))
    elif action == "click":
        button = str(data.get("button", "left"))
        if button not in ("left", "right", "middle"):
            button = "left"
        mouse_button(button, True)
        mouse_button(button, False)
    elif action == "mousedown":
        mouse_button(str(data.get("button", "left")), True)
    elif action == "mouseup":
        mouse_button(str(data.get("button", "left")), False)
    elif action == "scroll":
        scroll_mouse(int(float(data.get("delta", 0))))
    elif action == "touch":
        # 手机端触摸：优先注入 Windows 触摸；不支持时回退为鼠标
        ttype = str(data.get("type", "move"))
        x = int(round(float(data.get("x", 0))))
        y = int(round(float(data.get("y", 0))))
        try:
            pid = int(data.get("pointer_id", 1) or 1)
        except Exception:
            pid = 1
        pid = max(1, min(10, pid))
        if not inject_touch(x, y, ttype, pid):
            if ttype == "down":
                move_absolute(x, y)
                mouse_button("left", True)
            elif ttype == "up":
                mouse_button("left", False)
            else:
                move_absolute(x, y)
    elif action == "osk":
        return _toggle_osk()
    elif action == "key":
        k = str(data.get("key", "")).lower()
        vk = VK_MAP.get(k)
        if vk is None:
            if len(k) == 1:
                send_text(k)
            else:
                return "未知按键"
        else:
            tap_vk(vk)
    elif action == "keydown":
        vk = VK_MAP.get(str(data.get("key", "")).lower()) or MOD_MAP.get(str(data.get("key", "")).lower())
        if vk:
            _send([_key(vk, 0)])
    elif action == "keyup":
        vk = VK_MAP.get(str(data.get("key", "")).lower()) or MOD_MAP.get(str(data.get("key", "")).lower())
        if vk:
            _send([_key(vk, KEYEVENTF_KEYUP)])
    elif action == "text":
        send_text(str(data.get("text", "")))
    elif action == "combo":
        keys = list(data.get("keys") or [])
        vks = []
        for k in keys:
            ks = str(k).lower()
            vk = MOD_MAP.get(ks) or VK_MAP.get(ks)
            if vk is None and len(ks) == 1:
                vk = ord(ks)
            if vk is None:
                return "未知按键组合"
            vks.append(vk)
        for v in vks:
            _send([_key(v, 0)])
        for v in reversed(vks):
            _send([_key(v, KEYEVENTF_KEYUP)])
    else:
        return "未知操作"
    return None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@remote_bp.route("/api/remote/start", methods=["POST"])
def api_remote_start():
    global _remote_active, _remote_teacher, _last_activity
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    teacher = str(data.get("teacher", "")).strip() or "老师"
    with _LOCK:
        _remote_active = True
        _remote_teacher = teacher
        _last_activity = time.time()
    if _desktop is not None:
        try:
            _desktop.show_remote_popup(teacher)
        except Exception:
            pass
    return jsonify({"ok": True, "active": True})


@remote_bp.route("/api/remote/stop", methods=["POST"])
def api_remote_stop():
    global _remote_active
    with _LOCK:
        _remote_active = False
    if _desktop is not None:
        try:
            _desktop.close_remote_popup()
        except Exception:
            pass
    _close_osk()
    return jsonify({"ok": True, "active": False})


@remote_bp.route("/api/remote/status")
def api_remote_status():
    err = check_access()
    if err:
        return err
    with _LOCK:
        return jsonify({"ok": True, "active": _remote_active,
                        "teacher": _remote_teacher})


@remote_bp.route("/api/remote/screen")
def api_remote_screen():
    err = check_access()
    if err:
        return err
    if not HAS_PIL:
        return jsonify({"ok": False, "error": "未安装屏幕捕获组件"}), 500
    with _LOCK:
        active = _remote_active
        _last_activity = time.time()
    if not active:
        return jsonify({"ok": False, "error": "远程控制未开启"}), 403
    resp = Response(_gen_screen(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@remote_bp.route("/api/remote/screen_size")
def api_remote_screen_size():
    err = check_access()
    if err:
        return err
    sw, sh = get_screen_size()
    return jsonify({"ok": True, "width": sw, "height": sh})


@remote_bp.route("/api/remote/input", methods=["POST"])
def api_remote_input():
    global _last_activity
    err = check_access()
    if err:
        return err
    with _LOCK:
        active = _remote_active
        _last_activity = time.time()
    if not active:
        return jsonify({"ok": False, "error": "远程控制未开启"}), 403
    data = request.get_json(silent=True) or {}
    err = handle_input(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 看门狗：页面断开后自动结束远程控制，恢复鼠标、关闭弹窗
# ---------------------------------------------------------------------------
def _watchdog():
    while True:
        time.sleep(5)
        global _remote_active
        with _LOCK:
            if _remote_active and time.time() - _last_activity > 30:
                _remote_active = False
        if not _remote_active:
            if _desktop is not None:
                try:
                    _desktop.close_remote_popup()
                except Exception:
                    pass


threading.Thread(target=_watchdog, daemon=True, name="remote-watchdog").start()


def remote_shutdown():
    global _remote_active
    _remote_active = False
    restore_cursor()
    _close_osk()


def ws_remote(ws):
    """远程控制 WebSocket：电脑持续发送屏幕帧，浏览器发送鼠标/键盘指令。"""
    global _remote_active, _remote_teacher, _last_activity
    err = check_access()
    if err:
        try:
            ws.send(json.dumps({"ok": False, "error": "需要密码激活后才能使用该功能"},
                               ensure_ascii=False))
            ws.close()
        except Exception:
            pass
        return
    if not HAS_PIL:
        try:
            ws.send(json.dumps({"ok": False, "error": "未安装屏幕捕获组件"},
                               ensure_ascii=False))
            ws.close()
        except Exception:
            pass
        return
    teacher = str(request.args.get("teacher", "")).strip() or "老师"
    with _LOCK:
        _remote_active = True
        _remote_teacher = teacher
        _last_activity = time.time()
    if _desktop is not None:
        try:
            _desktop.show_remote_popup(teacher)
        except Exception:
            pass
    try:
        last_frame = 0.0
        while True:
            now = time.time()
            if now - last_frame >= 0.125:  # 约 8 帧/秒，降低带宽与卡顿
                last_frame = now
                data = _grab_frame()
                if data:
                    try:
                        ws.send(data)
                    except Exception:
                        break
            # 非阻塞接收输入指令
            for _ in range(20):
                try:
                    msg = ws.receive(timeout=0.001)
                except Exception:
                    msg = None
                if msg is None:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    handle_input(json.loads(str(msg)))
                except Exception:
                    pass
            with _LOCK:
                _last_activity = time.time()
            time.sleep(0.01)
    finally:
        with _LOCK:
            _remote_active = False
        if _desktop is not None:
            try:
                _desktop.close_remote_popup()
            except Exception:
                pass
        _close_osk()
