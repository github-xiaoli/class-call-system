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
_config_getter = None


def set_config_getter(fn):
    global _config_getter
    _config_getter = fn


def _touch_mode():
    """auto（默认，单指鼠标/双指触摸） / touch（全部触摸注入） / mouse（全部鼠标）"""
    try:
        cfg = (_config_getter or (lambda: {}))()
        mode = str(cfg.get("remote_touch_mode", "mouse") or "mouse").strip().lower()
        return mode if mode in ("auto", "touch", "mouse") else "mouse"
    except Exception:
        return "auto"


HAS_TOUCH_API = hasattr(ctypes.windll.user32, "InjectTouchInput")
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


def _touch_init_tried_global_reset():
    """允许重复自检：清掉初始化缓存，重新 InitializeTouchInjection。"""
    global _touch_ready, _touch_init_tried
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


def _screen_dpi():
    """当前主屏 DPI（默认 96）。"""
    try:
        dc = _user32.GetDC(0)
        try:
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(dc, 88))  # LOGPIXELSX
            return dpi if dpi > 0 else 96
        finally:
            _user32.ReleaseDC(0, dc)
    except Exception:
        return 96


def _to_himetric(px, dpi=None):
    """像素 -> HIMETRIC（1 单位 = 0.01mm）。触点定位靠它，不能直接塞像素。"""
    d = dpi or _screen_dpi()
    return int(round(px * 2540.0 / max(1, d)))


def _touch_contact(x, y, action, pointer_id=1):
    """构造一个触点的 POINTER_TOUCH_INFO。"""
    info = _POINTER_TOUCH_INFO()
    info.pointerInfo.pointerType = PT_TOUCH
    info.pointerInfo.pointerId = int(pointer_id)
    flags = POINTER_FLAG_INRANGE
    if action == "down":
        flags |= POINTER_FLAG_DOWN | POINTER_FLAG_INCONTACT
    elif action == "move":
        flags |= POINTER_FLAG_UPDATE | POINTER_FLAG_INCONTACT
    elif action == "up":
        flags |= POINTER_FLAG_UP
    info.pointerInfo.pointerFlags = flags
    info.pointerInfo.ptPixelLocation = _POINT(int(x), int(y))
    # 注意：绝对不要设置 ptHimetricLocation！
    # Windows 要求 himetric 必须与"鼠标/笔设备上报的原始值"自洽，合成输入没有对应设备，
    # 填任何值（像素或换算值）都会让 InjectTouchInput 返回 87 ERROR_INVALID_PARAMETER，
    # 表现就是"完全点不动"。保持 0,0 即可，系统按像素坐标定位。
    try:
        info.pointerInfo.dwTime = int(ctypes.windll.kernel32.GetTickCount())
    except Exception:
        pass
    info.orientation = 90
    info.touchFlags = 0
    info.touchMask = 0
    return info


def _last_error():
    try:
        return int(ctypes.windll.kernel32.GetLastError())
    except Exception:
        return -1


_GESTURE = {"touch": False, "active": {}}
_LAST_INPUT_LOG = [0.0]


def _reset_gesture():
    """新连接/断线时清空触点与手势状态（顺手抬起残留触点，避免后续注入 87）。"""
    try:
        touch_reset(True)
        gesture_reset()
    except Exception as exc:
        print("[远控] 重置触点状态失败:", exc)


def _mouse_emulate(ttype, x, y):
    """鼠标路径：按下 / 移动 / 抬起。"""
    if ttype == "down":
        move_absolute(x, y)
        mouse_button("left", True)
    elif ttype in ("up", "cancel"):
        mouse_button("left", False)
    else:
        move_absolute(x, y)


