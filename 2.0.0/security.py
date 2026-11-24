# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
安全加固层（本地改造，GPL-3.0-or-later）

原版的问题：HTTP 明文端口和 HTTPS 安全端口挂的是同一个 Flask app，
敏感接口只校验令牌、不校验信道。于是只要把地址从 https://主机名.local:9443
换成 http://IP:8000，摄像头画面、麦克风音频、屏幕画面和整盘文件就都在局域网上
明文传输了。

本模块只做三件事，不改动任何业务逻辑：
1) 信道判定 + 强制 HTTPS：敏感接口在明文连接上一律 403，敏感页面自动跳 https；
2) 安全响应头：CSP（带 nonce）、HSTS、nosniff、禁止被 iframe 嵌套、
   Referrer-Policy（防止 URL 里的 token 通过 Referer 外泄）；
3) 激活接口的失败限速：局域网内暴力猜一次性密码会被锁一段时间。

依赖仅标准库 + Flask 的 request / g / jsonify，避免与 app.py、auth.py 循环引用。
"""
import ipaddress
import secrets
import threading
import time

from flask import jsonify, request

# ---------------------------------------------------------------------------
# 受信代理
#
# 默认**不信任任何代理**（列表为空）：此时 X-Forwarded-Proto 一律忽略，
# 谁都能伪造，认了就等于没有强制 HTTPS。
#
# 只有在前面确实架了反向代理/隧道时，才把它的地址填进 trusted_proxies。
# 典型配置：
#   本机跑 cloudflared / 本机 nginx   -> ["127.0.0.1", "::1"]
#   Caddy 在另一台机器 10.0.0.5      -> ["10.0.0.5"]
#   整个内网段                          -> ["10.0.0.0/8"]
#
# 判定规则：**只看 TCP 对端地址**（remote_addr）是否落在受信网段里。
# 请求头里写的任何地址都不参与这个判断——否则攻击者自己加个
# X-Forwarded-For: 127.0.0.1 就能冒充代理。
# ---------------------------------------------------------------------------
_trusted_proxies = []


def set_trusted_proxies(items):
    """登记受信代理网段；写法非法或为空的条目直接跳过。

    内部保存 (网络对象, 原始字符串) 便于回显——否则日志里会显示成
    IPv4Network('127.0.0.1/32')，管理员看不懂自己配了什么。
    """
    global _trusted_proxies
    nets = []
    seen = set()
    for item in items or []:
        try:
            text = str(item).strip()
            if not text or text in seen:
                continue
            nets.append((ipaddress.ip_network(text, strict=False), text))
            seen.add(text)
        except Exception:
            continue
    _trusted_proxies = nets


def trusted_proxies():
    """返回原始写法（字符串列表），用于日志回显。"""
    return [label for _net, label in _trusted_proxies]


def _peer_is_trusted_proxy():
    """当前 TCP 对端是否为受信代理。"""
    if not _trusted_proxies:
        return False
    addr = (request.remote_addr or "").strip()
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except Exception:
        return False
    for net, _label in _trusted_proxies:
        try:
            if ip in net:
                return True
        except Exception:
            continue
    return False


def _first_header_ip(value):
    """从 X-Forwarded-For 这类逗号列表里取第一个合法 IP。"""
    for part in str(value or "").split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except Exception:
            continue
    return ""


def client_ip():
    """真实客户端 IP。

    只有**受信代理**转发的请求才读 X-Forwarded-For / X-Real-IP；
    其它情况一律用 TCP 对端地址，避免请求头伪造。
    限速、审计都用这个值，否则挂上 Cloudflare 之后所有请求都会被
    记成 127.0.0.1，一个人爆破就会把所有人一起锁死。
    """
    try:
        if _peer_is_trusted_proxy():
            real = _first_header_ip(request.headers.get("X-Forwarded-For"))
            if real:
                return real
            real = _first_header_ip(request.headers.get("X-Real-IP"))
            if real:
                return real
        return request.remote_addr or ""
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# 运行态：HTTPS 到底能不能用（由 app.py 在安全端口绑定成功后置位）
# ---------------------------------------------------------------------------
_https_available = False
_secure_port = 0


def set_https_available(available, port=0):
    """置位"强制 HTTPS"是否生效。

    只有 HTTPS 真的起来了才置 True：万一安全端口启动失败，或者用户在设置里
    主动关掉了 HTTPS，这里保持 False，鉴权退回原行为，不会把人锁在门外。
    """
    global _https_available, _secure_port
    _https_available = bool(available)
    _secure_port = int(port or 0)


def https_available():
    return _https_available


def secure_port():
    return _secure_port


# ---------------------------------------------------------------------------
# 公网入口（隧道专用端口，默认 9444）
#
# 隧道（微软 Dev Tunnels / 任何反代）把请求从**回环**转进来，
# 也就是说 remote_addr 永远是 127.0.0.1、Host 被改写成 localhost。
# 只靠 IP 判断"是不是本机"必然误判 —— 于是任何拿到隧道网址的人都会
# 被当成"坐在电脑前的人"，直接拿到管理员权限。
#
# 所以公网口单独开一个监听、单独一套判定：
#   1) 这个端口上的请求 **永远不算本机**，永远要求登录；
#   2) 这个端口上唯一的可信代理就是回环自己，因此采信它给的
#      X-Forwarded-For / X-Forwarded-Proto（限速、审计、HTTPS 判定靠它）。
# ---------------------------------------------------------------------------
_public_port = 0


def set_public_port(port):
    global _public_port
    try:
        _public_port = int(port or 0)
    except Exception:
        _public_port = 0


def public_port():
    return _public_port


def is_public_request():
    """当前请求是否来自"公网口"（隧道那一路）。"""
    if not _public_port:
        return False
    try:
        cands = []
        sp = request.environ.get("SERVER_PORT")
        if sp:
            cands.append(str(sp))
        host = request.host or ""
        if ":" in host:
            cands.append(host.rsplit(":", 1)[1])
        for c in cands:
            if c.isdigit() and int(c) == _public_port:
                return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 信道判定
# ---------------------------------------------------------------------------
def is_secure_request():
    """是否处于加密信道。

    判定顺序：
      1) werkzeug 写入的 wsgi.url_scheme= https  -> 直接成立（本机 HTTPS 端口）
      2) 若 TCP 对端是**受信代理**，采信它转发的 X-Forwarded-Proto: https
      3) 其它情况一律不认请求头（防伪造）

    第 2 条是给 Cloudflare Tunnel / nginx / Caddy 这类部署用的：
    TLS 在边缘层终止，到这里已经是 http://127.0.0.1:8000 的明文回环，
    但对用户而言链路是加密的，不应该再逼他跳到自签名 https 端口。
    """
    try:
        scheme = (request.environ.get("wsgi.url_scheme") or "").lower()
        if scheme == "https":
            return True
        if _peer_is_trusted_proxy():
            proto = (request.headers.get("X-Forwarded-Proto") or "")
            proto = proto.split(",")[0].strip().lower()
            if proto == "https":
                return True
        return False
    except Exception:
        return False


def forwarded_host():
    """受信代理后面用户实际访问的主机名（否则空串）。"""
    if not _peer_is_trusted_proxy():
        return ""
    try:
        host = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        return host
    except Exception:
        return ""


def _host_only():
    """当前请求的主机名，去掉端口；兼容 IPv6 的 [::1]:8000 写法。"""
    host = forwarded_host() or request.host or ""
    if host.startswith("["):
        end = host.find("]")
        return host[:end + 1] if end > 0 else host
    return host.split(":")[0]


def https_url(path=None, query=""):
    """把当前请求拼成 https 地址，用于跳转提示。

    受信代理后面不再拼本机的安全端口：对外的 443 就是用户敲的地址。
    """
    p = request.path if path is None else path
    host = _host_only()
    behind_tls_proxy = bool(forwarded_host()) and is_secure_request()
    if behind_tls_proxy:
        url = "https://%s%s" % (host, p)
    elif _secure_port:
        url = "https://%s:%d%s" % (host, _secure_port, p)
    else:
        url = "https://%s%s" % (host, p)
    if query:
        q = query.decode("utf-8", "replace") if isinstance(query, bytes) else str(query)
        if q:
            url = url + "?" + q
    return url


def require_secure():
    """敏感接口用：明文连接返回 403，其余返回 None 表示放行。

    返回的 403 里带上正确的 https 地址，前端可以直接跳过去。
    """
    if not _https_available:
        return None            # HTTPS 未启用：退回原行为，不锁死功能
    if is_secure_request():
        return None
    target = https_url()
    return jsonify({
        "ok": False,
        "need_https": True,
        "https_url": target,
        "error": "该功能必须走加密连接，请用安全地址打开：%s" % target,
    }), 403


# ---------------------------------------------------------------------------
# 安全响应头
# ---------------------------------------------------------------------------
# 自签名证书场景下 HSTS 不宜设太长：证书重签或端口变更后，浏览器才不会因为
# 记住的 HSTS 把人挡在门外。1 天足够挡住降级攻击，也留了回退余地。
_HSTS_MAX_AGE = 86400


def build_csp(nonce=None):
    """构造 CSP。

    script-src 有 nonce 时就不用 'unsafe-inline'，内联脚本必须带对应 nonce 才
    执行；style-src 保留 'unsafe-inline'，因为模板里有十几处 style= 属性。
    """
    if nonce:
        script = "'self' 'nonce-%s'" % nonce
    else:
        # 理论上不会走到这里；万一没有 nonce，宁可放宽也不能让页面白屏
        script = "'self' 'unsafe-inline'"
    return "; ".join([
        "default-src 'self'",
        "script-src " + script,
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "connect-src 'self' ws: wss:",
        "media-src 'self' blob:",
        "font-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ])


def apply_headers(resp, nonce=None):
    """给每个响应补安全头。"""
    secure = is_secure_request()
    resp.headers["Content-Security-Policy"] = build_csp(nonce)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    # 本程序用不到这些能力，直接关掉，缩小被利用面；
    # 摄像头/麦克风不动：手机端对讲要用 getUserMedia。
    resp.headers["Permissions-Policy"] = (
        "geolocation=(), payment=(), usb=(), serial=(), hid=(), midi=()")
    if secure:
        resp.headers["Strict-Transport-Security"] = "max-age=%d" % _HSTS_MAX_AGE
    return resp


def new_nonce():
    return secrets.token_urlsafe(16)


# ---------------------------------------------------------------------------
# 失败限速（内存实现，进程重启清零；局域网自用够用）
# ---------------------------------------------------------------------------
class AttemptLimiter(object):
    """按来源 IP 记失败次数：窗口内连续失败达上限后锁定一段时间。

    window 秒内失败 max_failures 次 -> 锁定 lockout 秒。
    成功一次就清零，所以正常用户（输错一两次）不会被误伤。
    """

    def __init__(self, max_failures=8, window=600, lockout=900):
        self.max_failures = max(1, int(max_failures))
        self.window = max(1, int(window))
        self.lockout = max(1, int(lockout))
        self._lock = threading.Lock()
        self._items = {}

    def retry_after(self, key):
        """还要等多少秒才能再试；0 表示现在可以试。"""
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if not item:
                return 0
            if item.get("until", 0) > now:
                return int(item["until"] - now) + 1
            if now - item.get("first", 0) > self.window:
                self._items.pop(key, None)
            return 0

    def fail(self, key):
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if not item or now - item.get("first", 0) > self.window:
                item = {"count": 0, "first": now, "until": 0}
                self._items[key] = item
            item["count"] = item.get("count", 0) + 1
            if item["count"] >= self.max_failures:
                item["until"] = now + self.lockout
            # 顺手清理陈旧记录，避免字典无限增长
            if len(self._items) > 512:
                stale = [k for k, v in self._items.items()
                         if now - v.get("first", 0) > self.window
                         and v.get("until", 0) <= now]
                for k in stale:
                    self._items.pop(k, None)

    def reset(self, key):
        with self._lock:
            self._items.pop(key, None)


# 激活 / 扫码激活共用一个限速器
ACTIVATION_LIMITER = AttemptLimiter(max_failures=8, window=600, lockout=900)


class IntervalLimiter(object):
    """最小间隔节流：同一 key 两次操作之间至少间隔 min_interval 秒。

    和 AttemptLimiter 的区别：
      * AttemptLimiter 数的是**失败次数**，用于"输错密码"这类场景；
      * IntervalLimiter 管的是**操作频率**，无论成功失败都算，用于叫号这类
        需要保证物理动作间隔的场景（教室喇叭不能连珠炮一样响）。

    实现用"上次时间戳 + 短暂预占"，保证多线程并发时也不会两个请求同时通过：
    检查与写入在同一把锁内完成。
    """

    def __init__(self, min_interval=2.0, max_keys=2048):
        self.min_interval = max(0.0, float(min_interval))
        self.max_keys = max(16, int(max_keys))
        self._lock = threading.Lock()
        self._items = {}

    def wait_seconds(self, key):
        """还要等多少秒；0 表示现在可以执行。不占位。"""
        if self.min_interval <= 0:
            return 0.0
        now = time.time()
        with self._lock:
            last = self._items.get(key, 0.0)
            remain = self.min_interval - (now - last)
            return remain if remain > 0 else 0.0

    def take(self, key):
        """尝试占用一次额度。

        返回 (是否允许, 还需等待的秒数)。允许时已写入时间戳，
        调用方不需要再调用别的方法；不允许时不做任何修改。
        """
        if self.min_interval <= 0:
            return True, 0.0
        now = time.time()
        with self._lock:
            last = self._items.get(key, 0.0)
            remain = self.min_interval - (now - last)
            if remain > 0:
                return False, remain
            self._items[key] = now
            if len(self._items) > self.max_keys:
                cutoff = now - self.min_interval * 4
                for k in [k for k, v in self._items.items() if v < cutoff]:
                    self._items.pop(k, None)
            return True, 0.0

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._items.clear()
            else:
                self._items.pop(key, None)


# 叫号节流：全局限一份（同一间教室只有一套喇叭），默认 2 秒一次。
# 之所以不按 IP 分：教室里多人同时点击时，真正需要保护的是"播报的节奏"，
# 而不是某个人。按 IP 分反而会让 3 个老师各点一次 = 2 秒内响 3 次。
CALL_LIMITER = IntervalLimiter(min_interval=2.0)


def set_call_interval(seconds):
    """运行时调整叫号最小间隔（配置项 call_min_interval）。"""
    try:
        CALL_LIMITER.min_interval = max(0.0, float(seconds))
    except Exception:
        pass


def init_from_config(cfg):
    """从一份配置字典里加载所有安全相关设置。

    app._main() 启动时调用一次。之所以集中在这里，是为了让"配置→生效"
    只有一条路径：无论是正常启动、还是测试里手工构造 app，只要调它就不会
    漏掉某个开关（曾经就漏过 call_min_interval，导致配置不生效）。
    """
    try:
        set_trusted_proxies((cfg or {}).get("trusted_proxies") or [])
    except Exception:
        set_trusted_proxies([])
    try:
        raw = (cfg or {}).get("call_min_interval", 2.0)
        set_call_interval(2.0 if raw is None else raw)
    except Exception:
        set_call_interval(2.0)
    try:
        raw = (cfg or {}).get("api_rate_limit", 60)
        API_LIMITER.per_minute = 0 if raw is None else int(raw)
    except Exception:
        API_LIMITER.per_minute = 60


class TokenBucketLimiter(object):
    """按 key 的令牌桶：每分钟补充 rate 个令牌，用完了就要等。

    和前面两个限速器的区别：
      * AttemptLimiter  数"失败次数"（密码错误）
      * IntervalLimiter 卡"两次操作的最小间隔"（叫号 2 秒一次）
      * TokenBucketLimiter 管"整体吞吐"（每分钟最多 N 次请求）

    适合挡"高频刷接口"这种不需要失败、纯靠量打的服务消耗。

    key 一般是 "客户端IP|路径组"，这样不同接口各自独立计数，
    某个接口被刷不会连累其它接口。
    """

    def __init__(self, per_minute=60, max_keys=4096):
        self.per_minute = max(0, int(per_minute))
        self.max_keys = max(64, int(max_keys))
        self._lock = threading.Lock()
        self._items = {}          # key -> {"tokens": float, "ts": float}

    def _refill(self, item, now):
        """按经过的时间补充令牌，上限为每分钟配额。"""
        if self.per_minute <= 0:
            return
        elapsed = now - item["ts"]
        if elapsed <= 0:
            return
        item["tokens"] = min(float(self.per_minute),
                             item["tokens"] + elapsed * (self.per_minute / 60.0))
        item["ts"] = now

    def take(self, key, cost=1):
        """尝试消耗一个令牌。返回 (是否允许, 建议等待秒数, 剩余令牌)。"""
        if self.per_minute <= 0:
            return True, 0.0, float("inf")
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                item = {"tokens": float(self.per_minute), "ts": now}
                self._items[key] = item
                # 容量保护：键太多时清掉最久没动过的
                if len(self._items) > self.max_keys:
                    stale = sorted(self._items.items(), key=lambda kv: kv[1]["ts"])
                    for k, _ in stale[:len(stale) // 4]:
                        self._items.pop(k, None)
            else:
                self._refill(item, now)

            if item["tokens"] >= cost:
                item["tokens"] -= cost
                return True, 0.0, item["tokens"]

            deficit = cost - item["tokens"]
            wait = deficit / (self.per_minute / 60.0)
            return False, wait, item["tokens"]

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._items.clear()
            else:
                self._items.pop(key, None)


# 通用接口限速：默认每个 (IP, 路径组) 每分钟 60 次。
# 0 表示关闭。叫号另有 2 秒节流，不靠这个兜底。
API_LIMITER = TokenBucketLimiter(per_minute=60)


# 路径分组：同类接口共用一份配额，避免"一个接口被刷、连累全部"。
_RATE_GROUPS = (
    ("/api/auth/login", "login"),
    ("/api/auth/reset", "reset"),
    ("/api/call", "call"),
    ("/api/message", "message"),
    ("/api/fs/", "fs"),
    ("/api/monitor/", "monitor"),
    ("/api/remote/", "remote"),
    ("/api/update/", "update"),
    ("/api/admin/", "admin"),
    ("/api/", "api"),
    ("/ws/", "ws"),
)

# 这些路径不参与通用限速（要么已有专门限速，要么是静态资源/健康检查）
_RATE_SKIP = ("/static/", "/favicon.ico", "/api/health")


def rate_group(path):
    """/api/fs/list -> "fs"；/api/roster -> "api"；不可限速的返回 ""。"""
    for prefix in _RATE_SKIP:
        if path.startswith(prefix):
            return ""
    for prefix, name in _RATE_GROUPS:
        if path.startswith(prefix):
            return name
    return ""


def check_rate_limit():
    """通用接口限速入口。返回 None 表示放行，否则返回 Flask 响应。

    - 只针对远程来源：本机直连（教室电脑操作界面）不设限，
      否则老师在电脑上连点导入/刷新会被自己挡住。
    - 登录/重置已有更严的 AttemptLimiter，这里用较大配额兜底即可。
    """
    if API_LIMITER.per_minute <= 0:
        return None
    try:
        path = request.path or ""
        group = rate_group(path)
        if not group:
            return None
        # 本机直连放行：教室电脑的正常操作不该被限
        if _peer_is_trusted_local():
            return None
        ip = client_ip() or "unknown"
        allowed, wait, _left = API_LIMITER.take("%s|%s" % (ip, group))
        if allowed:
            return None
        retry = max(1, int(wait) + 1)
        resp = jsonify({
            "ok": False,
            "rate_limited": True,
            "retry_after": retry,
            "error": "请求过于频繁，请 %d 秒后再试" % retry,
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry)
        return resp
    except Exception:
        return None                 # 限速本身出错不能阻断业务


def _local_autologin_on():
    """本机免登录是否开启（延迟导入 login，避免循环依赖）。

    login.py 在导入时就依赖 security，所以这里只能在**调用时**导入；
    好处是它按 config.json 的 mtime 热读取，改了立刻生效。
    """
    try:
        import login
        return login.local_auto_login_enabled()
    except Exception:
        return True


def _peer_is_trusted_local():
    """当前请求是否来自"未经代理的本机直连"。

    注意：这里不能调用 auth._has_proxy_headers()——security 是被 auth 导入的，
    反向引用会形成循环依赖（而且当时就是因为这个 NameError 被 except 吞掉，
    导致本机请求被误判为远程）。

    另外：**本机免登录关掉之后，本机位置不再享有任何特权**，包括限速豁免。
    否则 TCP 型中转（frpc 连 127.0.0.1、不注入 X-Forwarded-*）会把所有公网
    流量伪装成本机，导致整站限速被整体绕过。
    """
    if not _local_autologin_on():
        return False
    try:
        for key in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
            if request.headers.get(key):
                return False
        addr = (request.remote_addr or "").strip()
        if not addr:
            return False
        return ipaddress.ip_address(addr).is_loopback
    except Exception:
        return False
