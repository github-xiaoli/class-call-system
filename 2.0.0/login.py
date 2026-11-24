# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
统一登录（公网化改造 · 第 2 步）

用户名 + 密码 → 会话 Cookie；会话存在 data/app.db。

与旧机制（一次性密码 / 二维码）**并存**，本步只新增、不替换：
第 3 步才会把 check_access() 切到"会话 or 设备令牌"，并删掉旧入口。
这样每一步都能单独跑起来验证，出问题也退得干净。

约定：
* Cookie 名 cqt_sid，HttpOnly + SameSite=Strict；HTTPS 下自动加 Secure
* 登录失败不区分"用户不存在 / 密码错"，且按 IP 限速
* must_change_pwd 的用户只能访问改密与登出，其余一律 428
* OYCOMO 为固定重置口令（按需求：不额外做安全限制，但记审计）
"""
import functools
import json
import os

from flask import Blueprint, g, jsonify, request

import accounts
import security
from accounts import ROLE_ADMIN

login_bp = Blueprint("login", __name__)

SESSION_COOKIE = "cqt_sid"

# ---------------------------------------------------------------------------
# 密码强度（唯一的公网凭据，必须比"4 位"硬）
# ---------------------------------------------------------------------------
PW_MIN_LEN = 8
PW_WEAK = {"12345678", "123456789", "1234567890", "password", "passw0rd",
           "abc12345", "qwertyui", "11111111", "00000000", "88888888",
           "admin123", "iloveyou", "woaini1314", "a1234567"}


def validate_password(pwd, username=""):
    """返回 (ok, 错误说明)。公网只靠这一个密码，所以要求至少 8 位且非常见弱口令。"""
    pwd = str(pwd or "")
    if len(pwd) < PW_MIN_LEN:
        return False, "密码至少 %d 位" % PW_MIN_LEN
    if len(pwd) > 128:
        return False, "密码过长（最多 128 位）"
    low = pwd.lower()
    if low in PW_WEAK:
        return False, "这个密码太常见了，换一个"
    if username and low == str(username).strip().lower():
        return False, "密码不能和用户名相同"
    if low in ("1234567890123456", str(username).lower() + "123", str(username).lower() + "123456"):
        return False, "这个密码太常见了，换一个"
    return True, ""

# ---------------------------------------------------------------------------
# 本机免登录开关
#
# 默认开启（教室电脑打开即用）。一旦把服务暴露到公网（隧道/端口映射/反代），
# 必须关掉：像 frp 的 TCP 型、花生壳、natapp 这类**不写 X-Forwarded-For** 的
# 隧道，会让服务端看到 remote_addr=127.0.0.1 且没有代理头，从而把公网请求
# 判成本机、自动登录管理员——整套登录就白做了。
#
# 读 data/config.json 的 local_auto_login；文件里没有这个键时按 True 处理，
# 这样老配置不受影响。这里单独读文件而不是 import app，避免循环导入。
# ---------------------------------------------------------------------------
_CONFIG_PATH = os.path.join(accounts.DATA_DIR, "config.json")
_CACHE = {"value": None, "mtime": 0.0}


def local_auto_login_enabled():
    """本机免登录是否生效。文件改动后自动重新读取（按 mtime 判断）。"""
    try:
        st = os.stat(_CONFIG_PATH)
    except OSError:
        return True                     # 没有配置文件：按默认（开启）
    try:
        if _CACHE["value"] is not None and _CACHE["mtime"] == st.st_mtime:
            return _CACHE["value"]
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        value = cfg.get("local_auto_login", True)
        value = True if value is None else bool(value)
        _CACHE["value"] = value
        _CACHE["mtime"] = st.st_mtime
        return value
    except Exception:
        return True

# 登录失败限速：同 IP 10 分钟内失败 8 次，锁 15 分钟
LOGIN_LIMITER = security.AttemptLimiter(max_failures=8, window=600, lockout=900)
# 重置口令限速：比登录宽松一点，但也要挡扫描器
RESET_LIMITER = security.AttemptLimiter(max_failures=10, window=600, lockout=900)

# must_change_pwd 状态下仍允许的路径
_CHANGE_ALLOWED = {
    "/api/auth/session",
    "/api/auth/change-pwd",
    "/api/auth/logout",
    "/api/health",
}
# 登录/重置等匿名接口，不该被 must_change 拦
_ANON_PREFIXES = ("/api/auth/login", "/api/auth/reset", "/static/", "/login")


# ---------------------------------------------------------------------------
# 当前用户
# ---------------------------------------------------------------------------
def current_user():
    """取当前登录用户。无效/过期/被封禁都返回 None。

    本机直连（127.0.0.1 + 无代理头）且当前没有会话时，自动以管理员身份登录，
    这就是"本机免登录"的口子。之所以安全，是因为：
      * 只有能访问 127.0.0.1 的进程才能走到这里（等价于物理接触本机）
      * 带任何代理头的请求一律不算本机（堵掉同机反代）
      * 配置项 local_auto_login 可以整体关掉（上公网/开隧道时必须关）

    ⚠️ 注意：像 frp TCP 型、花生壳、natapp 这类隧道**不写 X-Forwarded-For**，
    转发后 remote_addr 就是 127.0.0.1 且没有代理头，会被判成本机。
    所以隧道场景必须设置 local_auto_login=false。
    """
    if "user" in g:
        return g.user
    sid = request.cookies.get(SESSION_COOKIE, "")
    sess, user = accounts.get_session(sid)
    if sess:
        accounts.touch_session(sid)
    g.user = user
    g.sid = sid if sess else ""

    # 公网口（隧道专用端口）一律不走本机免登录：
    # 隧道把请求从回环转进来，靠 remote_addr 判断必然误判成本机。
    try:
        if security.is_public_request():
            g.local_admin = False
            return user
    except Exception:
        pass

    if user is None and local_auto_login_enabled() and _is_local_direct():
        admin = accounts.first_admin()
        if admin and admin["status"] == accounts.STATUS_ACTIVE:
            g.user = admin
            g.local_admin = True
            return admin
    return user


def is_admin():
    user = current_user()
    return bool(user and user.get("is_admin"))


def login_required(fn):
    """需要登录的接口。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return jsonify({"ok": False, "need_login": True,
                            "error": "请先登录"}), 401
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify({"ok": False, "need_login": True,
                            "error": "请先登录"}), 401
        if not user.get("is_admin"):
            return jsonify({"ok": False, "error": "需要管理员权限"}), 403
        return fn(*args, **kwargs)
    return wrapper