def _handle_touch(data):
    """手机触摸分发：默认走 Windows 多点触摸 API，失败自动降级鼠标。

    触摸注入 = InitializeTouchInjection + InjectTouchInput（Win8+ 多点触摸 API）。
    注入的触点会以 WM_POINTERDOWN 投递给目标窗口，普通 Win32 窗口还会被系统
    自动 promote 成 WM_LBUTTONDOWN，所以"真触摸"和"点得动"可以同时成立。
    """
    ttype = str(data.get("type", "move"))
    try:
        x = int(round(float(data.get("x", 0))))
        y = int(round(float(data.get("y", 0))))
    except Exception:
        x, y = 0, 0
    try:
        pid = int(data.get("pointer_id", 1) or 1)
    except Exception:
        pid = 1
    pid = max(1, min(10, pid))
    mode = _touch_mode()

    now = time.time()
    if now - _LAST_INPUT_LOG[0] > 1.0:
        _LAST_INPUT_LOG[0] = now
        print("[远控] 触摸 %s pid=%d (%d,%d) 模式=%s%s"
              % (ttype, pid, x, y, mode,
                 "（已降级鼠标）" if touch_fallback_active() else ""))

    # 双指手势（缩放/滚动）：优先于单点注入，任何程序都生效
    if mode != "touch":
        if _gesture_feed(ttype, x, y, pid):
            if _TOUCH_STATE["down"]:
                touch_reset(True)     # 手势期间把残留触点抬干净，避免 87
            return

    if mode == "mouse" or touch_fallback_active():
        _mouse_emulate(ttype, x, y)
        return

    # 已经进入 batch 模式：每帧带上全部触点
    if _TOUCH_STATE.get("batch"):
        ok = _inject_frame(x, y, ttype, pid)
        if ok:
            return
        if mode == "touch":
            return
        _TOUCH_STATE["fallback_mouse"] = True
        print("[远控] 批量注入也不可用，本会话改用鼠标模式")
        _mouse_emulate(ttype, x, y)
        return

    ok = inject_touches([{"x": x, "y": y, "action": ttype, "pointer_id": pid}])
    if ok or mode == "touch":
        return

    # 顺序注入被拒：如果已经有手指按着，就切到"批量帧"再试一次
    if _TOUCH_STATE["down"]:
        _TOUCH_STATE["batch"] = True
        st = _TOUCH_STATE
        st["pts"] = {}
        for lp, cid in list(_PID_MAP.items()):
            px, py = st.get("pos", {}).get(lp, (x, y))
            st["pts"][lp] = (px, py, cid)
        print("[远控] 顺序注入被拒，切换为批量帧模式（一次调用下发全部触点）")
        if _inject_frame(x, y, ttype, pid):
            return
        _TOUCH_STATE["batch"] = False

    # 仍失败：auto 模式降级鼠标
    _TOUCH_STATE["fallback_mouse"] = True
    print("[远控] 触摸注入不可用，本会话改用鼠标模式（不影响使用）")
    _mouse_emulate(ttype, x, y)


# 双指手势状态：跟踪每根手指的位置，算出缩放/滚动
_GESTURE_STATE = {"pts": {}, "prev_dist": None, "prev_cy": None,
                  "zoom_acc": 0.0, "scroll_acc": 0.0, "last": 0.0,
                  "active": False}


def _gesture_mode():
    try:
        cfg = (_config_getter or (lambda: {}))()
        m = str(cfg.get("remote_gesture_mode", "zoom_scroll") or "zoom_scroll").lower()
        return m if m in ("zoom_scroll", "touch") else "zoom_scroll"
    except Exception:
        return "zoom_scroll"


def gesture_reset():
    _GESTURE_STATE.update({"pts": {}, "prev_dist": None, "prev_cy": None,
                           "zoom_acc": 0.0, "scroll_acc": 0.0,
                           "last": 0.0, "active": False})


