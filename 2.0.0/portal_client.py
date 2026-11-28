# -*- coding: utf-8 -*-
"""云端门户客户端（P2P 直连的「电脑这一端」）

职责：
  1. 采集本机 SN（BIOS SN 优先，MachineGuid 兜底，配置可覆盖）
  2. 用「叫号器总密码」把本机注册到 CF Worker（D1 存机器清单）
  3. 每 30 秒心跳，维护在线状态
  4. 连 Worker 的 WebSocket 信令房间，和老师网页交换 SDP/ICE
  5. 建好后用 aiortc 直连：DataChannel 跑业务 RPC，视频轨推屏幕画面

要点：
  * 业务数据一律走 DataChannel，不经过任何服务器
  * 视频轨在 answer 时一次性挂上（不重复协商）；不抓屏时只发黑帧，几乎不耗 CPU
  * 断线自动重连；凭据 DPAPI 加密存本机
"""
import asyncio
import base64
import ctypes
import json
import os
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid

DEFAULT_API = "https://signal2.xhzx.qzz.io"
DEFAULT_WS = "wss://signal2.xhzx.qzz.io"

_BAD_SN = ("", "to be filled by o.e.m.", "default string", "system serial number",
           "0", "none", "n/a", "not specified", "invalid", "unknown")


# --------------------------------------------------------------------------
# 1) 设备 SN
# --------------------------------------------------------------------------
def _ps(cmd, timeout=12):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _clean_sn(raw):
    s = "".join(ch for ch in str(raw or "").upper() if ch.isalnum() or ch in "-_")
    return s[:32]


def _bios_sn():
    for cmd in ("(Get-CimInstance Win32_ComputerSystemProduct).IdentifyingNumber",
                "(Get-CimInstance Win32_BIOS).SerialNumber"):
        v = _clean_sn(_ps(cmd))
        if v and v.lower() not in _BAD_SN and len(v) >= 4:
            return v
    return ""


def _machine_guid():
    v = _clean_sn(_ps(r"(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid"))
    return v if len(v) >= 8 else ""


def device_sn(cfg=None):
    """SN：配置里手动填的 > BIOS SN > MachineGuid > 随机。返回 (sn, 来源)。"""
    cfg = cfg or {}
    manual = _clean_sn(cfg.get("portal_sn_manual", ""))
    if manual:
        return manual, "manual"
    bios = _bios_sn()
    if bios:
        return bios, "bios"
    guid = _machine_guid()
    if guid:
        return guid, "machine-guid"
    return _clean_sn("CC" + uuid.uuid4().hex[:10]), "random"


# --------------------------------------------------------------------------
# 2) 凭据（DPAPI）
# --------------------------------------------------------------------------
class Vault(object):
    def __init__(self, path):
        self.path = path

    def _crypt(self, data, protect=True):
        try:
            import win32crypt
            if protect:
                return win32crypt.CryptProtectData(data, "ClassCallerPortal", None, None, None, 0)
            return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]
        except Exception:
            return None

    def save(self, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        blob = self._crypt(raw, True)
        if blob is None:
            blob = b"PLAIN:" + raw
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(blob)

    def load(self):
        try:
            with open(self.path, "rb") as f:
                blob = f.read()
        except Exception:
            return {}
        raw = blob[6:] if blob.startswith(b"PLAIN:") else (self._crypt(blob, False) or b"")
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}


# --------------------------------------------------------------------------
# 3) 极简 WebSocket 客户端（wss 支持，零额外依赖）
# --------------------------------------------------------------------------
class WSClient(object):
    def __init__(self, url, timeout=30):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    def connect(self):
        u = self.url
        secure = u.startswith("wss://")
        rest = u[6:] if secure else u[5:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or (443 if secure else 80))
        # 强制 IPv4：部分网络的 IPv6 到 CF 会被重置
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            addr = infos[0][4]
            raw = socket.create_connection(addr, timeout=self.timeout)
        except Exception:
            raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(max(45, self.timeout))
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\nOrigin: https://call.xhzx.qzz.io\r\n\r\n"
               % (path, hostport, key))
        raw.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                raise IOError("WebSocket 握手被断开")
            buf += chunk
        head, _, self._buf = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise IOError("WebSocket 握手失败：%s" % head.split(b"\r\n")[0].decode("latin1"))
        self.sock = raw

    def send_text(self, text):
        self._send(0x1, text.encode("utf-8"))

    def _send(self, opcode, payload):
        if not self.sock:
            raise IOError("未连接")
        hdr = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack("!H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack("!Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr) + masked)

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self, idle_timeout=None):
        """读一条文本；socket 超时返回 None（表示"暂时没消息"，不是断线）。"""
        try:
            return self._recv_loop()
        except socket.timeout:
            return None

    def _recv_loop(self):
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if (b2 & 0x80) else b""
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x1:
                return payload.decode("utf-8", "replace")
            if opcode == 0x8:
                raise IOError("对端关闭连接")
            if opcode == 0x9:
                try:
                    self._send(0xA, payload)
                except Exception:
                    pass

    def close(self):
        try:
            self._send(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 4) 屏幕视频轨