def public_user(user):
    """对外的用户信息，不含任何凭据。"""
    if not user:
        return None
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": user["role"],
        "is_admin": bool(user.get("is_admin")),
        "must_change_pwd": bool(user.get("must_change_pwd")),
    }


def _client_ip():
    """真实客户端 IP（受信代理会解析 X-Forwarded-For，否则用 TCP 对端）。

    限速和审计都用它：挂上 Cloudflare/nginx 之后，如果仍用 remote_addr，
    所有请求都会记成 127.0.0.1，一个人爆破就把所有人一起锁死。
    """
    ip = security.client_ip()
    return ip or (request.remote_addr or "")


def _ua():
    return (request.headers.get("User-Agent") or "")[:300]


def _set_cookie(resp, sid):
    resp.set_cookie(
        SESSION_COOKIE, sid,
        max_age=accounts.SESSION_TTL,
        httponly=True,
        samesite="Strict",
        secure=security.is_secure_request(),   # HTTP 本机访问时不加 Secure
        path="/",
    )


def _clear_cookie(resp):
    resp.delete_cookie(SESSION_COOKIE, path="/")


# ---------------------------------------------------------------------------
# 强制改密闸门
# ---------------------------------------------------------------------------
@login_bp.before_app_request
def _force_password_change():
    path = request.path or "/"
    if any(path.startswith(p) for p in _ANON_PREFIXES):
        return None
    if path in _CHANGE_ALLOWED:
        return None
    # 本机（坐在电脑前的人）不强制改密：只有远程访问才需要这道闸门
    try:
        if _is_local_direct() and not security.is_public_request():
            return None
    except Exception:
        pass
    user = current_user()
    if user and user.get("must_change_pwd"):
        if path.startswith("/api/"):
            return jsonify({"ok": False, "need_change_pwd": True,
                            "error": "请先修改初始密码"}), 428
        from flask import redirect
        return redirect("/login?change=1")
    return None


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
@login_bp.route("/api/auth/session")
def api_session():
    """当前登录状态。未登录也返回 200，前端据此决定是否跳登录页。"""
    user = current_user()
    try:
        need_bootstrap = accounts.count_users() == 0
    except Exception:
        need_bootstrap = False
    return jsonify({
        "ok": True,
        "logged_in": user is not None,
        "user": public_user(user),
        "local": _is_local_direct(),
        "local_admin": bool(g.get("local_admin")),
        "need_bootstrap": need_bootstrap,
    })


