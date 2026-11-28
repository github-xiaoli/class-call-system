# -*- coding: utf-8 -*-
"""WebRPC 服务端：把叫号器的每个业务接口，在 DataChannel 上实现成 RPC 方法。

设计原则：
  * 不重复实现业务逻辑——所有接口都调用原 app.py / fsapi.py / remote.py / monitor.py
    里的同名函数，行为与网页版完全一致（校验、权限、语音播报、桌面通知等）。
  * 原函数用 Flask 的 request 取参、jsonify 返回，因此这里用 Flask 的
    test_request_context 伪造 request，让原函数原封不动地跑起来。
  * 返回体统一解析成 JSON；文件类接口（图片/音频/文件下载）单独处理，
    走分块 base64，避免超过 DataChannel 单条消息上限。

RPC 方法命名与原接口一一对应：
    call / calls / calls.clear / roster.list / roster.add / roster.delete /
    roster.clear / message / status / settings.get / settings.save / logs.tail /
    files.drives / files.list / files.mkdir / files.rename / files.copy /
    files.move / files.delete / files.download / files.upload /
    remote.start / remote.stop / remote.status / remote.screen /
    remote.screen_size / remote.input / remote.selftest /
    monitor.cameras / monitor.mics / monitor.audio / monitor.intercom /
    monitor.video
"""
import base64
import io
import json
import os
import threading

# 分块大小：DataChannel 单条消息上限约 64KB，30KB 原始数据 → 40KB base64，留足余量
CHUNK = 30000
MAX_INLINE = 30000


def _jsonify_to_dict(resp):
    """把 Flask 响应对象转成 (status, data)。"""
    try:
        data = resp.get_json(silent=True)
    except Exception:
        data = None
    if data is None:
        try:
            data = json.loads(resp.data.decode("utf-8", "replace"))
        except Exception:
            data = {"ok": False, "error": (resp.data or b"")[:200].decode("utf-8", "replace")}
    return resp.status_code, data