# --------------------------------------------------------------------------
def make_screen_track(log=print, fps=12, max_width=1280):
    """构造 aiortc 视频轨；只有 active=True 时才真抓屏。"""
    from aiortc import VideoStreamTrack
    import numpy as np
    import av

    class ScreenTrack(VideoStreamTrack):
        kind = "video"

        def __init__(self):
            super().__init__()
            self.active = False
            self.fps = max(2, min(30, int(fps)))
            self.max_width = max_width
            self._user32 = ctypes.windll.user32
            self._gdi32 = ctypes.windll.gdi32
            self._black = None

        def _size(self):
            return (self._user32.GetSystemMetrics(0), self._user32.GetSystemMetrics(1))

        def _grab(self):
            w, h = self._size()
            hdc = self._user32.GetDC(0)
            if not hdc:
                return None, 0, 0
            mem = self._gdi32.CreateCompatibleDC(hdc)
            bmp = self._gdi32.CreateCompatibleBitmap(hdc, w, h)
            self._gdi32.SelectObject(mem, bmp)

            class BIH(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                            ("biClrImportant", ctypes.c_uint32)]

            bi = BIH()
            bi.biSize = ctypes.sizeof(BIH)
            bi.biWidth = w
            bi.biHeight = -h
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            try:
                self._gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)
                self._gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
            finally:
                self._gdi32.DeleteObject(bmp)
                self._gdi32.DeleteDC(mem)
                self._user32.ReleaseDC(0, hdc)
            return buf.raw, w, h

        def _frame(self, raw, w, h):
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))[:, :, :3]
            frame = av.VideoFrame.from_ndarray(arr, format="bgr24")
            if w > self.max_width:
                # 用 av 等比缩放（比截断采样画质好、码率低）
                nh = max(2, int(round(h * self.max_width / float(w) / 2)) * 2)
                frame = frame.reformat(width=self.max_width, height=nh, format="yuv420p")
            return frame

        def black_frame(self):
            if self._black is None:
                w, h = self._size()
                scale = min(1.0, self.max_width / float(max(1, w)))
                bw, bh = max(2, int(w * scale)), max(2, int(h * scale))
                self._black = np.zeros((bh, bw, 3), dtype=np.uint8)
            return av.VideoFrame.from_ndarray(self._black, format="bgr24")

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            frame = None
            if self.active:
                try:
                    raw, w, h = self._grab()
                    if raw:
                        frame = self._frame(raw, w, h)
                except Exception as exc:
                    log("[门户] 抓屏失败：%r" % (exc,))
            if frame is None:
                frame = self.black_frame()
                await asyncio.sleep(1.0)      # 不抓屏时 1fps，省 CPU
            else:
                await asyncio.sleep(1.0 / self.fps)
            frame.pts = pts
            frame.time_base = time_base
            return frame

    return ScreenTrack()