def _gesture_feed(ttype, x, y, pid):
    """喂一个触摸事件；返回 True 表示这次事件已被手势消费（不要再注入触点）。"""
    # 手势模式设为「真实触摸注入」时，不消费任何事件，让多根手指都真正注入
    if _gesture_mode() != "zoom_scroll":
        return False
    st = _GESTURE_STATE
    if ttype == "down":
        if not st["pts"]:
            gesture_reset()
        st["pts"][pid] = (x, y)
        if len(st["pts"]) == 2:
            ps = list(st["pts"].values())[:2]
            d = ((ps[0][0] - ps[1][0]) ** 2 + (ps[0][1] - ps[1][1]) ** 2) ** 0.5
            st["prev_dist"] = max(1.0, d)
            st["prev_cy"] = (ps[0][1] + ps[1][1]) / 2.0
            st["zoom_acc"] = 0.0
            st["scroll_acc"] = 0.0
            st["active"] = True
            st["last"] = 0.0
            print("[远控] 双指手势开始（%s）" % _gesture_mode())
            return True          # 第二根手指按下：不注入
        return False

    if ttype in ("up", "cancel"):
        was = st["active"]
        st["pts"].pop(pid, None)
        if len(st["pts"]) < 2:
            st["active"] = False
            st["prev_dist"] = None
            st["prev_cy"] = None
        return was

    # move
    if pid not in st["pts"]:
        return False
    st["pts"][pid] = (x, y)
    if not st["active"] or len(st["pts"]) < 2 or st["prev_dist"] is None:
        return False

    ps = list(st["pts"].values())[:2]
    dist = max(1.0, ((ps[0][0] - ps[1][0]) ** 2 + (ps[0][1] - ps[1][1]) ** 2) ** 0.5)
    cy = (ps[0][1] + ps[1][1]) / 2.0

    # 与上一帧比较，累积缩放/滚动量
    st["zoom_acc"] += (dist / st["prev_dist"] - 1.0) * 8.0
    st["scroll_acc"] += (cy - st["prev_cy"]) / 26.0
    st["prev_dist"] = dist
    st["prev_cy"] = cy

    now = time.time()
    if now - st["last"] < 0.03:
        return True
    st["last"] = now

    if _gesture_mode() == "zoom_scroll":
        # 滚轮事件是发给"光标下的窗口"的，所以先把光标挪到双指中心
        try:
            move_absolute(int(ps[0][0]), int(ps[0][1]))
        except Exception:
            pass
        z = int(st["zoom_acc"])
        if z:
            st["zoom_acc"] -= z
            _ctrl_wheel(z)
        sc = int(st["scroll_acc"])
        if sc:
            st["scroll_acc"] -= sc
            scroll_mouse(-sc * 2)
    return True


# 每个 WebSocket 连接一份触点状态；断线/新连接时强制清空
_TOUCH_STATE = {"down": set(), "fallback_mouse": False, "failures": 0,
                "batch": False, "pts": {}}
_PID_MAP = {}      # 逻辑手指号(1..10) -> 实际注入用的 pointerId


def touch_reset(release=True):
    """重置触点状态；release=True 时先把手里的触点全部抬起（防止 87 连锁）。"""
    global _TOUCH_STATE, _PID_MAP
    if release:
        for cid in list(_TOUCH_STATE["down"]):
            try:
                arr = (_POINTER_TOUCH_INFO * 1)(_touch_contact(0, 0, "up", cid))
                _user32.InjectTouchInput(1, arr)
            except Exception:
                pass
    _TOUCH_STATE = {"down": set(), "fallback_mouse": False, "failures": 0,
                    "batch": False, "pts": {}}
    _PID_MAP = {}


def touch_fallback_active():
    return bool(_TOUCH_STATE.get("fallback_mouse"))


def _raw_inject(x, y, action, cid):
    """真正调用 InjectTouchInput，返回 (ok, err)。"""
    try:
        arr = (_POINTER_TOUCH_INFO * 1)(_touch_contact(x, y, action, cid))
        ok = bool(_user32.InjectTouchInput(1, arr))
        return ok, (0 if ok else _last_error())
    except Exception as exc:
        print("[远控] InjectTouchInput 异常:", exc)
        return False, -1


