# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
r"""
公网访问：微软 Dev Tunnels（命名隧道）管理

设计要点（都是实测踩出来的）：

1. `devtunnel.exe` 是 .NET 单文件程序，**放中文路径会直接崩**
   （Failed to create directory … error code 5）。所以统一装到
   `C:\ProgramData\ClassCaller\tools\`，并且把 DOTNET_BUNDLE_EXTRACT_BASE_DIR
   也指向纯 ASCII 目录。
2. 「命名隧道」= 固定 ID 的持久隧道，URL 形如
   https://<id>-<port>.jpe1.devtunnels.ms，重启电脑也不变。
3. 隧道默认有效期最长 30 天，所以带一个续期线程。
4. 登录走 GitHub 设备码（`devtunnel user login -g -d`），网页只负责把
   设备码和链接显示出来，凭证存在系统凭据管理器里，不由我们保管。
5. 桥接端口（默认 9444）只绑 127.0.0.1；隧道侧终止 TLS 并注入
   X-Forwarded-*，软件侧的鉴权靠 login.check_access() 完成。
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# 路径：必须是纯 ASCII（中文路径会让 .NET 单文件解包失败）
# ---------------------------------------------------------------------------
TOOL_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                        "ClassCaller", "tools")
EXE_PATH = os.path.join(TOOL_DIR, "devtunnel.exe")
DOWNLOAD_URL = "https://aka.ms/TunnelsCliDownload/win-x64"
_BUNDLE_DIR = os.path.join(TOOL_DIR, "bundle")

_URL_RE = re.compile(r"https://[A-Za-z0-9\-\.]+\.devtunnels\.ms[^\s]*")
_CODE_RE = re.compile(r"(?:code[:\s]+)([A-Z0-9]{4}-[A-Z0-9]{4})", re.I)

_STATE = {
    "login_state": "idle",     # idle | waiting | ok | error
    "login_user": "",
    "user_code": "",
    "verify_url": "",
    "login_error": "",
    "host_state": "stopped",   # stopped | starting | running | error
    "url": "",
    "tunnel_id": "",
    "public_port": 0,
    "last_error": "",
    "started_at": 0,
    "restarts": 0,
    "mode": "login",           # login = 本机登录托管；token = 教室端凭据托管
    "token_expires": "",
    "token_len": 0,
}
_LOCK = threading.Lock()
_LOGIN_PROC = None
_HOST_PROC = None
_WATCH_THREAD = None
_WATCH_STOP = threading.Event()
_WANT_RUN = {"on": False, "port": 0, "tunnel_id": "", "expire_hours": 720}


def _env():
    """子进程环境：把 .NET 解包目录指到 ASCII 路径，并关掉自动更新。"""
    env = dict(os.environ)
    try:
        os.makedirs(_BUNDLE_DIR, exist_ok=True)
    except Exception:
        pass
    env["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = _BUNDLE_DIR
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    return env


def _run(args, timeout=60):
    """同步执行 devtunnel 命令，返回 (rc, stdout+stderr)。"""
    try:
        proc = subprocess.run([EXE_PATH] + list(args), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=_env(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))
    except Exception as exc:
        return -1, "执行失败: %r" % (exc,)


# ---------------------------------------------------------------------------
# 二进制：没装过就下载（约 23 MB）
# ---------------------------------------------------------------------------
def ensure_binary(log=None):
    """确保 devtunnel.exe 存在。返回 (ok, 说明)。"""
    if os.path.isfile(EXE_PATH):
        return True, ""
    try:
        os.makedirs(TOOL_DIR, exist_ok=True)
    except Exception as exc:
        return False, "无法创建目录 %s：%r" % (TOOL_DIR, exc)
    tmp = EXE_PATH + ".download"
    try:
        if log:
            log("正在下载微软隧道组件…")
        req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "ClassCaller"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(tmp, EXE_PATH)
        if log:
            log("下载完成：%s" % EXE_PATH)
        return True, ""
    except Exception as exc:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, "下载失败：%r" % (exc,)


def binary_ready():
    return os.path.isfile(EXE_PATH)


# ---------------------------------------------------------------------------
# 登录（GitHub 设备码）
# ---------------------------------------------------------------------------
def show_tunnel(tunnel_id):
    """隧道详情（JSON）。名字是自定义的时候，靠它看端口和公网地址。"""
    rc, out = _run(["show", tunnel_id, "-j"], timeout=60)
    start = (out or "").find("{")
    if rc != 0 or start < 0:
        return {}
    try:
        return json.loads(out[start:out.rfind("}") + 1])
    except Exception:
        return {}


def login_user():
    """已登录的账号（未登录返回空串）。"""
    if not binary_ready():
        return ""
    rc, out = _run(["user", "show"], timeout=30)
    m = re.search(r"Logged in as (\S+) using (\w+)", out)
    if m:
        return m.group(1)
    return ""


def login_start(log=None):
    """启动 GitHub 设备码登录，后台等待授权。返回状态字典。"""
    global _LOGIN_PROC
    ok, err = ensure_binary(log)
    if not ok:
        with _LOCK:
            _STATE.update({"login_state": "error", "login_error": err})
        return dict(_STATE)
    if login_user():
        with _LOCK:
            _STATE.update({"login_state": "ok", "login_user": login_user(),
                           "login_error": ""})
        return dict(_STATE)
    if _LOGIN_PROC is not None and _LOGIN_PROC.poll() is None:
        return dict(_STATE)

    with _LOCK:
        _STATE.update({"login_state": "waiting", "user_code": "",
                       "verify_url": "https://github.com/login/device",
                       "login_error": ""})
    try:
        _LOGIN_PROC = subprocess.Popen(
            [EXE_PATH, "user", "login", "-g", "-d"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=_env(), bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        with _LOCK:
            _STATE.update({"login_state": "error", "login_error": repr(exc)})
        return dict(_STATE)

    def _pump():
        seen = ""
        try:
            for line in _LOGIN_PROC.stdout:
                seen += line
                m = _CODE_RE.search(line)
                if m:
                    with _LOCK:
                        _STATE["user_code"] = m.group(1)
                if "logged in" in line.lower() or "successfully" in line.lower():
                    break
        except Exception:
            pass
        rc = _LOGIN_PROC.wait()
        user = login_user()
        with _LOCK:
            if user:
                _STATE.update({"login_state": "ok", "login_user": user,
                               "user_code": "", "login_error": ""})
            else:
                _STATE.update({"login_state": "error",
                               "login_error": (seen.strip().splitlines() or ["登录失败"])[-1][:200]})
        if log:
            log("[隧道] 登录结束：%s" % (user or "未登录"))

    threading.Thread(target=_pump, daemon=True, name="dt-login").start()
    return dict(_STATE)


# ---------------------------------------------------------------------------
# 隧道：创建（固定 ID）+ 托管
# ---------------------------------------------------------------------------
def _pick_tunnel_id(wanted=""):
    """确定隧道 ID：优先用户指定，其次沿用上次的，最后生成一个随机短名。"""
    wanted = (wanted or "").strip().lower()
    if wanted:
        return re.sub(r"[^a-z0-9\-]", "", wanted)[:32] or "classcaller"
    rid = "classcaller-" + "".join(
        __import__("secrets").choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(4))
    return rid


def _create_if_needed(tunnel_id, expire_hours, log=None):
    """确保隧道存在（已存在则复用），并刷新有效期。"""
    rc, out = _run(["list"], timeout=60)
    low = (out or "").lower()
    exists = (tunnel_id.split(".")[0].lower() in low) or ("conflict" in low)
    if not exists:
        rc, out = _run(["create", tunnel_id, "--expiration", "%dh" % int(expire_hours)],
                       timeout=90)
        if rc != 0:
            txt = (out or "").lower()
            if "already exists" not in txt and "conflict" not in txt:
                if log:
                    log("[隧道] 创建失败：%s" % out.strip()[-200:])
                return False, out.strip()[-200:]
    else:
        _run(["update", tunnel_id, "--expiration", "%dh" % int(expire_hours)], timeout=60)
    return True, ""


def _ensure_port(tunnel_id, port, log=None):
    """给已存在的隧道登记端口。

    实测：`host <id> -p 9444` 会报 "Batch update of ports is not supported"，
    必须先单独 `port create`，再 `host <id>`（不带 -p）。
    """
    rc, out = _run(["port", "create", tunnel_id, "-p", str(port),
                    "--protocol", "http"], timeout=60)
    if rc != 0:
        txt = (out or "").lower()
        if "exist" not in txt and "conflict" not in txt:
            if log:
                log("[隧道] 端口登记失败：%s" % out.strip()[-200:])
            return False
    return True


def _ensure_anonymous(tunnel_id, log=None):
    """开放匿名访问（用户要求：隧道本身不设密码，只靠软件密码）。

    已有隧道用 `host --allow-anonymous` 不生效，必须显式建一条访问控制项。
    """
    rc, out = _run(["access", "create", tunnel_id, "--anonymous"], timeout=60)
    if rc != 0:
        txt = (out or "").lower()
        if "exist" not in txt and "conflict" not in txt:
            if log:
                log("[隧道] 开放匿名访问失败（可能已开放）：%s" % out.strip()[-160:])
    return True


def _kill_stale_hosts(tunnel_id, log=None):
    """清掉所有"devtunnel host"进程（除我们自己刚拉起的那个）。

    一台电脑只托管一条隧道：切隧道时如果旧的 host 还在，
    旧地址会继续可以访问 —— 所以这里一律清掉。
    """
    keep = None
    try:
        keep = _HOST_PROC.pid if _HOST_PROC is not None else None
    except Exception:
        keep = None
    killed = 0
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='devtunnel.exe'",
             "get", "processid,commandline"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except Exception:
        return
    for line in out.splitlines():
        low = line.lower()
        if "devtunnel.exe" not in low or re.search(r"\bhost\b", low) is None:
            continue
        m = re.search(r"(\d+)\s*$", line.strip())
        if not m:
            continue
        pid = m.group(1)
        if keep and str(keep) == pid:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            killed += 1
        except Exception:
            pass
    if killed and log:
        log("[隧道] 已停掉 %d 个旧的隧道进程" % killed)

# --- 教室端令牌模式 / host 日志（切隧道时要维护的状态）---
_TOKEN = {"token": "", "tunnel_id": "", "port": 0, "expires_ts": 0}
_HOST_LOG = os.path.join(TOOL_DIR, "host.log")
_HOST_LOG_FH = None


def _clean_id(tunnel_id):
    return re.sub(r"[^a-z0-9\-]", "", str(tunnel_id or "").split(".")[0].lower())[:32]


def _token_usable(tunnel_id=""):
    """当前令牌能不能用：要有、没过期（留 10 分钟余量）、隧道对得上。"""
    with _LOCK:
        tok = _TOKEN.get("token", "")
        tid = _TOKEN.get("tunnel_id", "")
        exp = _TOKEN.get("expires_ts") or 0
    if not tok:
        return ""
    if exp and exp - time.time() < 600:
        return ""
    if tunnel_id and tid and tunnel_id != tid:
        return ""
    return tok


def set_token(info, log=None):
    """教室端拉到新令牌后交给这里。

    * 令牌变了就重启 host，让它用新令牌重连（旧令牌 24 小时到期）
    * 隧道 ID / 端口也一起带过来，第一次拿到时可直接启动托管
    """
    info = info or {}
    tok = str(info.get("token", "") or "")
    if not tok:
        return False
    changed = tok != _TOKEN.get("token")
    with _LOCK:
        _TOKEN.update({"token": tok,
                       "tunnel_id": _clean_id(info.get("tunnel_id")),
                       "port": int(info.get("port") or 0),
                       "expires_ts": float(info.get("expires_ts") or 0)})
        _STATE.update({"token_len": len(tok),
                       "token_expires": str(info.get("expires", "") or "")})
        want = dict(_WANT_RUN)
    if changed and want.get("on"):
        with _LOCK:
            _STATE["host_state"] = "starting"
        _spawn_host()
        if log:
            log("[隧道] 已用新令牌重连")
    return True


def _url_from_log(port):
    """从 host 日志里读公网地址（每行形如 Connect via browser: https://…）。"""
    try:
        with open(_HOST_LOG, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return ""
    best = ""
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip("/")
        if "-inspect." in url:
            continue
        if ("-%d." % int(port)) in url:
            return url
        best = best or url
    return best


def start(port, tunnel_id="", expire_hours=720, log=None, prepare=True):
    """启动（或重启）隧道托管。异步生效，用 status() 查结果。

    两种模式：
      登录模式 —— 本机登录了隧道账号：建隧道、登记端口、开匿名访问，全都自己来；
      令牌模式 —— 教室电脑没登录账号：这些准备动作发布机已经做过了，
                  本机只负责用令牌把 host 拉起来（这正是"免登录部署"的关键）。
    """
    ok, err = ensure_binary(log)
    if not ok:
        with _LOCK:
            _STATE.update({"host_state": "error", "last_error": err})
        return dict(_STATE)

    want_tid = _clean_id(tunnel_id) or _clean_id(_TOKEN.get("tunnel_id", ""))
    token = _token_usable(want_tid)
    logged = bool(login_user())
    if not logged and not token:
        with _LOCK:
            _STATE.update({"host_state": "error",
                           "last_error": "还没登录 GitHub，也没拿到教室凭据"})
        return dict(_STATE)

    tid = _pick_tunnel_id(want_tid or (_TOKEN.get("tunnel_id", "") if token else ""))
    # 切隧道前先把自己在托管的旧隧道停掉（否则旧地址还能访问）
    with _LOCK:
        old_tid = _WANT_RUN.get("tunnel_id", "")
        running = bool(_WANT_RUN.get("on"))
    if running and old_tid and old_tid != tid:
        try:
            stop(log=log)
        except Exception:
            pass
    prepare = bool(prepare)
    _kill_stale_hosts(tid, log=log)
    # 有教室凭据就优先走令牌模式：教室电脑一般登录不上账号，
    # 而且建隧道 / 登记端口 / 开匿名访问这些准备动作发布机都做过了。
    use_token = bool(token)
    if not use_token and prepare:
        created, cerr = _create_if_needed(tid, expire_hours, log=log)
        if not created:
            with _LOCK:
                _STATE.update({"host_state": "error", "last_error": cerr})
            return dict(_STATE)
        if not _ensure_port(tid, int(port), log=log):
            with _LOCK:
                _STATE.update({"host_state": "error",
                               "last_error": "端口 %d 登记失败" % int(port)})
            return dict(_STATE)
        _ensure_anonymous(tid, log=log)
    elif use_token and log:
        log("[隧道] 令牌模式：隧道 %s 已由发布机准备，本机只负责托管" % tid)

    with _LOCK:
        _WANT_RUN.update({"on": True, "port": int(port), "tunnel_id": tid,
                          "expire_hours": int(expire_hours)})
        _STATE.update({"host_state": "starting", "tunnel_id": tid,
                       "public_port": int(port), "last_error": "",
                       "url": "",          # 换隧道时必须清掉旧地址，否则界面显示上一个隧道的网址
                       "mode": "token" if use_token else "login"})
    _spawn_host()
    _ensure_watch()
    return dict(_STATE)


def public_url_of(tunnel_id, port):
    """从 `devtunnel show` 里读出该端口的公网地址。

    为什么不解析 host 的 stdout：devtunnel 在管道里会整块缓冲，
    URL 可能几十秒都不刷出来（实测）。`show` 是同步返回的，稳定得多。
    """
    rc, out = _run(["show", tunnel_id], timeout=45)
    if rc != 0 or not out:
        return ""
    for line in out.splitlines():
        if str(port) not in line:
            continue
        m = _URL_RE.search(line)
        if m:
            return m.group(0).rstrip("/")
    return ""


def named_url(tunnel_id, port):
    """用隧道 ID 拼出的固定地址：https://<id>-<port>.<cluster>.devtunnels.ms

    实测（2026-11，jpe1）：这个地址和 CLI 打印的随机别名指向**同一条隧道**，
    但地址里带自己起的名字、重启也不变，二维码可以长期发出去。
    """
    tid = _clean_id(tunnel_id)
    if not tid or not port:
        return ""
    try:
        info = show_tunnel(tid).get("tunnel") or {}
    except Exception:
        info = {}
    full = str(info.get("tunnelId") or "")
    cluster = full.split(".", 1)[1] if "." in full else ""
    if not cluster:
        return ""
    return "https://%s-%d.%s.devtunnels.ms" % (tid, int(port), cluster)


def _spawn_host():
    """真正拉起 `devtunnel host`。

    * 有令牌就带 `-t`（教室电脑不用登录账号）
    * stdout 落到日志文件：既能留证据，又能从里面读出公网 URL
    * URL 不走 `devtunnel show`——教室端没登录，那条路走不通
    """
    global _HOST_PROC, _HOST_LOG_FH
    with _LOCK:
        want = dict(_WANT_RUN)
    if not want.get("on"):
        return
    tid, port = want["tunnel_id"], int(want["port"])
    named = named_url(tid, port)      # 优先用"带名字"的地址
    token = _token_usable(tid)
    args = [EXE_PATH, "host", tid]
    if token:
        args += ["-t", token]
    if _HOST_LOG_FH is not None:
        try:
            _HOST_LOG_FH.close()
        except Exception:
            pass
        _HOST_LOG_FH = None
    try:
        try:
            with open(_HOST_LOG, "w", encoding="utf-8") as f:
                f.write("# devtunnel host %s\n" % tid)
        except Exception:
            pass
        _HOST_LOG_FH = open(_HOST_LOG, "ab", buffering=0)
        _HOST_PROC = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=_HOST_LOG_FH,
            stderr=subprocess.STDOUT, env=_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        with _LOCK:
            _STATE.update({"host_state": "error", "last_error": repr(exc)})
        return

    def _poll():
        for _ in range(40):                      # 最多等 80 秒
            time.sleep(2)
            proc = _HOST_PROC
            if proc is not None and proc.poll() is not None:
                with _LOCK:
                    if _STATE["host_state"] != "running":
                        _STATE.update({"host_state": "stopped", "url": "",
                                       "last_error": "隧道进程已退出（令牌可能已过期）"})
                return
            url = named or _url_from_log(port)
            if not url and login_user():
                url = public_url_of(tid, port)   # 本机登录着就还有这条后路
            if url:
                with _LOCK:
                    _STATE.update({"host_state": "running", "url": url,
                                   "started_at": time.time(), "last_error": ""})
                return
        with _LOCK:
            _STATE.update({"host_state": "error",
                           "last_error": "隧道已启动但迟迟拿不到公网地址，请检查网络后重试"})

    threading.Thread(target=_poll, daemon=True, name="dt-url").start()


def _ensure_watch():
    """守护线程：进程挂了自动重拉，URL 丢了自动重试（指数退避，最多 60 秒一次）。"""
    global _WATCH_THREAD
    if _WATCH_THREAD is not None and _WATCH_THREAD.is_alive():
        return
    _WATCH_STOP.clear()

    def _loop():
        delay = 3
        while not _WATCH_STOP.is_set():
            time.sleep(delay)
            with _LOCK:
                on = _WANT_RUN.get("on")
                state = _STATE["host_state"]
            if not on:
                delay = 3
                continue
            alive = _HOST_PROC is not None and _HOST_PROC.poll() is None
            if not alive:
                with _LOCK:
                    _STATE["host_state"] = "starting"
                    _STATE["restarts"] += 1
                _spawn_host()
                delay = min(delay * 2, 60)
            else:
                delay = 10

    _WATCH_THREAD = threading.Thread(target=_loop, daemon=True, name="dt-watch")
    _WATCH_THREAD.start()


def stop(log=None):
    """停止隧道托管（并尽力回收进程）。"""
    global _HOST_PROC
    with _LOCK:
        _WANT_RUN["on"] = False
        _STATE.update({"host_state": "stopped", "url": ""})
    proc, _HOST_PROC = _HOST_PROC, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if log:
        log("[隧道] 已停止")
    return dict(_STATE)


def status():
    with _LOCK:
        st = dict(_STATE)
    st["binary_ready"] = binary_ready()
    st["exe_path"] = EXE_PATH
    if st["login_state"] != "ok":
        user = login_user() if binary_ready() else ""
        if user:
            st["login_state"] = "ok"
            st["login_user"] = user
    return st


def _atexit_stop():
    try:
        if _WANT_RUN.get("on"):
            stop()
    except Exception:
        pass


atexit.register(_atexit_stop)


def summary_line():
    """给启动日志用的一行摘要。"""
    st = status()
    if st["host_state"] == "running" and st["url"]:
        return "公网访问已开通：%s" % st["url"]
    if st["host_state"] == "error":
        return "公网访问启动失败：%s" % (st.get("last_error") or "未知原因")
    return "公网访问未开通"
