# -*- coding: utf-8 -*-
"""WebRPC：按「原站 URL + 方法」派发，让原站 JS 一行都不用改。

映射规则：
    GET  /api/xxx      → rpc("GET", "/api/xxx")
    POST /api/xxx      → rpc("POST", "/api/xxx", body)
在 DataChannel 上原样表达，PC 端再把 URL 还原成原来的 Flask 接口。
"""
import base64
import io
import json
import os
import threading

CHUNK = 30000          # 每块原始字节数（base64 后约 40KB，留足 DataChannel 余量）
BIN_CHUNK = 20000      # 二进制分块


class WebRpcServer(object):
    """把叫号器的 HTTP 接口映射为 DataChannel 上的 RPC（按原 URL 派发）。"""

    def __init__(self, app, log=None):
        self.app = app
        self.log = log or (lambda m: None)

    # ------------------------------------------------------------------ 工具
    def _ctx(self, method, path, query=None, body=None, headers=None):
        """伪造 Flask 请求上下文。

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

    def _find_view(self, path, method):
        """按路径+方法找到 Flask 视图函数。"""
        for rule in self.app.url_map.iter_rules():
            if rule.rule != path:
                continue
            methods = rule.methods or set()
            if method in methods:
                return self.app.view_functions[rule.endpoint]
            if method == "GET" and "GET" in methods:
                return self.app.view_functions[rule.endpoint]
        return None

    def _call(self, method, path, query=None, body=None, token=None, raw_body=None):
        """在伪造的请求上下文里调用原接口，返回 (status, data, resp)。"""
        headers = {}
        if token:
            headers["Cookie"] = "cqt_sid=" + token
        with self._ctx(method, path, query, body, headers):
            view = self._find_view(path, method)
            if view is None:
                return 404, {"ok": False, "error": "接口不存在：%s" % path}, None
            if raw_body is not None:
                try:
                    from flask import request
                    request.environ["wsgi.input"] = io.BytesIO(raw_body)
                except Exception:
                    pass
            resp = view()
            try:
                data = resp.get_json(silent=True)
            except Exception:
                data = None
            if data is None:
                try:
                    data = json.loads(resp.data.decode("utf-8", "replace"))
                except Exception:
                    data = {"ok": False,
                            "error": (resp.data or b"")[:200].decode("utf-8", "replace")}
            return resp.status_code, data, resp

    def _binary(self, raw, ctype):
        total = len(raw)
        chunks = []
        for off in range(0, max(total, 1), BIN_CHUNK):
            chunks.append(base64.b64encode(raw[off:off + BIN_CHUNK]).decode())
        return {"ok": True, "ctype": ctype, "total": total, "chunks": chunks}

    # ------------------------------------------------------------ 主入口
    def dispatch(self, method, params, token=""):
        """method 为原站的 HTTP 方法（GET/POST）；params 里带 path/query/body。

        也支持几个特殊方法（不走 URL）：
            auth.session / auth.logout / screen
        """
        p = params or {}
        from werkzeug.exceptions import HTTPException
        try:
            if method == "auth.session":
                return self.session_info(p.get("token") or token)
            if method == "auth.logout":
                return self.logout(p.get("token") or token)
            return self._dispatch(method, p, token)
        except HTTPException as exc:
            raise RuntimeError(exc.description or exc.name)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("%s %s: %r" % (method, p.get("path"), exc))

    def _dispatch(self, method, p, token):
        path = str(p.get("path") or "")
        if not path.startswith("/"):
            raise RuntimeError("路径不合法")
        query = p.get("query") or None
        body = p.get("body")
        raw_body = None
        files = p.get("files")

        # 上传：base64 文件内容 + 目标路径
        if files:
            raw = base64.b64decode(files.get("b64") or "")
            fname = files.get("name") or "file.bin"
            fpath = files.get("path") or fname
            with self._ctx(method, path, query, body,
                           {"Cookie": "cqt_sid=" + token} if token else None):
                view = self._find_view(path, method)
                if view is None:
                    raise RuntimeError("接口不存在")
                try:
                    from flask import request
                    from werkzeug.datastructures import FileStorage
                    storage = FileStorage(stream=io.BytesIO(raw), filename=fname)
                    request.files["file"] = storage
                    if fpath:
                        request.form["path"] = fpath
                except Exception:
                    pass
                resp = view()
                try:
                    data = resp.get_json(silent=True)
                except Exception:
                    data = None
                if data is None:
                    try:
                        data = json.loads(resp.data.decode("utf-8", "replace"))
                    except Exception:
                        data = {"ok": False, "error": "响应解析失败"}
                if resp.status_code >= 400:
                    raise RuntimeError(data.get("error") or ("请求失败 %s" % resp.status_code))
                return data

        # 二进制响应（图片/音频/文件下载）
        if p.get("binary"):
            st, data, resp = self._call(method, path, query, body, token)
            if st >= 400:
                raise RuntimeError(data.get("error") or ("请求失败 %s" % st))
            ctype = (resp.headers.get("Content-Type") or "application/octet-stream")
            return self._binary(resp.data or b"", ctype)

        st, data, resp = self._call(method, path, query, body, token)
        if st >= 400:
            raise RuntimeError(data.get("error") or ("请求失败 %s" % st))
        return data

    # ------------------------------------------------------- 登录 / 会话
    def login(self, username, password):
        import accounts
        u = str(username or "").strip()
        user, err = accounts.verify_login(u, str(password or ""))
        if not user:
            raise RuntimeError(err or "账号或密码不正确")
        sid = accounts.create_session(user["id"], ua="portal", ip="webrtc")
        return {"token": sid,
                "user": {"username": user.get("username"),
                         "display_name": user.get("display_name") or "",
                         "is_admin": bool(user.get("is_admin"))}}

    def session_info(self, token):
        import accounts
        sess, user = accounts.get_session(token or "")
        if not user:
            raise RuntimeError("登录已失效，请重新登录")
        return {"user": {"username": user.get("username"),
                         "display_name": user.get("display_name") or "",
                         "is_admin": bool(user.get("is_admin"))}}

    def logout(self, token):
        import accounts
        try:
            accounts.revoke_session(token or "")
        except Exception:
            pass
        return {"ok": True}