def _inject_frame(x, y, action, logic_pid):
    """把"当前所有触点 + 本次变化"打包成一次 InjectTouchInput 调用。

    有些机器只接受这种帧式下发：顺序发第二个触点会返回 87。
    """
    st = _TOUCH_STATE
    pts = st.setdefault("pts", {})          # 逻辑手指 -> (x, y, 注入 id)
    mapped = _PID_MAP.get(logic_pid)

    if action == "down":
        if mapped is None:
            # 分配一个空闲 id
            used = {v[2] for v in pts.values()}
            cand = next((i for i in [logic_pid] + list(range(1, 11)) if i not in used), None)
            if cand is None:
                return False
            mapped = cand
            _PID_MAP[logic_pid] = cand
        pts[logic_pid] = (x, y, mapped)
    elif action in ("move",):
        if logic_pid not in pts:
            return False
        pts[logic_pid] = (x, y, pts[logic_pid][2])
    else:                                   # up / cancel
        if logic_pid not in pts:
            return True

    items = []
    for lp, (px, py, cid) in list(pts.items()):
        if lp == logic_pid:
            if action in ("up", "cancel"):
                flags = 0x2 | 0x40000       # INRANGE | UP
            elif action == "down":
                flags = 0x2 | 0x10000 | 0x4  # INRANGE | DOWN | INCONTACT
            else:
                flags = 0x2 | 0x20000 | 0x4  # INRANGE | UPDATE | INCONTACT
        else:
            flags = 0x2 | 0x20000 | 0x4      # 其它触点：UPDATE 停留在原位
        items.append(_touch_contact(px, py, "down" if flags & 0x10000 else
                                   ("up" if flags & 0x40000 else "move"), cid))

    try:
        arr = (_POINTER_TOUCH_INFO * len(items))(*items)
        ok = bool(_user32.InjectTouchInput(len(items), arr))
    except Exception as exc:
        print("[远控] 批量注入异常:", exc)
        ok = False

    if ok:
        if action == "down":
            st["down"].add(mapped)
        elif action in ("up", "cancel"):
            pts.pop(logic_pid, None)
            _PID_MAP.pop(logic_pid, None)
            st["down"].discard(mapped)
        return True

    st["failures"] += 1
    print("[远控] 批量注入失败 action=%s err=%s" % (action, _last_error()))
    if action in ("up", "cancel"):
        pts.pop(logic_pid, None)
        _PID_MAP.pop(logic_pid, None)
        st["down"].discard(mapped)
    return False


def _inject_one(x, y, action, logic_pid):
    """注入单个触点；pointerId 冲突(87)时自动换一个空闲 id。"""
    st = _TOUCH_STATE
    mapped = _PID_MAP.get(logic_pid)

    if action in ("up", "cancel"):
        if mapped is None:
            return True                      # 没按下就别抬
        ok, err = _raw_inject(x, y, "up", mapped)
        if ok or err == 87:                  # 87 说明这个触点早被系统取消了，也算结束
            st["down"].discard(mapped)
            _PID_MAP.pop(logic_pid, None)
            st.setdefault("pos", {}).pop(logic_pid, None)
            return True
        st["failures"] += 1
        print("[远控] 抬起失败 pid=%s(%d) err=%s" % (logic_pid, mapped, err))
        return False

    if action == "move":
        if mapped is None:
            return False                     # 没按下就移动：忽略
        ok, err = _raw_inject(x, y, "move", mapped)
        if ok:
            st.setdefault("pos", {})[logic_pid] = (x, y)
        if not ok:
            st["failures"] += 1
            if err == 87:
                # 触点已被系统取消（目标窗口不收触摸）：这个手指不再能用
                st["down"].discard(mapped)
                _PID_MAP.pop(logic_pid, None)
            print("[远控] 移动失败 pid=%s(%d) err=%s" % (logic_pid, mapped, err))
        return ok

    # ---- down ----
    if mapped is not None and mapped in st["down"]:
        _inject_one(x, y, "up", logic_pid)   # 先抬起，避免重复 down

    tried = []
    if mapped is not None:
        tried.append(mapped)
    for cand in [logic_pid] + list(range(1, 11)):
        if cand not in tried:
            tried.append(cand)

    for cid in tried:
        if cid in st["down"]:
            continue                         # 这个号已被其它手指占着
        ok, err = _raw_inject(x, y, "down", cid)
        if ok:
            _PID_MAP[logic_pid] = cid
            st["down"].add(cid)
            st.setdefault("pos", {})[logic_pid] = (x, y)
            if cid != logic_pid:
                print("[远控] 手指 %d 改用 pointerId=%d（%d 号被系统占用）"
                      % (logic_pid, cid, logic_pid))
            return True
        if err != 87:
            st["failures"] += 1
            print("[远控] 按下失败 pid=%d err=%s" % (cid, err))
            return False
    st["failures"] += 1
    print("[远控] 按下失败：pointerId 1~10 全部被拒（多为系统已占满或不允许注入）")
    return False