class WebRpcServer(object):
    """把叫号器的 HTTP 接口映射为 DataChannel 上的 RPC。"""

    def __init__(self, app, log=None):
        self.app = app
        self.log = log or (lambda m: None)
        self._local = threading.local()

    # ------------------------------------------------------------------ 工具
    def _ctx(self, method, path, query=None, body=None, headers=None):
        """构造一个 Flask 请求上下文。

        remote_addr 刻意设成公网地址：这样 check_access() 会走"必须登录"分支，
        真正校验会话，而不是走"本机直连免登录"——门户是远程访问，必须鉴权。
        """
        from urllib.parse import urlencode
        qs = ""
        if query:
            qs = urlencode(query)
        hdrs = {"Host": "127.0.0.1", "X-Forwarded-For": "203.0.113.7"}
        if headers:
            hdrs.update(headers)
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/json"
        return self.app.test_request_context(
            path + (("?" + qs) if qs else ""),
            method=method, json=body, headers=hdrs,
            environ_base={"REMOTE_ADDR": "203.0.113.7"},
        )

    def _call(self, method, path, query=None, body=None, token=None):
        """在伪造的请求上下文里调用原接口，返回 (status, data)。"""
        headers = {}
        if token:
            headers["Cookie"] = "cqt_sid=" + token
        with self._ctx(method, path, query, body, headers):
            # 供 check_access() 使用的会话
            try:
                from flask import session
                if token:
                    from login import get_session_user  # type: ignore
                session.permanent = False
            except Exception:
                pass
            # 路由到对应的视图函数
            view = self._find_view(path, method)
            if view is None:
                return 404, {"ok": False, "error": "接口不存在：%s" % path}
            resp = view()
            return _jsonify_to_dict(resp)

    def _find_view(self, path, method):
        """按路径+方法找到 Flask 视图函数。"""
        for rule in self.app.url_map.iter_rules():
            if rule.rule != path:
                continue
            methods = rule.methods or set()
            if method not in methods and "GET" not in methods:
                continue
            if "GET" in methods and method in ("GET", "HEAD"):
                pass
            elif method not in methods:
                continue
            return self.app.view_functions[rule.endpoint]
        return None

    # ------------------------------------------------------------ 分块传输
    def _binary(self, raw, ctype):
        """把二进制响应分块返回。"""
        total = len(raw)
        chunks = []
        for off in range(0, max(total, 1), CHUNK):
            part = raw[off:off + CHUNK]
            chunks.append(base64.b64encode(part).decode())
        return {"ok": True, "ctype": ctype, "total": total,
                "chunks": chunks, "chunk_size": CHUNK}

    # ------------------------------------------------------------ 主入口
    def dispatch(self, method, params, token=""):
        """执行一次 RPC，返回结果字典（出错时抛 RuntimeError）。"""
        p = params or {}
        from werkzeug.exceptions import HTTPException

        try:
            return self._dispatch(method, p, token)
        except HTTPException as exc:
            raise RuntimeError(exc.description or exc.name)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("%s: %r" % (method, exc))

    def _dispatch(self, method, p, token):
        app = self.app

        # ---------------- 登录 / 会话 ----------------
        if method == "auth.login":
            import accounts
            u = str(p.get("username") or "").strip()
            pw = str(p.get("password") or "")
            user, err = accounts.verify_login(u, pw)
            if not user:
                raise RuntimeError(err or "账号或密码不正确")
            sid = accounts.create_session(user["id"], ua="portal", ip="webrtc")
            return {"token": sid,
                    "user": {"username": user.get("username"),
                             "display_name": user.get("display_name") or "",
                             "is_admin": bool(user.get("is_admin"))}}

        if method == "auth.session":
            import accounts
            sess, user = accounts.get_session(token or "")
            if not user:
                raise RuntimeError("登录已失效，请重新登录")
            return {"user": {"username": user.get("username"),
                             "display_name": user.get("display_name") or "",
                             "is_admin": bool(user.get("is_admin"))}}

        if method == "auth.logout":
            import accounts
            try:
                accounts.revoke_session(token or "")
            except Exception:
                pass
            return {"ok": True}

        # ---------------- 叫号 ----------------
        if method == "call":
            st, data = self._call("POST", "/api/call", body={
                "number": p.get("number"), "name": p.get("name") or "",
                "note": p.get("note") or "", "teacher": p.get("teacher") or "",
                "force": bool(p.get("force"))}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or ("叫号失败(%s)" % st))
            return data

        if method == "calls":
            return self._call("GET", "/api/calls", token=token)[1]

        if method == "calls.clear":
            return self._call("POST", "/api/calls/clear", token=token)[1]

        # ---------------- 名条 ----------------
        if method == "roster.list":
            return self._call("GET", "/api/roster", token=token)[1]

        if method == "roster.add":
            st, data = self._call("POST", "/api/roster", body={
                "number": p.get("number"), "name": p.get("name") or ""}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "添加名条失败")
            return data

        if method == "roster.delete":
            st, data = self._call("POST", "/api/roster/delete", body={
                "number": p.get("number")}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "删除名条失败")
            return data

        if method == "roster.clear":
            return self._call("POST", "/api/roster/clear", token=token)[1]

        # ---------------- 播报消息 ----------------
        if method == "message":
            st, data = self._call("POST", "/api/message", body={
                "teacher": p.get("teacher") or "", "text": p.get("text") or ""}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "发送消息失败")
            return data

        # ---------------- 状态 / 设置 / 日志 ----------------
        if method == "status":
            return self._call("GET", "/api/status", token=token)[1]

        if method == "settings.get":
            return self._call("GET", "/api/settings", token=token)[1]

        if method == "settings.save":
            st, data = self._call("POST", "/api/settings", body=p.get("config") or {},
                                  token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "保存设置失败")
            return data

        if method == "logs.tail":
            q = {"limit": p.get("limit") or 400}
            if p.get("after") is not None:
                q["after"] = p["after"]
            if p.get("q"):
                q["q"] = p["q"]
            if p.get("level"):
                q["level"] = p["level"]
            return self._call("GET", "/api/logs", query=q, token=token)[1]

        # ---------------- 文件 ----------------
        if method == "files.drives":
            return self._call("GET", "/api/fs/drives", token=token)[1]

        if method == "files.list":
            q = {"path": p.get("path") or ""}
            return self._call("GET", "/api/fs/list", query=q, token=token)[1]

        if method == "files.mkdir":
            st, data = self._call("POST", "/api/fs/mkdir", body={
                "path": p.get("path") or "", "name": p.get("name") or ""}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "新建文件夹失败")
            return data

        if method == "files.rename":
            st, data = self._call("POST", "/api/fs/rename", body={
                "path": p.get("path") or "", "new_name": p.get("new_name") or ""}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "重命名失败")
            return data

        if method == "files.copy":
            st, data = self._call("POST", "/api/fs/copy", body={
                "src": p.get("src") or "", "dst": p.get("dst") or ""}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "复制失败")
            return data

        if method == "files.move":
            st, data = self._call("POST", "/api/fs/move", body={
                "src": p.get("src") or "", "dst": p.get("dst") or ""}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "移动失败")
            return data

        if method == "files.delete":
            st, data = self._call("POST", "/api/fs/delete", body={
                "paths": p.get("paths") or []}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "删除失败")
            return data

        if method == "files.download":
            # 二进制：分块 base64 返回
            with self._ctx("GET", "/api/fs/download",
                           query={"path": p.get("path") or ""},
                           headers={"Cookie": "cqt_sid=" + token} if token else None):
                view = self._find_view("/api/fs/download", "GET")
                if view is None:
                    raise RuntimeError("接口不存在")
                resp = view()
                ctype = (resp.headers.get("Content-Type") or "application/octet-stream")
                raw = resp.data or b""
            return self._binary(raw, ctype)

        if method == "files.upload":
            # 上传：base64 内容
            raw = base64.b64decode(p.get("b64") or "")
            with self._ctx("POST", "/api/fs/upload",
                           body={"path": p.get("path") or ""},
                           headers={"Cookie": "cqt_sid=" + token} if token else None):
                view = self._find_view("/api/fs/upload", "POST")
                if view is None:
                    raise RuntimeError("接口不存在")
                # 模拟 multipart 文件上传
                import io as _io
                from werkzeug.datastructures import FileStorage
                storage = FileStorage(stream=_io.BytesIO(raw),
                                      filename=os.path.basename(p.get("path") or "file.bin"))
                try:
                    from flask import request
                    request.files["file"] = storage
                except Exception:
                    pass
                resp = view()
                return _jsonify_to_dict(resp)[1]

        # ---------------- 手机远控 ----------------
        if method == "remote.start":
            st, data = self._call("POST", "/api/remote/start", body={
                "teacher": p.get("teacher") or ""}, token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "开启远控失败")
            return data

        if method == "remote.stop":
            return self._call("POST", "/api/remote/stop", token=token)[1]

        if method == "remote.status":
            return self._call("GET", "/api/remote/status", token=token)[1]

        if method == "remote.screen_size":
            return self._call("GET", "/api/remote/screen_size", token=token)[1]

        if method == "remote.screen":
            with self._ctx("GET", "/api/remote/screen",
                           headers={"Cookie": "cqt_sid=" + token} if token else None):
                view = self._find_view("/api/remote/screen", "GET")
                if view is None:
                    raise RuntimeError("接口不存在")
                resp = view()
                ctype = (resp.headers.get("Content-Type") or "image/jpeg")
                return self._binary(resp.data or b"", ctype)

        if method == "remote.input":
            st, data = self._call("POST", "/api/remote/input", body=p.get("input") or {},
                                  token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "远控指令失败")
            return data

        if method == "remote.selftest":
            return self._call("GET", "/api/remote/touch_selftest", token=token)[1]

        # ---------------- 屏幕监控 ----------------
        if method == "monitor.cameras":
            return self._call("GET", "/api/monitor/cameras", token=token)[1]

        if method == "monitor.mics":
            return self._call("GET", "/api/monitor/mics", token=token)[1]

        if method == "monitor.audio":
            with self._ctx("GET", "/api/monitor/audio",
                           query={"mic": p.get("mic") or ""},
                           headers={"Cookie": "cqt_sid=" + token} if token else None):
                view = self._find_view("/api/monitor/audio", "GET")
                if view is None:
                    raise RuntimeError("接口不存在")
                resp = view()
                ctype = (resp.headers.get("Content-Type") or "audio/wav")
                return self._binary(resp.data or b"", ctype)

        if method == "monitor.intercom":
            st, data = self._call("POST", "/api/monitor/intercom", token=token)
            if st >= 400:
                raise RuntimeError(data.get("error") or "对讲失败")
            return data

        if method == "monitor.video":
            # 摄像头视频是流，用 WebRTC 轨道，不走 RPC
            raise RuntimeError("摄像头视频请用 WebRTC 视频轨道")

        # ---------------- 健康检查 ----------------
        if method == "health":
            return self._call("GET", "/api/health", token=token)[1]

        raise RuntimeError("不支持的调用：%s" % method)