# --------------------------------------------------------------------------
# 5) 主客户端
# --------------------------------------------------------------------------
class PortalClient(object):
    def __init__(self, api_base=DEFAULT_API, ws_base=DEFAULT_WS, vault_path=None,
                 sn="", sn_source="", name="", log=print, on_rpc=None):
        self.api = (api_base or DEFAULT_API).rstrip("/")
        self.ws_base = (ws_base or DEFAULT_WS).rstrip("/")
        self.log = log
        self.sn = sn
        self.sn_source = sn_source
        self.name = name or sn
        self.secret = ""
        self.on_rpc = on_rpc
        self.vault = Vault(vault_path or os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"), "ClassCaller", "portal.json"))
        self.ws = None
        self.peers = {}          # peerId -> {"pc","dc","track","token"}
        self._ice_cache = None
        self.pc = None           # 兼容旧字段（最近一个）
        self.dc = None
        self.track = None
        self.connected = False
        self._hb_stop = threading.Event()
        self._ws_stop = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop_thread, daemon=True, name="portal-loop").start()
        saved = self.vault.load()
        if saved.get("sn") == self.sn and saved.get("secret"):
            self.secret = saved["secret"]
        if saved.get("name") and not name:
            self.name = saved["name"]

    # ---- 事件循环 ----
    def _loop_thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- HTTP ----
    UA = "ClassCallerPortal/2.0 (+classcaller)"

    def _get(self, path, timeout=20):
        req = urllib.request.Request(self.api + path, headers={"User-Agent": self.UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _post(self, path, payload, timeout=25):
        req = urllib.request.Request(self.api + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": self.UA},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def version(self):
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.json")
            with open(p, "r", encoding="utf-8") as f:
                return str(json.load(f).get("version", ""))
        except Exception:
            return ""

    def register(self, master):
        d = self._post("/api/register", {"sn": self.sn, "name": self.name,
                                         "master": master, "version": self.version()})
        if not d.get("ok"):
            raise RuntimeError(d.get("error") or "注册失败")
        self.secret = d.get("secret", "")
        self.name = d.get("name") or self.name
        self.vault.save({"sn": self.sn, "secret": self.secret,
                         "name": self.name, "api": self.api})
        self.log("[门户] 注册成功：%s（%s）" % (self.name, self.sn))
        return d

    def heartbeat_once(self):
        if not self.secret:
            return False
        try:
            d = self._post("/api/heartbeat", {"sn": self.sn, "secret": self.secret,
                                              "version": self.version()}, timeout=15)
            return bool(d.get("ok"))
        except Exception:
            return False

    # ---- 线程 ----
    def start(self):
        if not self.secret:
            self.log("[门户] 还没注册，跳过启动")
            return False
        self._hb_stop.clear()
        self._ws_stop.clear()
        threading.Thread(target=self._hb_loop, daemon=True, name="portal-hb").start()
        threading.Thread(target=self._ws_loop, daemon=True, name="portal-ws").start()
        threading.Thread(target=self._ping_loop, daemon=True, name="portal-ping").start()
        return True

    def stop(self):
        self._hb_stop.set()
        self._ws_stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None
        self.connected = False

    def _ping_loop(self):
        """每 20 秒给信令发一个 ping：Cloudflare 会关闭长时间空闲的 WebSocket。"""
        while not self._ws_stop.is_set():
            for _ in range(20):
                if self._ws_stop.is_set():
                    return
                time.sleep(1)
            ws = self.ws
            if ws:
                try:
                    ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    pass

    def _hb_loop(self):
        while not self._hb_stop.is_set():
            self.heartbeat_once()
            for _ in range(30):
                if self._hb_stop.is_set():
                    return
                time.sleep(1)

    def _ws_loop(self):
        backoff = 3
        while not self._ws_stop.is_set():
            try:
                url = "%s/ws?sn=%s&role=pc" % (self.ws_base, urllib.parse.quote(self.sn))
                self.ws = WSClient(url)
                self.ws.connect()
                self.log("[门户] 信令已连接")
                backoff = 3
                while not self._ws_stop.is_set():
                    self._on_signal(self.ws.recv_text())
            except Exception as exc:
                if not self._ws_stop.is_set():
                    self.log("[门户] 信令断开：%r" % (exc,))
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                self.ws = None
                self.connected = False
                for _ in range(backoff):
                    if self._ws_stop.is_set():
                        return
                    time.sleep(1)
                backoff = min(30, backoff * 2)

    # ---- 信令 ----
    def _on_signal(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        t = msg.get("type")
        if t == "offer":
            self._submit(self._handle_offer(msg.get("sdp") or {}, msg.get("fromId") or ""))
        elif t == "ice" and msg.get("candidate"):
            self._submit(self._add_ice(msg["candidate"], msg.get("fromId") or ""))
        elif t == "bye":
            self._submit(self._close_peer())
        elif t == "peer-left":
            self.connected = False

    async def _add_ice(self, cand, peer_id=""):
        try:
            from aiortc import RTCIceCandidate
            peer = self.peers.get(peer_id) or self.peers.get("")
            pc = (peer or {}).get("pc") or self.pc
            if pc:
                await pc.addIceCandidate(RTCIceCandidate(
                    sdpMid=cand.get("sdpMid") or "0",
                    sdpMLineIndex=int(cand.get("sdpMLineIndex") or 0),
                    candidate=cand.get("candidate") or ""))
        except Exception:
            pass

    def _ice_list(self):
        """优先用 Worker 下发的 ICE（含 TURN 中继），拿不到就用内置 STUN。"""
        if self._ice_cache is not None:
            return self._ice_cache
        servers = []
        try:
            url = self.api.rstrip("/") + "/ice"
            req = urllib.request.Request(url, headers={"User-Agent": self.UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            for item in data.get("iceServers") or []:
                urls = item.get("urls") or []
                if isinstance(urls, str):
                    urls = [urls]
                if not urls:
                    continue
                kw = {"urls": urls}
                if item.get("username"):
                    kw["username"] = item["username"]
                    kw["credential"] = item.get("credential") or ""
                servers.append(kw)
        except Exception as exc:
            self.log("[门户] 取 ICE 失败，用内置 STUN：%r" % (exc,))
        if not servers:
            servers = [
                {"urls": ["stun:stun.miwifi.com:3478", "stun:stun.qq.com:3478"]},
                {"urls": ["stun:stun.l.google.com:19302"]},
            ]
        self._ice_cache = servers
        return servers

    async def _handle_offer(self, sdp, peer_id=""):
        try:
            from aiortc import (RTCPeerConnection, RTCSessionDescription, RTCConfiguration,
                                RTCIceServer)
        except Exception as exc:
            self.log("[门户] 缺少 WebRTC 运行库，无法直连：%r（请安装完整版）" % (exc,))
            try:
                if self.ws:
                    self.ws.send_text(json.dumps({"type": "bye", "peerId": peer_id,
                                                  "reason": "no-webrtc"}))
            except Exception:
                pass
            return
        try:
            peer_id = peer_id or ("peer%d" % (len(self.peers) + 1))
            old = self.peers.pop(peer_id, None)
            if old and old.get("pc"):
                try:
                    await old["pc"].close()
                except Exception:
                    pass
            ice_objs = []
            for it in self._ice_list():
                if it.get("username"):
                    ice_objs.append(RTCIceServer(urls=it["urls"], username=it["username"],
                                                 credential=it.get("credential") or ""))
                else:
                    ice_objs.append(RTCIceServer(urls=it["urls"]))
            cfg = RTCConfiguration(iceServers=ice_objs)
            pc = RTCPeerConnection(cfg)
            entry = {"pc": pc, "dc": None, "track": None, "token": ""}
            self.peers[peer_id] = entry
            self.pc = pc

            @pc.on("datachannel")
            def on_dc(channel):
                entry["dc"] = channel
                self.dc = channel

                def _msg(raw, _e=entry):
                    self._on_rpc(raw, _e)
                channel.on("message")(_msg)

            @pc.on("icecandidate")
            def on_ice(candidate):
                if candidate and self.ws:
                    try:
                        self.ws.send_text(json.dumps({
                            "type": "ice", "peerId": peer_id,
                            "candidate": {"candidate": candidate.candidate,
                                          "sdpMid": candidate.sdpMid,
                                          "sdpMLineIndex": candidate.sdpMLineIndex}}))
                    except Exception:
                        pass

            @pc.on("connectionstatechange")
            async def on_state():
                self.connected = any((p.get("pc") and p["pc"].connectionState == "connected")
                                     for p in self.peers.values())
                self.log("[门户] 连接状态（%s）：%s" % (peer_id, pc.connectionState))
                if pc.connectionState in ("failed", "closed"):
                    self.peers.pop(peer_id, None)

            # 视频轨一次性挂上：不抓屏时只发黑帧，不需要二次协商
            has_video = "m=video" in str(sdp.get("sdp", ""))
            if has_video:
                try:
                    entry["track"] = make_screen_track(self.log)   # 每个老师一条独立轨道
                    pc.addTrack(entry["track"])
                except Exception as exc:
                    self.log("[门户] 创建视频轨失败：%r" % (exc,))

            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp.get("sdp", ""),
                                                                type="offer"))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            if self.ws:
                self.ws.send_text(json.dumps({
                    "type": "answer", "peerId": peer_id,
                    "sdp": {"type": "answer", "sdp": pc.localDescription.sdp}}))
            self.log("[门户] 已回 answer（%s，共 %d 位老师）" % (peer_id, len(self.peers)))
        except Exception as exc:
            self.log("[门户] 处理 offer 失败：%r" % (exc,))

    async def _close_peer(self, peer_id=""):
        if peer_id:
            entry = self.peers.pop(peer_id, None)
            if entry and entry.get("pc"):
                try:
                    await entry["pc"].close()
                except Exception:
                    pass
        else:
            for entry in list(self.peers.values()):
                try:
                    await entry["pc"].close()
                except Exception:
                    pass
            self.peers.clear()
        self.pc = None
        self.dc = None
        self.connected = bool(self.peers)

    def set_screen(self, on):
        n = 0
        for entry in self.peers.values():
            if entry.get("track") is not None:
                entry["track"].active = bool(on)
                n += 1
        return bool(on)

    # ---- 业务 RPC ----
    def _on_rpc(self, raw, entry=None):
        try:
            msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
        except Exception:
            return
        rid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        token = msg.get("token") or (entry or {}).get("token") or ""
        try:
            if method == "auth.login" and entry is not None:
                # 记住每位老师自己的会话令牌
                pass
            if method == "ping":
                result = {"pong": time.time()}
            elif method == "screen":
                result = {"on": self.set_screen(bool(params.get("on")))}
            elif self.on_rpc:
                result = self.on_rpc(method, params, token)
            else:
                raise RuntimeError("没有业务处理器")
            reply = {"id": rid, "ok": True, "result": result}
        except Exception as exc:
            reply = {"id": rid, "ok": False, "error": str(exc)[:200]}
        try:
            dc = (entry or {}).get("dc") or self.dc
            if dc:
                dc.send(json.dumps(reply, ensure_ascii=False))
        except Exception:
            pass