def inject_touches(items):
    """逐个注入触点（批量注入在部分机器上会 87）。任一失败返回 False。"""
    if not items:
        return True
    if not _init_touch():
        return False
    all_ok = True
    for it in items:
        try:
            pid = int(it.get("pointer_id", 1))
        except Exception:
            pid = 1
        action = str(it.get("action", "move"))
        all_ok = _inject_one(it.get("x", 0), it.get("y", 0), action, pid) and all_ok
    return all_ok


def inject_touch(x, y, action, pointer_id=1):
    """注入单指触摸。action: down | move | up；x,y 为屏幕像素坐标。"""
    if not _init_touch():
        return False
    return inject_touches([{"x": x, "y": y, "action": action,
                            "pointer_id": pointer_id}])


def touch_supported():
    """触摸注入是否可用（初始化成功即视为可用）。"""
    return bool(_init_touch())


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


VK_CONTROL_ = 0x11


def _ctrl_wheel(steps):
    """按住 Ctrl 滚轮（多数程序里等于缩放）。"""
    n = int(steps)
    if n == 0:
        return
    _send([_key(VK_CONTROL_, 0)])
    try:
        for _ in range(abs(n)):
            scroll_mouse(1 if n > 0 else -1)
    finally:
        _send([_key(VK_CONTROL_, KEYEVENTF_KEYUP)])


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


def _grab_frame(quality=40, max_width=1024):
    """抓取一帧屏幕，返回 JPEG bytes（失败返回 b""）。"""
    try:
        img = ImageGrab.grab(all_screens=False)
        w, h = img.size
        # 大屏先按整数倍快速降采样（C 实现，比 resize 快几倍），再精确缩到目标宽度
        while w > max_width * 2:
            img = img.reduce(2)
            w, h = img.size
        if w > max_width:
            scale = float(max_width) / w
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             resample=1)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=int(quality))
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
        try:
            _handle_touch(data)
        except Exception as exc:
            print("[远控] 处理触摸事件失败:", exc)
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
    _reset_gesture()          # 新连接，清掉上次可能残留的触点状态
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


