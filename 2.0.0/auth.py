# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
密码保护模块（开源库 cryptography 仅用于校验签名）：
1) 本机访问时可创建密码（内容 + 有效时间）
2) 手机浏览器“激活”：浏览器生成 ECDSA P-256 公私钥，把公钥 + 密码发给服务器
3) 服务器校验密码后保存公钥，密码一次性失效
4) 之后敏感操作：服务器下发随机挑战 -> 浏览器签名 -> 服务器用公钥验签 -> 发 1 分钟令牌
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")  # 共享数据目录（OTA 更新不删除）
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
AUTH_PATH = os.path.join(DATA_DIR, "auth.json")

auth_bp = Blueprint("auth", __name__)

_LOCK = threading.Lock()
_SECRET_LOCK = threading.Lock()
_challenges = {}  # challenge_id -> {"device_id", "challenge", "expires"}

DEFAULT_AUTH = {"passwords": [], "devices": {}}


# ---------------------------------------------------------------------------
# 通用读写
# ---------------------------------------------------------------------------
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_auth():
    with _LOCK:
        return _load_json(AUTH_PATH, dict(DEFAULT_AUTH))


def save_auth(auth):
    with _LOCK:
        _save_json(AUTH_PATH, auth)


def ensure_auth_secret():
    """令牌签名密钥，首次运行自动生成并写入 config.json。"""
    with _SECRET_LOCK:
        cfg = _load_json(CONFIG_PATH, {})
        if not cfg.get("auth_secret"):
            cfg["auth_secret"] = secrets.token_hex(32)
            _save_json(CONFIG_PATH, cfg)
        return cfg["auth_secret"]


def _b64url_decode(s):
    s = str(s)
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _b64url_encode(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 令牌（HMAC 签名，有效期 60 秒）
# ---------------------------------------------------------------------------
def make_token(device_id):
    exp = int(time.time()) + 60
    payload = json.dumps({"d": device_id, "exp": exp},
                         separators=(",", ":")).encode("utf-8")
    sig = hmac.new(ensure_auth_secret().encode("utf-8"), payload,
                   hashlib.sha256).hexdigest()
    return _b64url_encode(payload) + "." + sig


def verify_token(token):
    try:
        if not token:
            return False
        payload_b64, sig = str(token).rsplit(".", 1)
        payload = _b64url_decode(payload_b64)
        data = json.loads(payload.decode("utf-8"))
        if int(time.time()) > int(data.get("exp", 0)):
            return False
        expect = hmac.new(ensure_auth_secret().encode("utf-8"), payload,
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return False
        # 令牌本身有效后，还要检查设备绑定是否仍有效（未被撤销、未过激活后有效期）
        auth = load_auth()
        device = auth.get("devices", {}).get(str(data.get("d", "")))
        if not device:
            return False
        if device.get("revoked"):
            return False
        if device.get("valid_until", 0) and device.get("valid_until", 0) < time.time():
            return False
        return True
    except Exception:
        return False


def device_id_from_token(token):
    try:
        payload_b64 = str(token).rsplit(".", 1)[0]
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        return str(payload.get("d", ""))
    except Exception:
        return None


def _machine_ips():
    """本机所有 IP（含回环与局域网 IP），用于判定“本机访问”。"""
    ips = {"127.0.0.1", "::1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def is_local():
    try:
        return request.remote_addr in _machine_ips()
    except Exception:
        return False


def check_access():
    """受保护接口统一鉴权：本机直接放行；远程需有效令牌。
    返回 None 表示放行，否则返回可直接 return 的 Flask 响应。"""
    if is_local():
        return None
    token = request.headers.get("X-Auth-Token") or request.args.get("token") or ""
    if token and verify_token(token):
        return None
    return jsonify({"ok": False, "error": "需要密码激活后才能使用该功能"}), 401


def _jwk_to_public_key(jwk):
    from cryptography.hazmat.primitives.asymmetric import ec
    x = _b64url_decode(jwk["x"])
    y = _b64url_decode(jwk["y"])
    return ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b"\x04" + x + y)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@auth_bp.route("/api/auth/me")
def api_auth_me():
    device_id = str(request.args.get("device_id", "")).strip()
    auth = load_auth()
    has_valid = any(
        (not p.get("used")) and (p.get("expires_at", 0) > time.time())
        for p in auth.get("passwords", []))
    return jsonify({
        "ok": True,
        "local": is_local(),
        "activated": bool(device_id and device_id in auth.get("devices", {})),
        "has_valid_password": has_valid,
    })


@auth_bp.route("/api/auth/passwords")
def api_auth_passwords():
    if not is_local():
        return jsonify({"ok": False, "error": "仅本机可管理密码"}), 403
    auth = load_auth()
    out = []
    for p in sorted(auth.get("passwords", []), key=lambda x: x.get("created_at", 0)):
        device_id = p.get("device_id")
        device = auth.get("devices", {}).get(device_id) if device_id else None
        valid_until = device.get("valid_until", 0) if device else 0
        bound = bool(device)
        now = time.time()
        out.append({
            "id": p.get("id"),
            "name": p.get("name", ""),
            "note": p.get("note", ""),
            "created_text": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(p.get("created_at", 0))),
            "activate_until": p.get("activate_until", 0),
            "activate_until_text": time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(p.get("activate_until", 0))),
            "valid_days": p.get("valid_days", 0),
            "used": bool(p.get("used")),
            "bound": bound,
            "device_id": device_id or "",
            "valid_until_text": (time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(valid_until))
                                 if bound and valid_until else ""),
            "expired": bool(p.get("activate_until", 0) <= now),
            "binding_expired": bool(bound and valid_until and valid_until <= now),
        })
    return jsonify({"ok": True, "passwords": out})


