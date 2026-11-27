# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
r"""用 Edge/Chrome 的调试协议（CDP）驱动浏览器，零第三方依赖。

为什么自己写：
    自动登录要"在真实页面里填表、点按钮"，用 Selenium/Playwright 都得往
    便携 python 里塞几十 MB。而 Edge 本来就装在每台 Windows 上，
    只要会讲 WebSocket 就够了——这里就手写一个最小可用的 RFC6455 客户端。

能用的能力（够自动登录用了）：
    nav(url)          打开网页
    js(expr)          在页面里执行 JS，返回 JSON 结果
    title() / url()   当前页面标题、地址
    close()           结束
"""
import base64
import json
import os
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


# ---------------------------------------------------------------------------
# 找 Edge
# ---------------------------------------------------------------------------
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _bundled_browser():
    """内置的 Chromium headless-shell（随安装包发布，不依赖系统 Edge）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "MS-browser", "chrome-headless-shell.exe"),
        os.path.join(here, "MS-browser", "chrome-headless-shell-win64", "chrome-headless-shell.exe"),
        os.path.join(here, "MS-browser", "chrome.exe"),
    ]
    base = os.path.dirname(here)
    cands += [
        os.path.join(base, "MS-browser", "chrome-headless-shell.exe"),
        os.path.join(base, "MS-browser", "chrome-headless-shell-win64", "chrome-headless-shell.exe"),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    return ""


def find_browser():
    force = os.environ.get("CC_BROWSER_EXE")
    if force and os.path.isfile(force):
        return force
    b = _bundled_browser()
    if b:
        return b
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(key, "")
        if not base:
            continue
        for sub in (r"Microsoft\Edge\Application\msedge.exe",
                    r"Google\Chrome\Application\chrome.exe"):
            p = os.path.join(base, sub)
            if os.path.isfile(p):
                return p
    return ""


def _free_port(start=9222, end=9260):
    for port in range(start, end):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return 0


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端
# ---------------------------------------------------------------------------
class _WS(object):
    def __init__(self, ws_url, timeout=30):
        m = re.match(r"ws://([^/:]+):(\d+)(/.*)$", ws_url)
        if not m:
            raise ValueError("ws 地址不合法：%s" % ws_url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("握手被断开")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise IOError("握手失败：%s" % buf.split(b"\r\n")[0])

    # -- 发送 --
    def send(self, text):
        data = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    # -- 接收一帧 --
    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("连接已关闭")
            buf += chunk
        return buf

    def recv(self):
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(ln) if ln else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise IOError("对方关闭连接")
            if opcode == 0x9:                     # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode in (0x1, 0x0, 0x2):
                return payload.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 浏览器会话
# ---------------------------------------------------------------------------
def _kill_stray_login_browsers(keep_profile=None):
    """清掉上一次没退干净的登录用浏览器（命令行里带 cqt_browser_ 的）。"""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='msedge.exe'", "get", "processid,commandline"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        if "cqt_browser_" not in line.lower():
            continue
        if keep_profile and keep_profile.lower() in line.lower():
            continue
        m = re.search(r"(\d+)\s*$", line.strip())
        if not m:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", m.group(1)],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            killed += 1
        except Exception:
            pass
    return killed


def _is_headless_shell(exe):
    """内置的 chrome-headless-shell 本身就是无头的，不能再传 --headless*，否则直接退出。"""
    return "headless-shell" in os.path.basename(str(exe)).lower()


def _is_admin():
    """当前进程是否是管理员（提权后启动的 Chromium 不会开放调试端口）。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False



