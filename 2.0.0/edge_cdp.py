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


def find_browser():
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
class Browser(object):
    def __init__(self, headless=True, log=None, timeout=30):
        self.log = log or (lambda m: None)
        self.exe = find_browser()
        if not self.exe:
            raise RuntimeError("这台电脑上找不到 Edge/Chrome")
        self.port = _free_port()
        if not self.port:
            raise RuntimeError("找不到可用调试端口")
        self.profile = tempfile.mkdtemp(prefix="cqt_browser_")
        args = [self.exe, "--remote-debugging-port=%d" % self.port,
                "--user-data-dir=%s" % self.profile,
                "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking",
                "--window-size=1280,900"]
        if headless:
            args.insert(1, "--headless=new")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.ws = None
        self._id = 0
        self.timeout = timeout
        self._connect()

    def _connect(self):
        deadline = time.time() + 25
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % self.port, timeout=2) as r:
                    targets = json.loads(r.read().decode("utf-8", "replace"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        self.ws = _WS(t["webSocketDebuggerUrl"], timeout=self.timeout)
                        return
            except Exception as exc:
                last = repr(exc)
            time.sleep(0.4)
        raise RuntimeError("连不上浏览器调试接口：%s" % last)

    def call(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
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