@login_bp.route("/api/auth/bootstrap", methods=["POST"])
def api_bootstrap():
    """首次运行：创建第一个管理员账号（**只允许本机**，且只能建一次）。

    密码由用户自己设，绝不在代码里内置默认账号。
    建完之后必须重新登录（must_change_pwd=1，首次登录再改一次密）。
    """
    try:
        if security.is_public_request() or not _is_local_direct():
            return jsonify({"ok": False, "error": "只能在电脑本机上创建初始账号"}), 403
    except Exception:
        return jsonify({"ok": False, "error": "只能在电脑本机上创建初始账号"}), 403
    if accounts.count_users() > 0:
        return jsonify({"ok": False, "error": "已经有账号了，请直接登录"}), 400

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "") or "admin").strip()
    password = str(data.get("password", ""))
    display = str(data.get("display_name", "") or "管理员").strip()

    ok_pw, perr = validate_password(password, username)
    if not ok_pw:
        return jsonify({"ok": False, "error": perr}), 400
    user, err = accounts.ensure_admin(username, password, display)
    if user is None:
        return jsonify({"ok": False, "error": err or "创建失败"}), 400
    accounts.audit("admin_bootstrap", username=user["username"],
                   target=user["username"], ip=_client_ip(),
                   detail="首次运行创建管理员")
    sid = accounts.create_session(user["id"], ua=_ua(), ip=_client_ip())
    resp = jsonify({"ok": True, "user": public_user(user),
                    "needs_change": True})
    _set_cookie(resp, sid)
    return resp


@login_bp.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    ip = _client_ip()

    wait = LOGIN_LIMITER.retry_after(ip)
    if wait > 0:
        return jsonify({"ok": False,
                        "error": "尝试次数过多，请 %d 秒后再试" % wait}), 429

    if not username or not password:
        return jsonify({"ok": False, "error": "请输入用户名和密码"}), 400

    user, err = accounts.verify_login(username, password)
    if err:
        LOGIN_LIMITER.fail(ip)
        accounts.audit("login_fail", username=username, ip=ip, detail=err)
        return jsonify({"ok": False, "error": err}), 401

    LOGIN_LIMITER.reset(ip)
    sid = accounts.create_session(user["id"], ua=_ua(), ip=ip)
    accounts.audit("login_ok", user_id=user["id"], username=user["username"], ip=ip)

    resp = jsonify({
        "ok": True,
        "user": public_user(user),
        "needs_change": bool(user.get("must_change_pwd")),
    })
    _set_cookie(resp, sid)
    return resp


@login_bp.route("/api/auth/logout", methods=["POST"])
def api_logout():
    sid = request.cookies.get(SESSION_COOKIE, "")
    user = current_user()
    if sid:
        accounts.revoke_session(sid)
    if user:
        accounts.audit("logout", user_id=user["id"], username=user["username"],
                       ip=_client_ip())
    resp = jsonify({"ok": True})
    _clear_cookie(resp)
    return resp