def _launch_unelevated(exe, args, cwd=None):
    """用资源管理器的令牌启动进程 —— 从管理员进程里"降权"拉起浏览器。

    原理：explorer.exe 一定跑在普通用户权限下，复制它的令牌创建进程，
    子进程就不再是管理员，Chromium 也就会正常开放 DevTools 端口。
    """
    import ctypes
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)

    # 1) 找 shell 窗口 → 取它的进程（就是 explorer）
    hwnd = u32.GetShellWindow()
    if not hwnd:
        raise RuntimeError("找不到 shell 窗口")
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    hproc = k32.OpenProcess(0x0400, False, pid.value)          # PROCESS_QUERY_INFORMATION
    if not hproc:
        raise RuntimeError("打开 explorer 进程失败")
    htok = wintypes.HANDLE()
    if not adv.OpenProcessToken(hproc, 0x0002 | 0x0008 | 0x0001, ctypes.byref(htok)):
        raise RuntimeError("取 explorer 令牌失败")
    hdup = wintypes.HANDLE()
    if not adv.DuplicateTokenEx(htok, 0x02000000, None, 2, 1, ctypes.byref(hdup)):
        raise RuntimeError("复制令牌失败")

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    cmdline = " ".join(['"%s"' % exe] + [('"%s"' % a) if " " in a else a for a in args])
    buf = ctypes.create_unicode_buffer(cmdline)
    ok = adv.CreateProcessWithTokenW(hdup, 0, None, buf, 0, None,
                                     cwd, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise RuntimeError("CreateProcessWithTokenW 失败：%d" % ctypes.get_last_error())
    k32.CloseHandle(pi.hThread)
    return pi.hProcess, pi.dwProcessId


def _kill_by_profile(profile):
    """经 explorer 代理启动时拿不到进程句柄，只能按命令行里的 profile 路径找。"""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='msedge.exe'", "get", "processid,commandline"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except Exception:
        return
    for line in out.splitlines():
        if profile and profile.lower() in line.lower():
            m = re.search(r"(\d+)\s*$", line.strip())
            if m:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", m.group(1)],
                                   capture_output=True, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    pass


class Browser(object):
    def __init__(self, headless=True, log=None, timeout=30, via_explorer=None, proxy=""):
        self.log = log or (lambda m: None)
        self.exe = find_browser()
        if not self.exe:
            raise RuntimeError("这台电脑上找不到 Edge/Chrome")
        self.ws = None
        self._id = 0
        self.timeout = timeout
        self.headless = headless
        self.session_id = None
        self.proxy = (proxy or os.environ.get("CQT_BROWSER_PROXY") or "").strip()
        self.via_explorer = _is_admin() if via_explorer is None else bool(via_explorer)
        self.profile = tempfile.mkdtemp(prefix="cqt_browser_")
        self.errlog = os.path.join(self.profile, "browser.err.log")
        self._connect()

    def _spawn(self, extra):
        """按不同参数组合启动浏览器；把 stderr 落到文件里，失败时能看原因。"""
        self.port = _free_port()
        if not self.port:
            raise RuntimeError("找不到可用调试端口")
        args = [self.exe, "--remote-debugging-port=%d" % self.port,
                "--user-data-dir=%s" % self.profile,
                "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking",
                "--disable-gpu", "--remote-allow-origins=*",
                "--window-size=1280,900"] + list(extra)
        if getattr(self, "proxy", ""):
            p = self.proxy
            if "://" not in p:
                p = "http://" + p
            args.append("--proxy-server=%s" % p)
            self.log("[浏览器] 使用代理 %s" % p)
        if _is_headless_shell(self.exe) and not any(a.startswith("http") or a == "about:blank"
                                                    for a in args[1:]):
            # headless-shell 必须给个初始页面，否则不会有 page target（就没法截图/操作）
            args.append("about:blank")
        try:
            killed = _kill_stray_login_browsers(keep_profile=self.profile)
            if killed:
                self.log("[浏览器] 清掉 %d 个残留的登录用浏览器" % killed)
        except Exception:
            pass
        try:
            self.errfh = open(self.errlog, "ab")
        except Exception:
            self.errfh = subprocess.DEVNULL
        if self.via_explorer:
            # 关键：管理员进程直接 Popen 出来的 Edge 也是管理员，Chromium 会拒绝开调试端口。
            # 交给 explorer.exe 代劳，Edge 就以普通权限启动，调试端口正常开放。
            self.log("[浏览器] 管理员模式 → 用 shell 令牌降权启动")
            hproc, pid = _launch_unelevated(args[0], args[1:], cwd=os.path.expanduser("~"))
            self.proc = subprocess.Popen(["cmd", "/c", "exit"], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._browser_pid = pid
            self._browser_h = hproc
        else:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=self.errfh,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _tail_err(self, n=260):
        try:
            with open(self.errlog, "rb") as f:
                data = f.read()[-n:]
            return data.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
        except Exception:
            return ""

    def _kill(self):
        try:
            if getattr(self, "via_explorer", False):
                _kill_by_profile(self.profile)
                return
            if getattr(self, "proc", None) and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass

    def _open_page_via_browser(self):
        """没有 page target 时（内置 headless-shell 常见）：自建页面并以 sessionId 通信。"""
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/version" % self.port, timeout=2) as r:
                ver = json.loads(r.read().decode("utf-8", "replace"))
            ws_url = ver.get("webSocketDebuggerUrl") or ""
            if not ws_url:
                return False
            if getattr(self, "ws", None) is None:
                self.ws = _WS(ws_url, timeout=self.timeout)
            self.session_id = None
            res = self.call("Target.createTarget", {"url": "about:blank"}, timeout=10) or {}
            tid = res.get("targetId")
            if not tid:
                return False
            res2 = self.call("Target.attachToTarget",
                             {"targetId": tid, "flatten": True}, timeout=10) or {}
            sid = res2.get("sessionId")
            if not sid:
                return False
            self.session_id = sid
            self.log("[浏览器] 已建页面会话 %s" % sid[:12])
            return True
        except Exception as exc:
            self.log("[浏览器] 建页面会话失败：%r" % (exc,))
            return False

    def _wait_cdp(self, seconds):
        deadline = time.time() + seconds
        last = ""
        while time.time() < deadline:
            if (not getattr(self, "via_explorer", False)) and getattr(self, "proc", None) \
                    and self.proc.poll() is not None:
                return False, "浏览器进程已退出（退出码 %s）" % self.proc.returncode
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % self.port, timeout=2) as r:
                    targets = json.loads(r.read().decode("utf-8", "replace"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        self.ws = _WS(t["webSocketDebuggerUrl"], timeout=self.timeout)
                        self.session_id = None
                        return True, ""
                # 内置 headless-shell 启动时没有 page target：连浏览器级 WS 自建一个
                if not getattr(self, "session_id", None):
                    if self._open_page_via_browser():
                        return True, ""
            except Exception as exc:
                last = repr(exc)
            time.sleep(0.5)
        return False, last or "调试端口一直没响应"

    def _connect(self):
        langs = []
        if _is_headless_shell(self.exe):
            # 内置 chrome-headless-shell 本身就是无头的，再传 --headless* 会直接退出
            langs.append([])
        elif self.headless:
            langs.append(["--headless=new"])
            langs.append(["--headless"])
            langs.append([])          # 兜底：真弹一个有头窗口
        else:
            langs.append([])          # 有头优先（最兼容）
            langs.append(["--headless=new"])
            langs.append(["--headless"])
        problems = []
        for extra in langs:
            try:
                self.log("[浏览器] 启动方式 %s" % (" ".join(extra) or "普通窗口"))
                self._spawn(extra)
                ok, why = self._wait_cdp(45)
                if ok:
                    return
                tail = self._tail_err()
                problems.append("%s → %s%s" % (" ".join(extra) or "普通窗口", why,
                                               ("｜浏览器输出：" + tail) if tail else ""))
                self._kill()
            except Exception as exc:
                problems.append("%s → %r" % (" ".join(extra) or "普通窗口", exc))
        raise RuntimeError("连不上浏览器调试接口：%s" % " ； ".join(problems)[:600])

    def call(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        sid = getattr(self, "session_id", None)
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))
        end = time.time() + (timeout or self.timeout)
        while time.time() < end:
            msg = self.ws.recv()
            try:
                obj = json.loads(msg)
            except Exception:
                continue
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError("%s 出错：%s" % (method, obj["error"]))
                return obj.get("result", {})
        raise TimeoutError("%s 超时" % method)

    # -- 便捷方法 --
    def nav(self, url, wait=1.5):
        self.call("Page.navigate", {"url": url}, timeout=45)
        time.sleep(wait)
        return self

    def js(self, expression, wait=0.6):
        """执行 JS，返回值（自动 await Promise 并取 JSON）。"""
        res = self.call("Runtime.evaluate", {
            "expression": "(async () => { %s })()" % expression,
            "awaitPromise": True, "returnByValue": True,
        }, timeout=45)
        time.sleep(wait)
        if res.get("exceptionDetails"):
            raise RuntimeError("JS 异常：%s" % json.dumps(
                res["exceptionDetails"], ensure_ascii=False)[:300])
        return (res.get("result") or {}).get("value")

    def title(self):
        try:
            return self.js("return document.title;", wait=0)
        except Exception:
            return ""

    def url(self):
        try:
            return self.js("return location.href;", wait=0)
        except Exception:
            return ""

    def html(self):
        try:
            return self.js("return document.documentElement.outerHTML;", wait=0) or ""
        except Exception:
            return ""

    def shot(self, path):
        try:
            res = self.call("Page.captureScreenshot", {"format": "png"}, timeout=20)
            data = base64.b64decode(res.get("data") or "")
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception:
            return ""

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            shutil.rmtree(self.profile, ignore_errors=True)
        except Exception:
            pass