@auth_bp.route("/api/auth/create", methods=["POST"])
def api_auth_create():
    if not is_local():
        return jsonify({"ok": False, "error": "仅本机可创建密码"}), 403
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()
    name = str(data.get("name", "")).strip()
    note = str(data.get("note", "")).strip()
    try:
        activate_minutes = int(data.get("activate_minutes", 0))
    except Exception:
        activate_minutes = 0
    try:
        valid_days = int(data.get("valid_days", 0))
    except Exception:
        valid_days = 0
    if not password:
        return jsonify({"ok": False, "error": "请输入密码内容"}), 400
    if len(password) > 128:
        return jsonify({"ok": False, "error": "密码过长"}), 400
    if not (1 <= activate_minutes <= 60 * 24):
        return jsonify({"ok": False, "error": "激活时限需在 1 分钟到 24 小时之间"}), 400
    if not (1 <= valid_days <= 365):
        return jsonify({"ok": False, "error": "激活后有效期需在 1 到 365 天之间"}), 400
    auth = load_auth()
    auth.setdefault("passwords", []).append({
        "id": uuid.uuid4().hex,
        "hash": _sha256_text(password),
        "name": name,
        "note": note,
        "created_at": time.time(),
        "activate_until": time.time() + activate_minutes * 60,
        "valid_days": valid_days,
        "used": False,
        "device_id": None,
        "revoked": False,
    })
    save_auth(auth)
    return jsonify({"ok": True})


@auth_bp.route("/api/auth/delete", methods=["POST"])
def api_auth_delete():
    if not is_local():
        return jsonify({"ok": False, "error": "仅本机可管理密码"}), 403
    data = request.get_json(silent=True) or {}
    pid = str(data.get("id", "")).strip()
    auth = load_auth()
    auth["passwords"] = [p for p in auth.get("passwords", []) if p.get("id") != pid]
    save_auth(auth)
    return jsonify({"ok": True})


@auth_bp.route("/api/auth/revoke", methods=["POST"])
def api_auth_revoke():
    """撤销密码：删除密码；若已绑定设备则同时解绑（该设备立即失效）。"""
    if not is_local():
        return jsonify({"ok": False, "error": "仅本机可管理密码"}), 403
    data = request.get_json(silent=True) or {}
    pid = str(data.get("id", "")).strip()
    auth = load_auth()
    device_id = None
    for p in auth.get("passwords", []):
        if p.get("id") == pid:
            device_id = p.get("device_id")
            break
    auth["passwords"] = [p for p in auth.get("passwords", []) if p.get("id") != pid]
    if device_id:
        auth.setdefault("devices", {}).pop(device_id, None)
    save_auth(auth)
    return jsonify({"ok": True, "unbound": bool(device_id)})


