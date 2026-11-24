# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
r"""教室端凭据：定时拉取 + 本机解密 + 交给隧道。

背景（为什么要这么绕）：
1. 微软 Dev Tunnels 的 host 令牌寿命只有 **24 小时**（实测 lifeTime 1.00:00:03），
   贴死在配置里第二天就失效；所以必须有人每天重签一次。
2. 教室电脑又不能都去登录账号（8 个班会变成 8 次登录、还会互相挤掉）。
   于是：发布机登录一次、每天签一次、加密推到公开仓库；教室机每天拉一次。

安全模型：
  推上去的是 AES-256-GCM 密文，密钥由「激活码」PBKDF2 派生（26 万次迭代）。
  激活码只在发布工具里显示，存在本机 data/config.json，不进仓库、不进日志。
  就算仓库是公开的，别人拿到文件也只是一串乱码。

失败处理（不打扰老师）：
  拉了用不上就继续用旧令牌（旧令牌通常还能撑几个小时），
  连续失败只记日志并在设置页显示红字；网络恢复后自动补拉。
"""
import calendar
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import security

CFG_REL = "creds/slot-%s.json"

_STATE = {
    "state": "idle",        # idle | fetching | ok | error
    "message": "",
    "slot": "",
    "tunnel_id": "",
    "port": 0,
    "url": "",
    "expires": "",
    "expires_ts": 0,
    "fetched_at": 0,
    "next_at": 0,
    "fails": 0,
    "source": "",
}
_LOCK = threading.Lock()
_TOKEN = {"token": "", "tunnel_id": "", "port": 0, "expires_ts": 0, "url": ""}
_THREAD = None
_STOP = threading.Event()
_ON_UPDATE = None
_CFG_GETTER = None
_LAST_TOKEN = ""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _cfg():
    try:
        return _CFG_GETTER() if _CFG_GETTER else {}
    except Exception:
        return {}


def code_of(cfg=None):
    cfg = cfg if cfg is not None else _cfg()
    return str(cfg.get("creds_code", "") or "").strip()


def slot_of(cfg=None):
    cfg = cfg if cfg is not None else _cfg()
    return str(cfg.get("creds_slot", "") or "").strip()


def configured(cfg=None):
    cfg = cfg if cfg is not None else _cfg()
    if not cfg.get("creds_enabled"):
        return False
    return bool(slot_of(cfg) and code_of(cfg) and slot_url(cfg))


def slot_url(cfg=None):
    """教室端要拉取的地址：本地目录 / 自建 base / GitHub raw。"""
    cfg = cfg if cfg is not None else _cfg()
    slot = slot_of(cfg)
    if not slot:
        return ""
    base = str(cfg.get("creds_base", "") or "").strip()
    if base:
        if not base.endswith("/"):
            base += "/"
        return base + (CFG_REL % slot)
    local = str(cfg.get("creds_path", "") or "").strip()
    if local:
        p = os.path.join(local, "creds", "slot-%s.json" % slot)
        return "file:///" + p.replace("\\", "/")
    repo = str(cfg.get("creds_repo", "") or "").strip().strip("/")
    if repo.count("/") == 1:
        branch = str(cfg.get("creds_branch", "") or "main").strip() or "main"
        return "https://raw.githubusercontent.com/%s/%s/creds/slot-%s.json" % (
            repo, branch, slot)
    return ""


# ---------------------------------------------------------------------------
# 拉取 + 解密
# ---------------------------------------------------------------------------
def _http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "ClassCaller/2.0",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def parse_expires(text):
    """'2026-11-25 11:48:24 UTC' → epoch（拿不到就 0）。"""
    text = str(text or "").strip()
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S"):
        try:
            t = time.strptime(text, fmt)
            if fmt.endswith("UTC") or fmt.endswith("Z"):
                return calendar.timegm(t)
            return time.mktime(t)
        except Exception:
            continue
    return 0


def unseal(code, slot, payload):
    """用激活码解开密文（算法与发布工具一致，见 publisher/store.py）。"""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if str(payload.get("slot", "")) != str(slot):
        raise ValueError("槽位不匹配（这文件不是本班的）")
    norm = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    salt = base64.b64decode(payload["salt"])
    nonce = base64.b64decode(payload["nonce"])
    ct = base64.b64decode(payload["ct"])
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=int(payload.get("iter") or 260000))
    key = kdf.derive(norm.encode("utf-8"))
    plain = AESGCM(key).decrypt(nonce, ct, str(slot).encode("utf-8"))
    return json.loads(plain.decode("utf-8"))