@login_bp.route("/api/auth/change-pwd", methods=["POST"])
@login_required
def api_change_pwd():
    """改密：要求验证原密码；成功后当前会话保留，其他会话踢掉。"""
    user = current_user()
    data = request.get_json(silent=True) or {}
    old_pwd = str(data.get("old_password", ""))
    new_pwd = str(data.get("new_password", ""))

    checked, err = accounts.verify_login(user["username"], old_pwd)
    if err:
        accounts.audit("change_pwd_fail", user_id=user["id"],
                       username=user["username"], ip=_client_ip())
        return jsonify({"ok": False, "error": "原密码不正确"}), 401

    ok_pw, perr = validate_password(new_pwd, user["username"])
    if not ok_pw:
        return jsonify({"ok": False, "error": perr}), 400
    if new_pwd == old_pwd:
        return jsonify({"ok": False, "error": "新密码不能与原密码相同"}), 400

    ok, err2 = accounts.set_password(user["id"], new_pwd, must_change=False)
    if not ok:
        return jsonify({"ok": False, "error": err2 or "修改失败"}), 400

    # 其他设备下线，当前会话保留
    accounts.revoke_user_sessions(user["id"], keep_sid=request.cookies.get(SESSION_COOKIE))
    accounts.audit("change_pwd", user_id=user["id"], username=user["username"],
                   ip=_client_ip())
    g.pop("user", None)
    return jsonify({"ok": True, "user": public_user(accounts.get_user_by_id(user["id"]))})


@login_bp.route("/api/auth/reset", methods=["POST"])
def api_reset():
    """忘记密码：**只允许在电脑本机上执行**。

    公网口（隧道专用 9444）一律拒绝——公网侧不留任何重置入口，
    这样即使网址泄露、密码被猜，也无法通过"重置"绕过去。
    本机直连等价于物理接触这台电脑，因此不再额外要求口令。
    """
    ip = _client_ip()
    try:
        from_public = security.is_public_request()
    except Exception:
        from_public = True
    if from_public or not _is_local_direct():
        accounts.audit("reset_denied", ip=ip, detail="非本机尝试重置密码")
        return jsonify({"ok": False,
                        "error": "只能在电脑本机上重置密码（公网访问不提供此功能）"}), 403

    wait = RESET_LIMITER.retry_after(ip)
    if wait > 0:
        return jsonify({"ok": False,
                        "error": "尝试次数过多，请 %d 秒后再试" % wait}), 429

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    new_pwd = str(data.get("new_password", ""))

    user = accounts.get_user(username)
    if user is None:
        RESET_LIMITER.fail(ip)
        accounts.audit("reset_fail", username=username, ip=ip, detail="账号不存在")
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    ok, perr = validate_password(new_pwd, user["username"])
    if not ok:
        return jsonify({"ok": False, "error": perr}), 400

    RESET_LIMITER.reset(ip)
    accounts.set_password(user["id"], new_pwd, must_change=True)
    accounts.revoke_user_sessions(user["id"])
    accounts.audit("reset_ok", user_id=user["id"], username=user["username"],
                   ip=ip, detail="本机重置密码，已强制改密")
    return jsonify({"ok": True, "error": "",
                    "message": "密码已重置，请用新密码登录并立即修改"})


# ---------------------------------------------------------------------------
# 管理员后台接口
# ---------------------------------------------------------------------------
@login_bp.route("/api/admin/users")
@admin_required
def api_admin_users():
    return jsonify({"ok": True, "users": accounts.list_users()})


@login_bp.route("/api/admin/users", methods=["POST"])
@admin_required
def api_admin_create_user():
    me = current_user()
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    role = str(data.get("role", accounts.ROLE_TEACHER))
    display = str(data.get("display_name", "") or "")

    ok_pw, perr = validate_password(password, username)
    if not ok_pw:
        return jsonify({"ok": False, "error": perr}), 400
    user, err = accounts.create_user(username, password, role=role,
                                     display_name=display, must_change=True)
    if user is None:
        return jsonify({"ok": False, "error": err}), 400
    accounts.audit("user_create", user_id=me["id"], username=me["username"],
                   target=user["username"], ip=_client_ip(),
                   detail="role=%s" % user["role"])
    return jsonify({"ok": True, "user": user})