@remote_bp.route("/api/remote/touch_selftest")
def api_touch_selftest():
    """触摸自检：逐个 pointerId 探测 + 双指配对，返回每一步的 ok/err。"""
    from auth import is_local
    if not is_local():
        return jsonify({"ok": False, "error": "只能在本机自检"}), 403
    if not HAS_TOUCH_API:
        return jsonify({"ok": False, "error": "当前系统不支持触摸注入 API"})

    res = {"admin": False, "init": None, "single": [], "pair": None}
    try:
        import ctypes as _ct
        res["admin"] = bool(_ct.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass

    try:
        w, h = get_screen_size()
        cx, cy = w // 2, h // 2
    except Exception:
        cx, cy = 400, 300

    _touch_init_tried_global_reset()
    ok_init = bool(_init_touch())
    res["init"] = {"ok": ok_init, "err": _last_error()}
    if not ok_init:
        res["ok"] = True
        res["hint"] = "InitializeTouchInjection 失败，无法使用触摸注入"
        return jsonify(res)

    # 逐个 id 探测：能不能单独按一下
    good_ids = []
    for cid in range(1, 11):
        touch_reset(False)
        ok, err = _raw_inject(cx, cy, "down", cid)
        if ok:
            _user32.InjectTouchInput(1, (_POINTER_TOUCH_INFO * 1)(_touch_contact(cx, cy, "up", cid)))
            good_ids.append(cid)
        res["single"].append({"id": cid, "ok": bool(ok), "err": err})
    touch_reset(False)

    # 双指配对：用两个可用的 id 同时按下并移动
    if len(good_ids) >= 2:
        a, b = good_ids[0], good_ids[1]
        seq = []
        ok1, e1 = _raw_inject(cx - 60, cy, "down", a)
        ok2, e2 = _raw_inject(cx + 60, cy, "down", b)
        ok3, e3 = _raw_inject(cx - 80, cy + 10, "move", a)
        ok4, e4 = _raw_inject(cx + 80, cy + 10, "move", b)
        ok5, e5 = _raw_inject(cx - 80, cy + 10, "up", a)
        ok6, e6 = _raw_inject(cx + 80, cy + 10, "up", b)
        seq = [{"step": "downA", "id": a, "ok": bool(ok1), "err": e1},
               {"step": "downB", "id": b, "ok": bool(ok2), "err": e2},
               {"step": "moveA", "id": a, "ok": bool(ok3), "err": e3},
               {"step": "moveB", "id": b, "ok": bool(ok4), "err": e4},
               {"step": "upA", "id": a, "ok": bool(ok5), "err": e5},
               {"step": "upB", "id": b, "ok": bool(ok6), "err": e6}]
        res["pair"] = {"ids": [a, b], "steps": seq,
                       "ok": all(x["ok"] for x in seq)}
    touch_reset(True)

    # 批量测试：两根手指放在"同一次调用"里下发（部分机器只认这种）
    if len(good_ids) >= 2:
        a, b = good_ids[0], good_ids[1]
        _touch_init_tried_global_reset()
        _init_touch()
        touch_reset(False)
        t = ctypes.windll.kernel32.GetTickCount()
        batch_steps = []

        def batch(items, name):
            arr = (_POINTER_TOUCH_INFO * len(items))(*items)
            ok = bool(_user32.InjectTouchInput(len(items), arr))
            batch_steps.append({"step": name, "ok": ok, "err": 0 if ok else _last_error()})
            return ok

        def C(x, y, flags, pid, dt):
            i = _POINTER_TOUCH_INFO()
            i.pointerInfo.pointerType = PT_TOUCH
            i.pointerInfo.pointerId = pid
            i.pointerInfo.pointerFlags = flags
            i.pointerInfo.ptPixelLocation = _POINT(int(x), int(y))
            i.pointerInfo.dwTime = dt
            i.orientation = 90
            return i

        batch([C(cx - 60, cy, 0x2 | 0x10000 | 0x4, a, t),
               C(cx + 60, cy, 0x2 | 0x10000 | 0x4, b, t)], "batch_down")
        batch([C(cx - 80, cy + 10, 0x2 | 0x20000 | 0x4, a, t + 1),
               C(cx + 80, cy + 10, 0x2 | 0x20000 | 0x4, b, t + 1)], "batch_move")
        batch([C(cx - 80, cy + 10, 0x2 | 0x40000, a, t + 2),
               C(cx + 80, cy + 10, 0x2 | 0x40000, b, t + 2)], "batch_up")
        res["batch"] = {"ids": [a, b], "steps": batch_steps,
                        "ok": all(x["ok"] for x in batch_steps)}
        touch_reset(False)

    res["ok"] = True
    res["usable_ids"] = good_ids
    if res.get("batch", {}).get("ok"):
        res["hint"] = "批量注入可用：请把「双指手势」设为「真实触摸注入」测试双指同按"
        return jsonify(res)
    if len(good_ids) >= 2 and res.get("pair", {}).get("ok"):
        res["hint"] = "多点触摸可用（双指可同时按下并拖动）"
    elif len(good_ids) >= 2:
        res["hint"] = "能按两根手指，但移动被拒（目标程序/系统限制）"
    elif good_ids:
        res["hint"] = "只允许一个触点，真多点不可用；双指请用「缩放/滚动」手势"
    else:
        res["hint"] = "触摸注入完全不可用，已自动使用鼠标"
    return jsonify(res)


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
        interval = 0.066           # 目标帧间隔（约 15fps）
        quality = 40
        while True:
            now = time.time()
            if now - last_frame >= 0.125:  # 约 8 帧/秒，降低带宽与卡顿
                last_frame = now
                data = _grab_frame(quality=quality)
                if data:
                    t_send = time.time()
                    try:
                        ws.send(data)
                    except Exception:
                        break
                    cost = time.time() - t_send
                    # 网络/手机跟不上就自动降帧降质，避免"卡一下再追上"的周期性停顿
                    if cost > 0.3:
                        interval = min(0.2, interval + 0.02)
                        quality = max(32, quality - 2)
                    elif cost < 0.1:
                        interval = max(0.05, interval - 0.005)
                        quality = min(48, quality + 1)
            # 非阻塞接收输入指令
            for _ in range(4):
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