def refresh_once(log=None):
    """拉一次并解密；成功就把令牌交给隧道。返回 (ok, 说明)。"""
    cfg = _cfg()
    slot = slot_of(cfg)
    if not configured(cfg):
        with _LOCK:
            _STATE.update({"state": "idle", "message": "还没配置凭据"})
        return False, "还没配置凭据"
    url = slot_url(cfg)
    with _LOCK:
        _STATE.update({"state": "fetching", "message": "正在拉取…",
                       "slot": slot, "source": url})
    try:
        payload = _http_json(url, timeout=int(cfg.get("creds_timeout", 15) or 15))
    except Exception as exc:
        return _fail("拉取失败：%r" % (exc,), log)
    try:
        plain = unseal(code_of(cfg), slot, payload)
    except Exception as exc:
        return _fail("解密失败：%s" % (exc,), log)

    token = str(plain.get("token", "") or "")
    if not token:
        return _fail("凭据里没有令牌", log)
    exp_ts = parse_expires(plain.get("expires", ""))
    if exp_ts and exp_ts - time.time() < 300:
        return _fail("令牌已过期（%s），等发布机重新签发" % plain.get("expires"), log)

    with _LOCK:
        _TOKEN.update({"token": token,
                       "tunnel_id": str(plain.get("tunnel_id") or ""),
                       "port": int(plain.get("port") or 0),
                       "expires_ts": exp_ts,
                       "expires": str(plain.get("expires") or ""),
                       "url": str(plain.get("url") or "")})
        _STATE.update({"state": "ok", "message": "",
                       "tunnel_id": str(plain.get("tunnel_id") or ""),
                       "port": int(plain.get("port") or 0),
                       "url": str(plain.get("url") or ""),
                       "expires": str(plain.get("expires") or ""),
                       "expires_ts": exp_ts, "fetched_at": time.time(),
                       "fails": 0})
    global _LAST_TOKEN
    changed = token != _LAST_TOKEN
    _LAST_TOKEN = token
    if log:
        log("[凭据] 已拉取令牌（%s 到期），%s" % (plain.get("expires", "?"),
                                            "已更新" if changed else "内容未变"))
    if _ON_UPDATE is not None:
        try:
            _ON_UPDATE(dict(_TOKEN))
        except Exception as exc:
            if log:
                log("[凭据] 交给隧道时出错：%r" % (exc,))
    return True, ""


def _fail(msg, log=None):
    with _LOCK:
        _STATE.update({"state": "error", "message": msg,
                       "fails": int(_STATE.get("fails") or 0) + 1})
    if log:
        log("[凭据] " + msg)
    return False, msg


def token_info():
    with _LOCK:
        return dict(_TOKEN)


def current_token():
    return token_info().get("token", "")


# ---------------------------------------------------------------------------
# 定时线程
# ---------------------------------------------------------------------------
def _loop(minutes):
    interval = max(5, int(minutes or 180)) * 60
    retry = 300          # 失败 5 分钟后重试
    while not _STOP.is_set():
        ok, _ = refresh_once()
        now = time.time()
        wait = interval if ok else retry
        with _LOCK:
            info = dict(_TOKEN)
            _STATE["next_at"] = now + wait
        # 令牌快到期了（<2 小时）就提前再拉一次
        exp = info.get("expires_ts") or 0
        if ok and exp and exp - now < 7200:
            wait = min(wait, 1800)
        if _STOP.wait(wait):
            break


def start(cfg_getter, on_update=None, log=None):
    global _THREAD, _CFG_GETTER, _ON_UPDATE
    _CFG_GETTER = cfg_getter
    _ON_UPDATE = on_update
    cfg = _cfg()
    if not configured(cfg):
        with _LOCK:
            _STATE.update({"state": "idle", "message": "还没配置凭据"})
        return status()
    if _THREAD is not None and _THREAD.is_alive():
        return status()
    _STOP.clear()
    minutes = int(cfg.get("creds_poll_minutes", 180) or 180)
    _THREAD = threading.Thread(target=_loop, args=(minutes,), daemon=True,
                               name="creds-poll")
    _THREAD.start()
    if log:
        log("[凭据] 定时拉取已启动：每 %d 分钟一次" % minutes)
    return status()


def stop():
    _STOP.set()
    with _LOCK:
        _STATE["state"] = "idle"


def restart(cfg_getter, on_update=None, log=None):
    stop()
    time.sleep(0.2)
    return start(cfg_getter, on_update=on_update, log=log)


def status():
    with _LOCK:
        st = dict(_STATE)
        st["token_len"] = len(_TOKEN.get("token") or "")
    cfg = _cfg()
    st["enabled"] = bool(cfg.get("creds_enabled"))
    st["slot"] = slot_of(cfg)
    st["url_source"] = slot_url(cfg)
    st["configured"] = configured(cfg)
    st["code_set"] = bool(code_of(cfg))
    st["interval_minutes"] = int(cfg.get("creds_poll_minutes", 180) or 180)
    if st.get("expires_ts"):
        left = int(st["expires_ts"] - time.time())
        st["expires_in"] = left
        st["expires_text"] = ("%d 小时 %d 分" % (left // 3600, left % 3600 // 60)
                              if left > 0 else "已过期")
    return st