@login_bp.route("/api/admin/users/action", methods=["POST"])
@admin_required
def api_admin_user_action():
    """封禁 / 解封 / 重置密码 / 删除 / 踢下线。"""
    me = current_user()
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", ""))
    username = str(data.get("username", "")).strip()
    target = accounts.get_user(username)

    if target is None:
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    if target["id"] == me["id"] and action in ("ban", "delete"):
        return jsonify({"ok": False, "error": "不能对自己执行该操作"}), 400

    if action == "ban":
        accounts.set_status(target["id"], accounts.STATUS_BANNED)
        detail = "已封禁，会话与设备已失效"
    elif action == "unban":
        accounts.set_status(target["id"], accounts.STATUS_ACTIVE)
        detail = "已解封"
    elif action == "kick":
        accounts.revoke_user_sessions(target["id"])
        detail = "已踢下线"
    elif action == "reset":
        new_pwd = str(data.get("password", "") or "").strip()
        ok_pw, perr = validate_password(new_pwd, target["username"])
        if not ok_pw:
            return jsonify({"ok": False, "error": perr}), 400
        ok, err = accounts.set_password(target["id"], new_pwd, must_change=True)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        accounts.revoke_user_sessions(target["id"])
        detail = "密码已重置，需重新登录并改密"
    elif action == "delete":
        ok, err = accounts.delete_user(target["id"])
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        detail = "已删除账号"
    else:
        return jsonify({"ok": False, "error": "未知操作"}), 400

    accounts.audit("user_" + action, user_id=me["id"], username=me["username"],
                   target=target["username"], ip=_client_ip(), detail=detail)
    return jsonify({"ok": True, "message": detail})


@login_bp.route("/api/admin/sessions")
@admin_required
def api_admin_sessions():
    return jsonify({"ok": True, "sessions": accounts.list_sessions(),
                    "devices": accounts.list_devices()})


@login_bp.route("/api/admin/sessions/revoke", methods=["POST"])
@admin_required
def api_admin_revoke_session():
    me = current_user()
    data = request.get_json(silent=True) or {}
    sid = str(data.get("sid", "")).strip()
    did = str(data.get("device_id", "")).strip()
    if sid:
        accounts.revoke_session(sid)
        accounts.audit("session_revoke", user_id=me["id"], username=me["username"],
                       target=sid[:12], ip=_client_ip())
        return jsonify({"ok": True, "message": "会话已失效"})
    if did:
        accounts.revoke_device(did)
        accounts.audit("device_revoke", user_id=me["id"], username=me["username"],
                       target=did[:12], ip=_client_ip())
        return jsonify({"ok": True, "message": "设备已解绑"})
    return jsonify({"ok": False, "error": "缺少参数"}), 400


@login_bp.route("/api/admin/audit")
@admin_required
def api_admin_audit():
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
        offset = max(0, int(request.args.get("offset", 0)))
    except Exception:
        limit, offset = 100, 0
    action = request.args.get("action") or None
    return jsonify({"ok": True,
                    "logs": accounts.list_audit(limit=limit, offset=offset,
                                                action=action),
                    "total": accounts.count_audit(action)})


# ---------------------------------------------------------------------------
# 本机直连判定（第 3 步会用到；这里先用于 session 接口）
# ---------------------------------------------------------------------------
def _is_local_direct():
    """是否为"未经代理的回环直连"。

    与旧 is_local() 的区别：旧版只看 remote_addr 是否属于本机 IP，
    一旦反代/穿透，remote_addr 变成 127.0.0.1 就会把公网请求误判成本机。
    这里额外要求"没有代理头"，代理转发必然带 X-Forwarded-For。
    """
    try:
        addr = request.remote_addr or ""
        if addr not in ("127.0.0.1", "::1"):
            return False
        if request.headers.get("X-Forwarded-For"):
            return False
        if request.headers.get("X-Real-IP"):
            return False
        if request.headers.get("Forwarded"):
            return False
        return True
    except Exception:
        return False