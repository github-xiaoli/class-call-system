# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
班级叫号器 桌面组件
- 右下角叫号弹窗（学号、老师大字绿色显示，可关闭）
- 软件面板（显示叫号记录，可隐藏到托盘）
- 系统托盘图标（显示面板 / 退出）
仅使用 pywin32（开源）实现，主线程运行 Windows 消息循环。
"""

import math
import os
import queue
import threading
import time

import win32api
import win32con
import win32gui

APP_TITLE = "班级叫号器"

# 应用数据目录（用于统一图标等资源）
APP_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# 控件 ID
ID_LIST = 101
ID_BTN_HIDE = 102
ID_BTN_WEB = 103
ID_BTN_QUIT = 104
ID_STATUS = 105
ID_POP_CLOSE = 201
ID_POP_NUM = 202
ID_POP_TEACHER = 203
ID_POP_NOTE = 204

# 自定义消息
TRAY_MSG = win32con.WM_APP + 1
POP_TIMER = win32con.WM_APP + 2

GREEN = win32api.RGB(0, 150, 0)
DARK_GRAY = win32api.RGB(80, 80, 80)
WHITE = win32api.RGB(255, 255, 255)
BG = win32api.RGB(250, 252, 255)
BLUE = win32api.RGB(37, 99, 235)
LIGHT = win32api.RGB(226, 232, 240)
RED = win32api.RGB(220, 38, 38)


def _make_font(height, weight=400, italic=0, face="微软雅黑"):
    lf = win32gui.LOGFONT()
    lf.lfHeight = height
    lf.lfWidth = 0
    lf.lfWeight = weight
    lf.lfItalic = italic
    lf.lfCharSet = win32con.DEFAULT_CHARSET
    lf.lfFaceName = face
    return win32gui.CreateFontIndirect(lf)


def _center_pos(w, h):
    try:
        rect = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0))).get("Work") or (0, 0, 1920, 1040)
    except Exception:
        rect = (0, 0, 1920, 1040)
    return (rect[0] + (rect[2] - rect[0] - w) // 2,
            rect[1] + (rect[3] - rect[1] - h) // 2)


def _icon_from_static(data_dir):
    """优先用版本目录里的 static/icon.png（随更新一起下发）生成 data/icon.ico。"""
    try:
        png = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icon.png")
        if not os.path.isfile(png):
            return None
        from PIL import Image
        img = Image.open(png).convert("RGBA")
        if img.getextrema()[3][0] < 255:
            # 透明像素统一铺白底
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.alpha_composite(img)
            img = bg
        os.makedirs(data_dir, exist_ok=True)
        out = os.path.join(data_dir, "icon.ico")
        img.save(out, format="ICO",
                 sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                        (128, 128), (256, 256)])
        return out
    except Exception:
        return None


def _make_tray_icon(data_dir):
    """生成/加载托盘图标：优先 data/icon.ico，缺失时从 static/icon.png 生成。"""
    icon_path = os.path.join(data_dir, "icon.ico")
    try:
        if os.path.isfile(icon_path):
            hicon = win32gui.LoadImage(0, icon_path, win32con.IMAGE_ICON, 0, 0,
                                       win32con.LR_LOADFROMFILE)
            if hicon:
                return hicon
    except Exception:
        pass
    gen = _icon_from_static(data_dir)
    if gen:
        try:
            hicon = win32gui.LoadImage(0, gen, win32con.IMAGE_ICON, 0, 0,
                                       win32con.LR_LOADFROMFILE)
            if hicon:
                return hicon
        except Exception:
            pass
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((1, 1, 62, 62), radius=14, fill=(37, 99, 235, 255))
        d.rounded_rectangle((1, 1, 62, 62), radius=14, outline=(255, 255, 255, 70), width=2)
        font = None
        for fp in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
                   r"C:\Windows\Fonts\simhei.ttf"):
            if os.path.isfile(fp):
                try:
                    font = ImageFont.truetype(fp, 40)
                    break
                except Exception:
                    continue
        if font is None:
            font = ImageFont.load_default()
        bbox = d.textbbox((0, 0), "叫", font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((64 - tw) / 2 - bbox[0], (64 - th) / 2 - bbox[1] - 2), "叫",
               font=font, fill=(255, 255, 255, 255))
        try:
            img.save(icon_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
        except Exception:
            pass
        hicon = win32gui.LoadImage(0, icon_path, win32con.IMAGE_ICON, 0, 0,
                                   win32con.LR_LOADFROMFILE)
        if hicon:
            return hicon
    except Exception:
        pass
    return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)


class SplashWindow:
    """启动动画：居中置顶不可关闭，左侧转圈，右侧三行（大标题 / 斜体“启动中” / 状态）。
    窗口在自己的线程中运行消息循环，主线程做启动检查时动画与状态文字持续刷新。"""

    ID_STATUS = 301

    def __init__(self):
        self._hwnd = None
        self._status = None
        self._angle = 0.0
        self._proc = None
        self._font_title = None
        self._font_sub = None
        self._font_status = None
        self._pen_track = None
        self._pen_arc = None
        self._queue = queue.Queue()
        self._thread = None
        self._shown = threading.Event()

    # ------------------------------------------------------------------
    # 主线程调用
    # ------------------------------------------------------------------
    def show(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="splash")
        self._thread.start()
        self._shown.wait(5)

    def set_status(self, text):
        self._queue.put(("status", str(text)))

    def close(self):
        self._queue.put(("close", None))
        if self._thread is not None:
            self._thread.join(3)

    # ------------------------------------------------------------------
    # 动画线程
    # ------------------------------------------------------------------
    def _run(self):
        try:
            self._register()
            self._create_window()
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOW)
            win32gui.UpdateWindow(self._hwnd)
            self._shown.set()
            last = 0.0
            while self._drain():
                try:
                    win32gui.PumpWaitingMessages()
                except Exception:
                    pass
                now = time.time()
                if now - last >= 0.06:  # 不用 WM_TIMER，自己按时间旋转动画
                    last = now
                    self._angle = (self._angle + 12) % 360
                    if self._hwnd and win32gui.IsWindow(self._hwnd):
                        # bErase=True 先擦背景，避免弧线走过处残留蓝点
                        win32gui.InvalidateRect(self._hwnd, (30, 48, 122, 142), True)
                time.sleep(0.005)
        finally:
            self._cleanup()

    def _drain(self):
        """处理主线程发来的消息；收到 close 返回 False。"""
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == "status":
                    if self._status:
                        win32gui.SetWindowText(self._status, item[1])
                elif item[0] == "close":
                    return False
        except queue.Empty:
            pass
        return True

    def _register(self):
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "CQTSplashClass"
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32gui.CreateSolidBrush(WHITE)
        self._proc = self._wnd_proc
        wc.lpfnWndProc = self._proc
        win32gui.RegisterClass(wc)

    def _create_window(self):
        self._font_title = _make_font(36, 700)
        self._font_sub = _make_font(20, 400, italic=1)
        self._font_status = _make_font(20)
        self._pen_track = win32gui.CreatePen(win32con.PS_SOLID, 7, LIGHT)
        self._pen_arc = win32gui.CreatePen(win32con.PS_SOLID, 7, BLUE)
        hinst = win32api.GetModuleHandle(None)
        w, h = 580, 190
        x, y = _center_pos(w, h)
        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            "CQTSplashClass", "班级叫号器",
            win32con.WS_POPUP | win32con.WS_BORDER,
            x, y, w, h, 0, 0, hinst, None)

        win32gui.CreateWindowEx(
            0, "STATIC", "班级叫号器",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            158, 26, 404, 46, self._hwnd, 302, hinst, None)
        win32gui.CreateWindowEx(
            0, "STATIC", "启动中…",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            158, 78, 404, 32, self._hwnd, 303, hinst, None)
        self._status = win32gui.CreateWindowEx(
            0, "STATIC", "正在准备…",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            158, 116, 404, 52, self._hwnd, self.ID_STATUS, hinst, None)

        win32gui.SendMessage(win32gui.GetDlgItem(self._hwnd, 302),
                             win32con.WM_SETFONT, self._font_title, True)
        win32gui.SendMessage(win32gui.GetDlgItem(self._hwnd, 303),
                             win32con.WM_SETFONT, self._font_sub, True)
        win32gui.SendMessage(self._status, win32con.WM_SETFONT, self._font_status, True)

    def _cleanup(self):
        try:
            if self._hwnd and win32gui.IsWindow(self._hwnd):
                win32gui.DestroyWindow(self._hwnd)
        except Exception:
            pass
        self._hwnd = None
        for obj in (self._font_title, self._font_sub, self._font_status,
                    self._pen_track, self._pen_arc):
            try:
                if obj:
                    win32gui.DeleteObject(obj)
            except Exception:
                pass
        self._font_title = self._font_sub = self._font_status = None
        self._pen_track = self._pen_arc = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_PAINT:
            self._paint(hwnd)
            return 0
        elif msg == win32con.WM_CTLCOLORSTATIC:
            # 文本框背景与窗口背景一致（白色、透明文字背景）
            win32gui.SetTextColor(wparam, win32api.RGB(15, 23, 42))
            win32gui.SetBkMode(wparam, win32con.TRANSPARENT)
            return win32gui.GetStockObject(win32con.WHITE_BRUSH)
        elif msg == win32con.WM_CLOSE:
            return 0  # 启动动画不可关闭
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd):
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            cx, cy, r = 76, 95, 38
            old_pen = win32gui.SelectObject(hdc, self._pen_track)
            old_brush = win32gui.SelectObject(hdc, win32gui.GetStockObject(win32con.NULL_BRUSH))
            win32gui.Ellipse(hdc, cx - r, cy - r, cx + r, cy + r)
            win32gui.SelectObject(hdc, self._pen_arc)
            a1 = math.radians(self._angle)
            a2 = math.radians(self._angle + 280)
            p1 = (int(cx + r * math.cos(a1)), int(cy + r * math.sin(a1)))
            p2 = (int(cx + r * math.cos(a2)), int(cy + r * math.sin(a2)))
            win32gui.Arc(hdc, cx - r, cy - r, cx + r, cy + r,
                         p1[0], p1[1], p2[0], p2[1])
            win32gui.SelectObject(hdc, old_brush)
            win32gui.SelectObject(hdc, old_pen)
        finally:
            win32gui.EndPaint(hwnd, ps)


class UpdateToast:
    """右下角更新提示气泡：无标题栏、可点击、可关闭，10 秒后自动消失。

    设计目的：后台下载/解压更新包时**不再**弹大窗口挡住屏幕，只在右下角提示一下。
    点气泡 → 打开「软件更新」网页看详细进度；点右上角 ✕ → 关闭。
    """

    W, H = 384, 116
    AUTO_CLOSE = 10.0

    def __init__(self, text, on_click=None):
        self._text = str(text or "")
        self._on_click = on_click
        self._hwnd = None
        self._proc = None
        self._font_title = None
        self._font_body = None
        self._born = 0.0
        self._close_rect = (0, 0, 0, 0)

    # -- 创建/销毁 ------------------------------------------------------
    def _register(self):
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "CQTToastClass"
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_HAND)
        wc.hbrBackground = win32gui.CreateSolidBrush(WHITE)
        self._proc = self._wnd_proc
        wc.lpfnWndProc = self._proc
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass

    def show(self):
        self._register()
        self._font_title = _make_font(18, 700)
        self._font_body = _make_font(15)
        w, h = self.W, self.H
        try:
            area = win32api.GetMonitorInfo(
                win32api.MonitorFromPoint((0, 0))).get("Work") or (0, 0, 1920, 1040)
        except Exception:
            area = (0, 0, 1920, 1040)
        x = area[2] - w - 18
        y = area[3] - h - 18
        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            "CQTToastClass", "软件更新",
            win32con.WS_POPUP,
            x, y, w, h, 0, 0, win32api.GetModuleHandle(None), None)
        self._close_rect = (w - 34, 4, w - 8, 30)
        self._born = time.time()
        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.UpdateWindow(self._hwnd)

    def set_text(self, text):
        self._text = str(text or "")
        if self._hwnd and win32gui.IsWindow(self._hwnd):
            win32gui.InvalidateRect(self._hwnd, None, True)

    def expired(self):
        return self._born and (time.time() - self._born) >= self.AUTO_CLOSE

    def close(self):
        try:
            if self._hwnd and win32gui.IsWindow(self._hwnd):
                win32gui.DestroyWindow(self._hwnd)
        except Exception:
            pass
        self._hwnd = None
        for obj in (self._font_title, self._font_body):
            try:
                if obj:
                    win32gui.DeleteObject(obj)
            except Exception:
                pass
        self._font_title = self._font_body = None

    # -- 消息 ----------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_LBUTTONUP:
            x = win32api.LOWORD(lparam)
            y = win32api.HIWORD(lparam)
            r = self._close_rect
            if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                self.close()
                return 0
            if self._on_click is not None:
                try:
                    self._on_click()
                except Exception:
                    pass
            self.close()
            return 0
        elif msg == win32con.WM_SETCURSOR and win32api.LOWORD(lparam) == 1:  # HTCLIENT
            try:
                win32gui.SetCursor(win32gui.LoadCursor(0, win32con.IDC_HAND))
                return 1
            except Exception:
                pass
        elif msg == win32con.WM_PAINT:
            self._paint(hwnd)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd):
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            w, h = self.W, self.H
            brush = win32gui.CreateSolidBrush(WHITE)
            win32gui.FillRect(hdc, (0, 0, w, h), brush)
            win32gui.DeleteObject(brush)
            pen = win32gui.CreatePen(win32con.PS_SOLID, 1, LIGHT)
            old_pen = win32gui.SelectObject(hdc, pen)
            old_brush = win32gui.SelectObject(
                hdc, win32gui.GetStockObject(win32con.NULL_BRUSH))
            win32gui.Rectangle(hdc, 0, 0, w, h)
            win32gui.SelectObject(hdc, old_brush)
            win32gui.SelectObject(hdc, old_pen)
            win32gui.DeleteObject(pen)
            # 左侧蓝色装饰条
            bar = win32gui.CreateSolidBrush(BLUE)
            win32gui.FillRect(hdc, (0, 0, 4, h), bar)
            win32gui.DeleteObject(bar)

            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            old_font = win32gui.SelectObject(hdc, self._font_title)
            win32gui.SetTextColor(hdc, win32api.RGB(15, 23, 42))
            win32gui.DrawText(hdc, "班级叫号器 · 软件更新", -1,
                              (14, 8, w - 40, 32),
                              win32con.DT_LEFT | win32con.DT_SINGLELINE)
            win32gui.SetTextColor(hdc, DARK_GRAY)
            win32gui.DrawText(hdc, "✕", -1, (w - 34, 6, w - 8, 30),
                              win32con.DT_CENTER | win32con.DT_SINGLELINE)
            win32gui.SelectObject(hdc, self._font_body)
            win32gui.SetTextColor(hdc, win32api.RGB(51, 65, 85))
            win32gui.DrawText(hdc, self._text, -1, (14, 36, w - 16, h - 24),
                              win32con.DT_LEFT | win32con.DT_WORDBREAK | win32con.DT_NOPREFIX)
            win32gui.SetTextColor(hdc, BLUE)
            win32gui.DrawText(hdc, "点击这里查看更新进度 · 10 秒后自动关闭", -1,
                              (14, h - 24, w - 16, h - 4),
                              win32con.DT_LEFT | win32con.DT_SINGLELINE)
            win32gui.SelectObject(hdc, old_font)
        finally:
            win32gui.EndPaint(hwnd, ps)


class UpdateWindow:
    """软件更新进度窗口：可最小化、不可关闭，带加载动画。"""

    def __init__(self):
        self._hwnd = None
        self._bar = None
        self._detail = None
        self._pct = None
        self._proc = None
        self._font_title = None
        self._font_detail = None
        self._font_pct = None
        self._pen_track = None
        self._pen_arc = None
        self._angle = 0.0

    def _register(self):
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "CQTUpdateClass"
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32gui.CreateSolidBrush(WHITE)
        self._proc = self._wnd_proc
        wc.lpfnWndProc = self._proc
        win32gui.RegisterClass(wc)

    def show(self):
        self._register()
        self._font_title = _make_font(24, 700)
        self._font_detail = _make_font(18)
        self._font_pct = _make_font(22, 700)
        self._pen_track = win32gui.CreatePen(win32con.PS_SOLID, 5, LIGHT)
        self._pen_arc = win32gui.CreatePen(win32con.PS_SOLID, 5, BLUE)
        hinst = win32api.GetModuleHandle(None)
        w, h = 520, 330
        x, y = _center_pos(w, h)
        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST,
            "CQTUpdateClass", "软件更新",
            win32con.WS_OVERLAPPED | win32con.WS_CAPTION | win32con.WS_SYSMENU |
            win32con.WS_MINIMIZEBOX,
            x, y, w, h, 0, 0, hinst, None)

        # 统一图标
        try:
            icon = _make_tray_icon(APP_DATA_DIR)
            win32gui.SendMessage(self._hwnd, win32con.WM_SETICON, win32con.ICON_SMALL, icon)
            win32gui.SendMessage(self._hwnd, win32con.WM_SETICON, win32con.ICON_BIG, icon)
        except Exception:
            pass

        # 按实际客户区宽度计算居中位置（避免边框/DPI 造成偏移）
        cw = win32gui.GetClientRect(self._hwnd)[2]
        side = 24
        ctrl_w = cw - side * 2
        bar_w = 400
        self._spin_cx = cw // 2
        self._spin_cy = 72
        self._spin_r = 28

        # 标题：大字体、水平居中
        win32gui.CreateWindowEx(
            0, "STATIC", "班级叫号器 · 软件更新",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_CENTER,
            side, 104, ctrl_w, 40, self._hwnd, 501, hinst, None)
        # 更新状态：大字体、水平居中
        self._detail = win32gui.CreateWindowEx(
            0, "STATIC", "准备更新…",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_CENTER,
            side, 148, ctrl_w, 32, self._hwnd, 503, hinst, None)
        self._bar = win32gui.CreateWindowEx(
            0, "msctls_progress32", None,
            win32con.WS_CHILD | win32con.WS_VISIBLE,
            (cw - bar_w) // 2, 192, bar_w, 22, self._hwnd, 502, hinst, None)
        self._pct = win32gui.CreateWindowEx(
            0, "STATIC", "0%",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_CENTER,
            side, 220, ctrl_w, 36, self._hwnd, 504, hinst, None)

        win32gui.SendMessage(win32gui.GetDlgItem(self._hwnd, 501),
                             win32con.WM_SETFONT, self._font_title, True)
        win32gui.SendMessage(self._detail, win32con.WM_SETFONT, self._font_detail, True)
        win32gui.SendMessage(self._pct, win32con.WM_SETFONT, self._font_pct, True)

        # 进度条范围 0-100
        win32gui.SendMessage(self._bar, 0x0406, 0, 100)   # PBM_SETRANGE32
        win32gui.SendMessage(self._bar, 0x0402, 0, 0)     # PBM_SETPOS

        # 置灰关闭按钮：可最小化，不可关闭
        try:
            hmenu = win32gui.GetSystemMenu(self._hwnd, False)
            win32gui.EnableMenuItem(hmenu, win32con.SC_CLOSE,
                                    win32con.MF_BYCOMMAND | win32con.MF_GRAYED)
        except Exception:
            pass

        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOW)
        win32gui.UpdateWindow(self._hwnd)

    def set_progress(self, pct, detail):
        pct = max(0, min(100, int(pct)))
        if self._hwnd and win32gui.IsWindow(self._hwnd):
            win32gui.SendMessage(self._bar, 0x0402, pct, 0)  # PBM_SETPOS
            win32gui.SetWindowText(self._pct, "%d%%" % pct)
            win32gui.SetWindowText(self._detail, str(detail))

    def tick(self):
        """加载动画：旋转居中的小圈。"""
        self._angle = (self._angle + 12) % 360
        if self._hwnd and win32gui.IsWindow(self._hwnd):
            win32gui.InvalidateRect(self._hwnd, (226, 38, 294, 106), True)

    def close(self):
        try:
            if self._hwnd and win32gui.IsWindow(self._hwnd):
                win32gui.DestroyWindow(self._hwnd)
        except Exception:
            pass
        self._hwnd = None
        for obj in (self._font_title, self._font_detail, self._font_pct,
                    self._pen_track, self._pen_arc):
            try:
                if obj:
                    win32gui.DeleteObject(obj)
            except Exception:
                pass
        self._font_title = self._font_detail = self._font_pct = None
        self._pen_track = self._pen_arc = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_CLOSE:
            return 0  # 不可关闭
        elif msg == win32con.WM_CTLCOLORSTATIC:
            # 文本透明背景，与窗口背景一致
            win32gui.SetTextColor(wparam, win32api.RGB(15, 23, 42))
            win32gui.SetBkMode(wparam, win32con.TRANSPARENT)
            return win32gui.GetStockObject(win32con.WHITE_BRUSH)
        elif msg == win32con.WM_PAINT:
            self._paint(hwnd)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd):
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            cx, cy, r = 260, 72, 28
            old_pen = win32gui.SelectObject(hdc, self._pen_track)
            old_brush = win32gui.SelectObject(hdc, win32gui.GetStockObject(win32con.NULL_BRUSH))
            win32gui.Ellipse(hdc, cx - r, cy - r, cx + r, cy + r)
            win32gui.SelectObject(hdc, self._pen_arc)
            a1 = math.radians(self._angle)
            a2 = math.radians(self._angle + 280)
            p1 = (int(cx + r * math.cos(a1)), int(cy + r * math.sin(a1)))
            p2 = (int(cx + r * math.cos(a2)), int(cy + r * math.sin(a2)))
            win32gui.Arc(hdc, cx - r, cy - r, cx + r, cy + r,
                         p1[0], p1[1], p2[0], p2[1])
            win32gui.SelectObject(hdc, old_brush)
            win32gui.SelectObject(hdc, old_pen)
        finally:
            win32gui.EndPaint(hwnd, ps)


class ErrorDialog:
    """启动失败弹窗：居中置顶，显示错误原因与解决办法，可复制、退出。"""

    def __init__(self, cause, solution):
        self.cause = str(cause)
        self.solution = str(solution)
        self._hwnd = None
        self._proc = None
        self._font_title = None
        self._font_body = None
        self._done = False

    def _register(self):
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "CQTErrorClass"
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32gui.CreateSolidBrush(WHITE)
        self._proc = self._wnd_proc
        wc.lpfnWndProc = self._proc
        win32gui.RegisterClass(wc)

    def run(self):
        self._register()
        self._font_title = _make_font(17, 700)
        self._font_body = _make_font(13)
        hinst = win32api.GetModuleHandle(None)
        w, h = 560, 360
        x, y = _center_pos(w, h)
        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            "CQTErrorClass", "班级叫号器 - 启动失败",
            win32con.WS_POPUP | win32con.WS_BORDER,
            x, y, w, h, 0, 0, hinst, None)
        win32gui.CreateWindowEx(
            0, "STATIC", "班级叫号器启动失败",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            24, 18, 512, 30, self._hwnd, 401, hinst, None)
        body = ("【错误原因】\r\n%s\r\n\r\n【解决办法】\r\n%s" %
                (self.cause, self.solution))
        edit = win32gui.CreateWindowEx(
            win32con.WS_EX_CLIENTEDGE, "EDIT", body,
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_VSCROLL |
            win32con.ES_MULTILINE | win32con.ES_READONLY | win32con.ES_AUTOVSCROLL,
            24, 58, 512, 224, self._hwnd, 402, hinst, None)
        win32gui.CreateWindowEx(
            0, "BUTTON", "复制全部信息",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
            24, 300, 130, 36, self._hwnd, 403, hinst, None)
        win32gui.CreateWindowEx(
            0, "BUTTON", "退出",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
            406, 300, 130, 36, self._hwnd, 404, hinst, None)
        win32gui.SendMessage(win32gui.GetDlgItem(self._hwnd, 401),
                             win32con.WM_SETFONT, self._font_title, True)
        win32gui.SendMessage(edit, win32con.WM_SETFONT, self._font_body, True)
        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOW)
        win32gui.UpdateWindow(self._hwnd)
        while not self._done:
            win32gui.PumpWaitingMessages()
            time.sleep(0.02)
        self._cleanup()

    def _copy(self):
        try:
            import win32clipboard
            text = "班级叫号器启动失败\r\n错误原因：%s\r\n解决办法：%s" % (
                self.cause, self.solution)
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            return False

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_COMMAND:
            cid = win32api.LOWORD(wparam)
            if cid == 403:
                try:
                    win32gui.MessageBox(hwnd,
                                        "已复制到剪贴板" if self._copy() else "复制失败",
                                        "复制", win32con.MB_OK)
                except Exception:
                    pass
                return 0
            if cid == 404:
                self._done = True
                return 0
        elif msg == win32con.WM_CLOSE:
            self._done = True
            return 0
        elif msg == win32con.WM_CTLCOLORSTATIC:
            if lparam == win32gui.GetDlgItem(hwnd, 401):
                win32gui.SetTextColor(wparam, RED)
                win32gui.SetBkMode(wparam, win32con.TRANSPARENT)
                return win32gui.GetStockObject(win32con.WHITE_BRUSH)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _cleanup(self):
        try:
            if self._hwnd and win32gui.IsWindow(self._hwnd):
                win32gui.DestroyWindow(self._hwnd)
        except Exception:
            pass
        self._hwnd = None
        for obj in (self._font_title, self._font_body):
            try:
                if obj:
                    win32gui.DeleteObject(obj)
            except Exception:
                pass
        self._font_title = self._font_body = None


def show_fatal_error(cause, solution):
    try:
        ErrorDialog(cause, solution).run()
    except Exception as exc:
        # 兜底：无法创建窗口时用系统消息框
        try:
            win32api.MessageBox(0, "错误原因：%s\n\n解决办法：%s" % (cause, solution),
                                "班级叫号器 - 启动失败",
                                win32con.MB_OK | win32con.MB_ICONERROR)
        except Exception:
            print("[错误] " + cause + "\n解决办法: " + solution)


class DesktopUI:
    """桌面 UI。线程安全：外部线程只能调用 queue 方法，主线程消费。"""

    def __init__(self, data_dir, port, get_records=None):
        self.data_dir = data_dir
        self.port = port
        self.get_records = get_records
        self._queue = queue.Queue()
        self._quit = False
        self._hwnd = None       # 隐藏的托盘消息窗口（不再显示主窗口）
        self._tray_added = False
        self._tray_icon = None
        self._update_win = None
        self._toast = None
        self._updating = False
        self._last_spin = 0.0
        self._popup = None
        self._popup_bmp = None
        self._remote_popup = None
        self._h_num = None
        self._h_teacher = None
        self._h_note = None
        self._h_phrase = None
        self._h_msg_text = None
        self._h_msg_teacher = None
        self._h_remote_text = None
        self._font_small = None
        self._font_num = None
        self._font_teacher = None
        self._font_note = None
        self._green_brush = None
        self._white_brush = None
        self._tray_proc = None
        self._popup_proc = None

    # ------------------------------------------------------------------
    # 外部线程调用
    # ------------------------------------------------------------------
    def notify_call(self, record):
        self._queue.put(("popup", record))

    def add_record(self, record):
        pass  # 无主窗口，叫号记录只在网页端显示

    def show_popup(self, record):
        self._queue.put(("popup", record))

    def show_message_popup(self, record):
        self._queue.put(("message_popup", record))

    def show_remote_popup(self, teacher):
        self._queue.put(("remote_on", teacher))

    def close_remote_popup(self):
        self._queue.put(("remote_off", None))

    def close_popup(self):
        self._queue.put(("close_popup", None))

    def show_update_window(self):
        self._queue.put(("update_show", None))

    def set_update_progress(self, pct, detail):
        self._queue.put(("update_progress", (pct, detail)))

    def close_update_window(self):
        self._queue.put(("update_close", None))

    def notify_update_toast(self, text):
        """后台下载更新时只弹右下角气泡（不弹大窗口）。"""
        self._queue.put(("update_toast", text))

    def _open_update_page(self):
        """打开网页版更新界面（和托盘菜单共用）。"""
        self._close_toast()
        try:
            os.startfile("http://127.0.0.1:%d/update" % self.port)
        except Exception as exc:
            print("[更新] 打开更新页面失败:", exc)

    def _open_toast(self, text):
        self._close_toast()
        try:
            self._toast = UpdateToast(text, self._open_update_page)
            self._toast.show()
        except Exception as exc:
            print("[更新提示] 显示失败:", exc)
            self._toast = None

    def _close_toast(self):
        try:
            if self._toast is not None:
                self._toast.close()
        except Exception:
            pass
        self._toast = None

    def show_panel(self):
        pass  # 无主窗口

    def clear_records(self):
        pass  # 无主窗口，叫号记录只在网页端显示

    def quit(self):
        self._queue.put(("quit", None))

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self):
        try:
            self._register_classes()
            self._create_tray()
            while not self._quit:
                win32gui.PumpWaitingMessages()
                self._drain_queue()
                # 更新窗口加载动画
                if self._update_win is not None and time.time() - self._last_spin >= 0.06:
                    self._last_spin = time.time()
                    try:
                        self._update_win.tick()
                    except Exception:
                        pass
                # 右下角气泡 10 秒自动关闭
                if self._toast is not None and self._toast.expired():
                    self._close_toast()
                time.sleep(0.02)
        finally:
            self._cleanup()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "popup":
                    self._show_popup(payload)
                elif kind == "message_popup":
                    self._show_message_popup(payload)
                elif kind == "close_popup":
                    self._close_popup()
                elif kind == "remote_on":
                    self._show_remote_popup(payload)
                elif kind == "remote_off":
                    self._close_remote_popup()
                elif kind == "update_show":
                    self._open_update_window()
                elif kind == "update_toast":
                    self._updating = True
                    self._open_toast(payload)
                elif kind == "update_progress":
                    if self._update_win is not None:
                        self._update_win.set_progress(payload[0], payload[1])
                    if self._toast is not None:
                        try:
                            self._toast.set_text("%s（%s%%）" % (payload[1], payload[0]))
                        except Exception:
                            pass
                elif kind == "update_close":
                    self._updating = False
                    self._close_toast()
                    self._close_update_window()
                elif kind == "quit":
                    self._quit_app()
        except queue.Empty:
            pass

    def _open_update_window(self):
        try:
            if self._update_win is None:
                self._update_win = UpdateWindow()
                self._update_win.show()
        except Exception as exc:
            print("[更新窗口] 显示失败:", exc)
            self._update_win = None

    def _close_update_window(self):
        try:
            if self._update_win is not None:
                self._update_win.close()
        except Exception:
            pass
        self._update_win = None

    # ------------------------------------------------------------------
    # 窗口类
    # ------------------------------------------------------------------
    def _register_classes(self):
        hinst = win32api.GetModuleHandle(None)

        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "CQTMsgClass"   # 隐藏的托盘消息窗口
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32gui.GetStockObject(win32con.WHITE_BRUSH)
        self._tray_proc = self._tray_wnd_proc
        wc.lpfnWndProc = self._tray_proc
        win32gui.RegisterClass(wc)

        wc2 = win32gui.WNDCLASS()
        wc2.hInstance = hinst
        wc2.lpszClassName = "CQTPopupClass"
        wc2.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc2.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc2.hbrBackground = win32gui.CreateSolidBrush(BG)
        self._popup_proc = self._popup_wnd_proc
        wc2.lpfnWndProc = self._popup_proc
        win32gui.RegisterClass(wc2)

        self._green_brush = win32gui.CreateSolidBrush(BG)
        self._white_brush = win32gui.CreateSolidBrush(WHITE)
        self._font_small = _make_font(15)
        self._font_num = _make_font(34, 700)
        self._font_teacher = _make_font(26, 700)
        self._font_note = _make_font(14)

    # ------------------------------------------------------------------
    # 托盘消息窗口（隐藏，不显示主窗口）
    # ------------------------------------------------------------------
    def _create_tray_window(self):
        hinst = win32api.GetModuleHandle(None)
        self._hwnd = win32gui.CreateWindowEx(
            0, "CQTMsgClass", APP_TITLE, win32con.WS_POPUP,
            0, 0, 0, 0, 0, 0, hinst, None)

    def _tray_wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == TRAY_MSG:
            # 有更新在跑时，左键点托盘直接打开更新界面；否则弹菜单
            if lparam == win32con.WM_LBUTTONUP and self._updating:
                self._open_update_page()
                return 0
            if lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONUP):
                self._show_tray_menu()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------------
    # 右下角弹窗
    # ------------------------------------------------------------------
    def _wrap_text(self, text, width=26):
        text = str(text or "")
        if len(text) <= width:
            return text
        lines = []
        while len(text) > width:
            seg = text[:width]
            cut = -1
            for p in ("，", "。", "、", "；", "：", " ", ","):
                idx = seg.rfind(p)
                if idx > width // 2:
                    cut = idx
                    break
            if cut >= 0:
                seg = text[:cut + 1]
                text = text[cut + 1:]
            else:
                text = text[width:]
            lines.append(seg)
        lines.append(text)
        return "\n".join(lines)

    def _close_popup(self):
        try:
            if self._popup and win32gui.IsWindow(self._popup):
                win32gui.DestroyWindow(self._popup)
        except Exception:
            pass
        self._popup = None
        try:
            if self._popup_bmp:
                win32gui.DeleteObject(self._popup_bmp)
        except Exception:
            pass
        self._popup_bmp = None
        self._h_num = self._h_teacher = self._h_note = None
        self._h_phrase = self._h_msg_text = self._h_msg_teacher = None

    def _load_font(self, size):
        from PIL import ImageFont
        for fp in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
                   r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"):
            if os.path.isfile(fp):
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    def _render_popup_bmp(self, rec):
        """把叫号语这一句话渲染成图片：学号/老师名称绿色大字体，其余正常。"""
        from PIL import Image, ImageDraw
        phrase = str(rec.get("phrase", ""))
        number = str(rec.get("number", ""))
        teacher = str(rec.get("teacher", ""))
        normal_font = self._load_font(20)
        big_font = self._load_font(32)
        green = (0, 150, 0)
        dark = (40, 40, 40)
        max_w = 560
        pad = 18
        line_h = 46

        # 高亮位置：优先使用叫号时记录的位置（仅占位符处，避免备注中的相同文字被放大）
        tokens = []
        for hl in (rec.get("num_hl"), rec.get("teacher_hl")):
            if isinstance(hl, (list, tuple)) and len(hl) == 2:
                try:
                    s, e = int(hl[0]), int(hl[1])
                except Exception:
                    continue
                if 0 <= s < e <= len(phrase):
                    tokens.append((s, e, phrase[s:e]))
        if not tokens:
            # 旧记录无位置信息时：只高亮第一次出现
            for val in (number, teacher):
                if val:
                    i = phrase.find(val)
                    if i >= 0:
                        tokens.append((i, i + len(val), val))
        tokens.sort()
        merged = []
        for t in tokens:
            if merged and t[0] < merged[-1][1]:
                if (t[1] - t[0]) > (merged[-1][1] - merged[-1][0]):
                    merged[-1] = t
            else:
                merged.append(t)
        segs = []
        pos = 0
        for s, e, val in merged:
            if s > pos:
                segs.append((phrase[pos:s], False))
            segs.append((val, True))
            pos = e
        if pos < len(phrase):
            segs.append((phrase[pos:], False))
        if not segs:
            segs = [(phrase, False)]

        # 排版
        tmp = Image.new("RGB", (10, 10), (255, 255, 255))
        draw = ImageDraw.Draw(tmp)
        lines = []
        cur_line = []
        cur_w = 0
        for text, big in segs:
            f = big_font if big else normal_font
            buf = ""
            for ch in text:
                w = draw.textlength(ch, font=f)
                if buf and cur_w + w > max_w:
                    cur_line.append((buf, big))
                    lines.append(cur_line)
                    cur_line = []
                    cur_w = 0
                    buf = ""
                buf += ch
                cur_w += w
            if buf:
                cur_line.append((buf, big))
        if cur_line:
            lines.append(cur_line)

        h = pad * 2 + len(lines) * line_h
        img = Image.new("RGB", (max_w + pad * 2, h), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        y = pad
        for line in lines:
            x = pad
            for text, big in line:
                f = big_font if big else normal_font
                color = green if big else dark
                draw.text((x, y), text, font=f, fill=color)
                x += draw.textlength(text, font=f)
            y += line_h
        path = os.path.join(self.data_dir, "popup.bmp")
        img.save(path, "BMP")
        return path, img.width, img.height

    def _show_popup(self, rec):
        self._close_popup()
        hinst = win32api.GetModuleHandle(None)
        try:
            path, w, h = self._render_popup_bmp(rec)
        except Exception:
            # 渲染失败时退回纯文本
            path = None
            w, h = 560, 120
        x, y = self._popup_position(w, h)

        self._popup = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            "CQTPopupClass", APP_TITLE,
            win32con.WS_POPUP | win32con.WS_BORDER,
            x, y, w, h, 0, 0, hinst, None)

        if path and os.path.isfile(path):
            try:
                hbm = win32gui.LoadImage(0, path, win32con.IMAGE_BITMAP, 0, 0,
                                         win32con.LR_LOADFROMFILE)
                if hbm:
                    self._popup_bmp = hbm
                    static = win32gui.CreateWindowEx(
                        0, "STATIC", None,
                        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_BITMAP,
                        0, 0, w, h, self._popup, ID_POP_NUM, hinst, None)
                    win32gui.SendMessage(static, win32con.STM_SETIMAGE,
                                         win32con.IMAGE_BITMAP, hbm)
            except Exception:
                pass
        else:
            self._h_phrase = win32gui.CreateWindowEx(
                0, "STATIC", str(rec.get("phrase", "")),
                win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
                12, 40, w - 24, 60, self._popup, ID_POP_NOTE, hinst, None)
            win32gui.SendMessage(self._h_phrase, win32con.WM_SETFONT, self._font_small, True)

        win32gui.CreateWindowEx(
            0, "BUTTON", "×",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
            w - 40, 6, 32, 28, self._popup, ID_POP_CLOSE, hinst, None)

        win32gui.ShowWindow(self._popup, win32con.SW_SHOW)
        win32gui.UpdateWindow(self._popup)

    def _show_message_popup(self, rec):
        self._close_popup()
        hinst = win32api.GetModuleHandle(None)
        full = str(rec.get("phrase", ""))
        teacher = str(rec.get("teacher", ""))
        text = self._wrap_text(full, 26)
        lines = text.split("\n")
        if len(lines) > 5:
            text = "\n".join(lines[:5]) + "…"

        w, h = 520, 210
        x, y = self._popup_position(w, h)
        self._popup = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            "CQTPopupClass", "文本消息",
            win32con.WS_POPUP | win32con.WS_BORDER,
            x, y, w, h, 0, 0, hinst, None)
        self._h_msg_text = win32gui.CreateWindowEx(
            0, "STATIC", text,
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            16, 14, w - 76, 128, self._popup, ID_POP_NOTE, hinst, None)
        self._h_msg_teacher = win32gui.CreateWindowEx(
            0, "STATIC", "来自：%s" % teacher,
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            16, 150, w - 76, 44, self._popup, ID_POP_TEACHER, hinst, None)
        win32gui.CreateWindowEx(
            0, "BUTTON", "×",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
            w - 52, 10, 40, 32, self._popup, ID_POP_CLOSE, hinst, None)

        win32gui.SendMessage(self._h_msg_text, win32con.WM_SETFONT, self._font_small, True)
        win32gui.SendMessage(self._h_msg_teacher, win32con.WM_SETFONT, self._font_teacher, True)
        win32gui.ShowWindow(self._popup, win32con.SW_SHOW)
        win32gui.UpdateWindow(self._popup)

    def _show_remote_popup(self, teacher):
        self._close_remote_popup()
        hinst = win32api.GetModuleHandle(None)
        teacher = str(teacher or "老师")
        w, h = 420, 140
        x, y = self._popup_position(w, h)
        self._remote_popup = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            "CQTPopupClass", "远程控制",
            win32con.WS_POPUP | win32con.WS_BORDER,
            x, y, w, h, 0, 0, hinst, None)
        self._h_remote_text = win32gui.CreateWindowEx(
            0, "STATIC", "%s正在远程控制电脑" % teacher,
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_CENTER,
            12, 32, w - 24, 56, self._remote_popup, ID_POP_TEACHER, hinst, None)
        win32gui.CreateWindowEx(
            0, "BUTTON", "×",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
            w - 52, 10, 40, 32, self._remote_popup, ID_POP_CLOSE, hinst, None)
        win32gui.SendMessage(self._h_remote_text, win32con.WM_SETFONT, self._font_teacher, True)
        win32gui.ShowWindow(self._remote_popup, win32con.SW_SHOW)
        win32gui.UpdateWindow(self._remote_popup)

    def _close_remote_popup(self):
        try:
            if self._remote_popup and win32gui.IsWindow(self._remote_popup):
                win32gui.DestroyWindow(self._remote_popup)
        except Exception:
            pass
        self._remote_popup = None
        self._h_remote_text = None

    def _popup_position(self, w, h):
        try:
            rect = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0))).get("Work") or (0, 0, 1920, 1040)
        except Exception:
            rect = (0, 0, 1920, 1040)
        return rect[2] - w - 24, rect[3] - h - 24

    def _popup_wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_COMMAND:
            cid = win32api.LOWORD(wparam)
            if cid == ID_POP_CLOSE:
                if hwnd == self._remote_popup:
                    self._close_remote_popup()
                else:
                    self._close_popup()
                return 0
        elif msg == win32con.WM_CTLCOLORSTATIC:
            hdc = wparam
            ctrl = lparam
            green = False
            if hwnd == self._remote_popup:
                green = (ctrl == self._h_remote_text)
            else:
                green = ctrl in (self._h_num, self._h_teacher, self._h_msg_teacher)
            win32gui.SetTextColor(hdc, GREEN if green else DARK_GRAY)
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            return self._green_brush
        elif msg == win32con.WM_CLOSE:
            if hwnd == self._remote_popup:
                self._close_remote_popup()
            else:
                self._close_popup()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------------
    # 托盘
    # ------------------------------------------------------------------
    def _create_tray(self):
        try:
            self._create_tray_window()
            self._tray_icon = _make_tray_icon(self.data_dir)
            nid = (self._hwnd, 0,
                   win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                   TRAY_MSG, self._tray_icon, APP_TITLE)
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
            self._tray_added = True
        except Exception as exc:
            print("[桌面] 托盘创建失败:", exc)

    def _show_tray_menu(self):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "打开网页")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 3,
                            "软件更新（查看进度）" if self._updating else "软件更新")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, None)
        win32gui.AppendMenu(menu, win32con.MF_STRING, 2, "退出")
        pos = win32gui.GetCursorPos()
        try:
            win32gui.SetForegroundWindow(self._hwnd)
        except Exception:
            pass
        cmd = win32gui.TrackPopupMenu(
            menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON |
            win32con.TPM_RETURNCMD,
            pos[0], pos[1], 0, self._hwnd, None)
        win32gui.DestroyMenu(menu)
        if cmd == 1:
            try:
                os.startfile("http://127.0.0.1:%d" % self.port)
            except Exception:
                pass
        elif cmd == 3:
            self._open_update_page()
        elif cmd == 2:
            self._quit_app()

    def _quit_app(self):
        self._quit = True
        try:
            if self._tray_added:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE,
                                          (self._hwnd, 0, 0, 0, 0, ""))
        except Exception:
            pass
        try:
            if self._tray_icon:
                win32gui.DestroyIcon(self._tray_icon)
        except Exception:
            pass
        self._tray_icon = None
        try:
            if self._hwnd and win32gui.IsWindow(self._hwnd):
                win32gui.DestroyWindow(self._hwnd)
        except Exception:
            pass

    def _cleanup(self):
        try:
            if self._tray_added:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE,
                                          (self._hwnd, 0, 0, 0, 0, ""))
        except Exception:
            pass
        try:
            if self._tray_icon:
                win32gui.DestroyIcon(self._tray_icon)
        except Exception:
            pass
        self._tray_icon = None
        self._close_toast()
        self._close_update_window()
        try:
            if self._hwnd and win32gui.IsWindow(self._hwnd):
                win32gui.DestroyWindow(self._hwnd)
        except Exception:
            pass
        for obj in (self._font_small, self._font_num, self._font_teacher,
                    self._font_note, self._green_brush, self._white_brush):
            try:
                if obj:
                    win32gui.DeleteObject(obj)
            except Exception:
                pass