@auth_bp.route("/api/auth/deactivate", methods=["POST"])
def api_auth_deactivate():
    """浏览器主动取消激活：解除本设备绑定。远程需有效令牌；本机可撤销指定设备。"""
    token = request.headers.get("X-Auth-Token") or request.args.get("token") or ""
    if is_local():
        data = request.get_json(silent=True) or {}
        device_id = str(data.get("device_id", "")).strip()
    else:
        if not token or not verify_token(token):
            return jsonify({"ok": False, "error": "需要密码激活后才能使用该功能"}), 401
        device_id = device_id_from_token(token)
    if not device_id:
        return jsonify({"ok": False, "error": "缺少设备标识"}), 400
    auth = load_auth()
    removed = auth.setdefault("devices", {}).pop(device_id, None) is not None
    for p in auth.get("passwords", []):
        if p.get("device_id") == device_id:
            p["device_id"] = None
    save_auth(auth)
    return jsonify({"ok": True, "removed": removed})


@auth_bp.route("/api/auth/activate", methods=["POST"])
def api_auth_activate():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", "")).strip()
    public_key = data.get("public_key") or {}
    if not password or not public_key.get("x") or not public_key.get("y"):
        return jsonify({"ok": False, "error": "缺少密码或公钥"}), 400
    auth = load_auth()
    now = time.time()
    target = None
    for p in auth.get("passwords", []):
        if p.get("used") or p.get("revoked"):
            continue
        if p.get("activate_until", 0) <= now:  # 超时未激活的密码不能激活
            continue
        if hmac.compare_digest(str(p.get("hash", "")), _sha256_text(password)):
            target = p
            break
    if target is None:
        return jsonify({"ok": False, "error": "密码错误、已使用、已撤销或已超时"}), 401
    try:
        _jwk_to_public_key(public_key)
    except Exception:
        return jsonify({"ok": False, "error": "公钥格式无效"}), 400
    device_id = uuid.uuid4().hex
    valid_days = max(1, int(target.get("valid_days", 30)))
    auth.setdefault("devices", {})[device_id] = {
        "jwk": public_key,
        "created_at": now,
        "valid_until": now + valid_days * 86400,
        "revoked": False,
    }
    target["used"] = True
    target["used_at"] = now
    target["device_id"] = device_id
    save_auth(auth)
    return jsonify({"ok": True, "device_id": device_id,
                    "valid_until": now + valid_days * 86400})


@auth_bp.route("/api/auth/challenge", methods=["POST"])
def api_auth_challenge():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    auth = load_auth()
    device = auth.get("devices", {}).get(device_id)
    if not device:
        return jsonify({"ok": False, "error": "设备未激活，请先激活"}), 401
    if device.get("revoked"):
        return jsonify({"ok": False, "error": "设备绑定已被撤销，请重新激活"}), 401
    if device.get("valid_until", 0) and device.get("valid_until", 0) < time.time():
        return jsonify({"ok": False, "error": "设备绑定已过期，请重新激活"}), 401
    challenge = secrets.token_bytes(32)
    challenge_id = uuid.uuid4().hex
    with _LOCK:
        now = time.time()
        for cid in list(_challenges.keys()):
            if _challenges[cid]["expires"] < now:
                _challenges.pop(cid, None)
        _challenges[challenge_id] = {
            "device_id": device_id,
            "challenge": challenge,
            "expires": now + 120,
        }
    return jsonify({"ok": True, "challenge_id": challenge_id,
                    "challenge": _b64url_encode(challenge)})


@auth_bp.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    data = request.get_json(silent=True) or {}
    challenge_id = str(data.get("challenge_id", "")).strip()
    signature_b64 = str(data.get("signature", "")).strip()
    with _LOCK:
        rec = _challenges.pop(challenge_id, None)
    if rec is None or rec.get("expires", 0) < time.time():
        return jsonify({"ok": False, "error": "挑战已过期，请重试"}), 401
    auth = load_auth()
    device = auth.get("devices", {}).get(rec["device_id"])
    if not device:
        return jsonify({"ok": False, "error": "设备不存在，请重新激活"}), 401
    try:
        sig = _b64url_decode(signature_b64)
        if len(sig) != 64:
            raise ValueError("签名长度错误")
        r = int.from_bytes(sig[:32], "big")
        s = int.from_bytes(sig[32:], "big")
        der = encode_dss_signature(r, s)
        pub = _jwk_to_public_key(device["jwk"])
        pub.verify(der, rec["challenge"], ec.ECDSA(hashes.SHA256()))
    except Exception as exc:
        return jsonify({"ok": False, "error": "签名校验失败"}), 401
    return jsonify({"ok": True, "token": make_token(rec["device_id"]),
                    "expires_in": 60})
