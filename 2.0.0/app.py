# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序是自由软件：你可以依据自由软件基金会（FSF）发布的 GNU 通用公共许可证
# 第 3 版或任何更高版本的条款，再分发和/或修改本程序。不提供任何担保。
"""
班级叫号器 —— 主程序
Python + Flask 实现网页叫号、名条管理、TTS 播放、提示音、mDNS 局域网访问。

运行: python app.py
访问: http://本机IP:端口 或 http://主机名.local:端口 (需 mDNS 支持)
"""

import base64
import csv
import io
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

from flask import (Flask, Response, g, jsonify, render_template, request,
                   redirect, send_file, send_from_directory)
from flask_sock import Sock

# ---------------------------------------------------------------------------
# 基础路径与默认配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 便携版 Python 的 ._pth 不会把应用目录加入 sys.path，这里手动加入，
# 确保同目录的 desktop.py 等模块可以被 import。
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from auth import auth_bp, check_access, ensure_auth_secret, is_local
from fsapi import fs_bp
from monitor import (monitor_bp, monitor_shutdown, set_config_getter,
                     ws_monitor_audio, ws_monitor_camera, ws_monitor_intercom)
from remote import (remote_bp, remote_shutdown, set_desktop, ws_remote,
                    ws_remote_input,
                    set_config_getter as remote_set_config_getter)
import abr                    # 自适应码率控制器
import accounts               # 账号与会话（SQLite）
import security               # 信道判定 / 限速 / 安全响应头
import tunnel                 # 微软 Dev Tunnels（公网访问）
import creds                  # 教室端凭据（定时拉取，免登录）
import autologin              # 自动登录（GitHub 账号密码 → 设备码授权）
from login import login_bp, validate_password
import update as update_mod

ROOT_DIR = os.path.dirname(BASE_DIR)   # 版本目录的上一级（程序根目录，含 data/ 与 version.json）
DATA_DIR = os.path.join(ROOT_DIR, "data")   # 共享数据目录：OTA 更新时保留、不删除
SOUNDS_DIR = os.path.join(DATA_DIR, "sounds")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ROSTER_PATH = os.path.join(DATA_DIR, "roster.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
CALLS_PATH = os.path.join(DATA_DIR, "calls.json")
LOG_PATH = os.path.join(DATA_DIR, "calls.log")
LOGS_DIR = os.path.join(DATA_DIR, "logs")     # 运行日志（排障用，网页 /logs 可查看导出）

for _d in (DATA_DIR, SOUNDS_DIR, UPLOAD_DIR, LOGS_DIR):
    os.makedirs(_d, exist_ok=True)

DEFAULT_CONFIG = {
    "port": 8000,
    "mdns_enabled": True,
    "mdns_hostname": "",            # 空则使用电脑主机名
    "mdns_interface": "auto",       # mDNS 绑定网卡：auto=全部，或指定网卡名称（本机可在设置中选择）
    "phrase": "请%number%号%name%到%teacher%办公室%note%",
    "play_count": 1,
    "prompt_enabled": True,
    "prompt_sound": "beep",         # none | beep | custom
    "custom_sound_file": "",        # 存放在 sounds/ 下的文件名
    "tts_engine": "auto",           # auto | edge | comtypes
    "volume": 100,                  # 叫号时系统音量 0-100
    "volume_enabled": True,         # 叫号时是否切换系统音量
    "autostart": False,             # 开机自启
    "require_admin": False,          # 启动时自动申请管理员（自启用计划任务，登录不再弹 UAC）
    "secure_enabled": True,         # 是否启用 HTTPS 安全端口（手机端激活/敏感功能用）
    "monitor_quality": "standard",   # 监控画质：smooth(480p) / standard(720p) / hd(1080p)
    "remote_touch_mode": "auto",      # 手机触控：auto(触摸优先,失败降级鼠标) / touch / mouse
    "remote_gesture_mode": "zoom_scroll",  # 双指手势：zoom_scroll(缩放+滚动) / touch(真实触摸)
    "monitor_jpeg_quality": 0,       # 0=用预设；30~95 可直接指定 JPEG 质量
    "abr_enabled": True,             # 自适应码率：按手机回执自动调分辨率/质量/帧率
    "video_window": 8,               # 在途帧数上限（1=老式停等，越大上限越高，按需自动加）
    "monitor_audio_rate": 16000,     # 监控音频采样率（16000 省上行，48000 最保真）
    "latency_budget_monitor": 5.0,   # 监控/图传延迟预算（秒）：5=画质优先（宁晚几秒也不降画质）
    "latency_budget_remote": 0.5,    # 远控延迟预算（秒）：0.5=触控跟手
    "secure_port": 9443,            # HTTPS 安全端口（8443 常被 Windows 保留，故默认 9443）
    "update_channel": "stable",     # OTA 更新通道：stable | beta | dev
    "update_policy": "none",        # OTA 更新策略：none | check | download | install
    # ---- 公网访问（微软 Dev Tunnels）----
    "public_enabled": False,        # 是否开通公网访问（隧道）
    "public_port": 9444,            # 公网专用端口：只绑 127.0.0.1，仅隧道可达
    "tunnel_id": "",                # 固定隧道 ID（空=首次开通时自动生成）
    "tunnel_expiration_hours": 720,  # 隧道有效期（小时，最长 720=30 天），到期前自动续期
    "tunnel_autostart": True,      # 软件启动时自动开启穿透
    "tunnel_auto_login": True,     # 隧道登录失效时用保存的账号密码自动重登
    "creds_enabled": False,        # 教室端：从仓库定时拉取令牌（免登录）
    "creds_slot": "",              # 班级槽位号（发布工具里配的）
    "creds_code": "",              # 激活码（只在本机，不进仓库）
    "creds_repo": "",              # owner/repo
    "creds_branch": "main",
    "creds_base": "",              # 也可以直接给完整 base URL
    "creds_path": "",              # 本地文件夹（离线自测）
    "creds_poll_minutes": 180,     # 多久拉一次（令牌 24 小时到期）
    "creds_timeout": 15,
    "local_auto_login": True,       # 本机免登录（公网口强制关闭，不读此值）
    "trusted_proxies": ["127.0.0.1", "::1"],  # 唯一可信代理：本机回环（隧道入口）
    "api_rate_limit": 60,           # 通用接口限速：每 (IP,路径组) 每分钟次数，0=关闭
    "call_min_interval": 2.0,       # 叫号最小间隔（秒），0=不限制
}

try:
    import winsound
    HAS_WINSOUND = True
except Exception:
    HAS_WINSOUND = False

try:
    from zeroconf import ServiceInfo, Zeroconf, NonUniqueNameException
    try:
        from zeroconf import IPVersion       # 0.154 起可把收发限制在 IPv4
    except Exception:
        IPVersion = None
    HAS_ZEROCONF = True
except Exception:
    HAS_ZEROCONF = False
    IPVersion = None

# 控制台/日志编码统一成 UTF-8（Windows 默认 GBK 时，print 中文或 emoji 会抛
# UnicodeEncodeError，把后台线程带崩）
try:
    import sys as _sys
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
except Exception:
    pass

# 运行日志：print/logging 的输出同时进内存缓冲与 data/logs/app-日期.log，
# 网页 /logs 可实时查看 + 一键导出（不再需要截图黑窗口）。
import netif        # 网卡枚举：GetAdaptersAddresses（不依赖 PowerShell / WMI）
import runlog       # 运行日志

runlog.install(LOGS_DIR)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB，支持文件传输上传
# WebSocket 心跳：Cloudflare 隧道边缘约 100 秒无数据会断开，主动 ping 更稳
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 20}
sock = Sock(app)

app.register_blueprint(auth_bp)
app.register_blueprint(login_bp)      # 账号登录（公网访问的唯一凭据）
app.register_blueprint(monitor_bp)
app.register_blueprint(remote_bp)
app.register_blueprint(fs_bp)
app.register_blueprint(update_mod.update_bp)

# WebSocket 实时通道（低延迟：摄像头/麦克风/对讲/远程控制）
sock.route("/ws/monitor/camera")(ws_monitor_camera)
sock.route("/ws/monitor/audio")(ws_monitor_audio)
sock.route("/ws/monitor/intercom")(ws_monitor_intercom)
sock.route("/ws/remote")(ws_remote)
sock.route("/ws/remote/input")(ws_remote_input)   # 触控/键盘独立通道（不被画面帧堵住）

# 生成令牌签名密钥（首次运行自动写入 config.json）
ensure_auth_secret()

# 账号与会话库（SQLite，data/app.db）；不创建任何默认账号
accounts.init_db()


# ---------------------------------------------------------------------------
# 安全层：CSP nonce / 限速 / 登录闸门
#
# 顺序很重要：nonce -> 限速 -> 登录闸门。限速必须排在鉴权之前，
# 否则未登录的人刷接口就能把线程池占满（threaded=True 每请求一线程）。
# ---------------------------------------------------------------------------
_PUBLIC_PREFIXES = ("/login", "/static/", "/favicon.ico",
                    "/api/auth/login", "/api/auth/session", "/api/auth/logout",
                    "/api/auth/reset", "/api/health", "/plain", "/ping",
                    "/cert", "/cert.crt", "/cert.mobileconfig")
# 这些页面涉及画面/声音/文件/远控，远程必须走加密信道
_SECURE_PAGES = ("/login", "/monitor", "/remote", "/files", "/account",
                 "/admin", "/update", "/logs")


@app.before_request
def _cc_security_nonce():
    """每个请求一个 CSP nonce，模板里的 <script> 必须带上它才被允许执行。"""
    g.csp_nonce = security.new_nonce()
    return None


@app.context_processor
def _cc_template_nonce():
    """把 nonce 交给模板。

    login / admin / account 三页的内联脚本靠它才不被 CSP 拦掉。
    漏了这一步的后果很隐蔽：页面照样能打开，但所有内联 JS 全被浏览器拒执行——
    「加载中…」永远转圈、按钮点了没反应、登录失败还一句提示都没有。
    """
    return {"csp_nonce": getattr(g, "csp_nonce", "")}


@app.before_request
def _cc_rate_limit():
    """通用接口限速：每 (客户端 IP, 路径组) 每分钟 N 次。"""
    return security.check_rate_limit()


@app.before_request
def _cc_force_https_pages():
    """敏感页面：远程设备自动跳到安全端口（本机不受影响）。"""
    if not security.https_available() or security.is_secure_request():
        return None
    try:
        if is_local():
            return None
        path = request.path or "/"
        for prefix in _SECURE_PAGES:
            if path == prefix or path.startswith(prefix + "/"):
                if request.method in ("GET", "HEAD"):
                    return redirect(security.https_url(path, request.query_string),
                                    code=302)
                return jsonify({"ok": False, "need_https": True,
                                "https_url": security.https_url(),
                                "error": "该功能必须在安全连接（HTTPS）下使用：%s"
                                         % security.https_url()}), 403
    except Exception:
        return None
    return None


@app.before_request
def _cc_login_gate():
    """页面级登录闸门：未登录只放行登录页/静态资源/公开接口。

    接口不在这里拦（各自 check_access() 返回 401/need_login，前端据此跳登录页）；
    WebSocket 同理交给各自处理函数，因为这里返回 302 会破坏升级握手。
    """
    path = request.path or "/"
    for p in _PUBLIC_PREFIXES:
        if path == p or path.startswith(p.rstrip("/") + "/"):
            return None
    try:
        # 还没有任何账号（全新安装）：本机照常可用，等同老版本体验；
        # 公网口仍然走下面（必须登录）——而公网口又没账号可登，等于进不来。
        if not security.is_public_request() and accounts.count_users() == 0:
            return None
    except Exception:
        pass
    try:
        from login import current_user
        if current_user() is not None:
            return None
    except Exception:
        return None
    if path.startswith("/api/") or path.startswith("/ws/"):
        return None
    return redirect("/login?next=" + urllib.parse.quote(path or "/"))


@app.before_request
def _cc_public_gate():
    """公网口（隧道）总闸：除登录相关与健康检查外，**一切**都要求已登录。

    这是"只靠软件密码"的安全底线：哪怕某个接口忘了写 check_access()，
    只要它出现在公网口上，也必须先有会话才能进。局域网口不受影响。
    """
    try:
        if not security.is_public_request():
            return None
    except Exception:
        return None
    path = request.path or "/"
    allow = ("/login", "/static/", "/favicon.ico", "/api/auth/login",
             "/api/auth/session", "/api/auth/logout", "/api/health",
             "/api/auth/bootstrap",          # 放行到处理函数，由它返回明确的 403
             "/cert.crt", "/cert.mobileconfig")
    for p in allow:
        if path == p or path.startswith(p.rstrip("/") + "/"):
            return None
    try:
        from login import current_user
        if current_user() is not None:
            return None
    except Exception:
        pass
    if path.startswith("/api/") or path.startswith("/ws/"):
        return jsonify({"ok": False, "need_login": True,
                        "error": "请先登录后再使用该功能"}), 401
    return redirect("/login?next=" + urllib.parse.quote(path or "/"))


@app.after_request
def _no_cache(resp):
    """禁止浏览器缓存页面/脚本，并统一挂上安全响应头。"""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    try:
        security.apply_headers(resp, getattr(g, "csp_nonce", None))
    except Exception:
        pass
    return resp

CONFIG_LOCK = threading.Lock()
ROSTER_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
CALLS_LOCK = threading.Lock()
MDNS_LOCK = threading.Lock()

_mdns_zc = None      # 当前 Zeroconf 实例
_mdns_infos = []     # 当前已注册的 ServiceInfo 列表（每块网卡一个）
_running_port = None  # 实际监听端口，启动后写入
_running_secure_port = None  # 实际 HTTPS 监听端口，启动后写入
_desktop = None       # DesktopUI 实例，main() 中赋值


# ---------------------------------------------------------------------------
# 通用 JSON 读写
# ---------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_config():
    with CONFIG_LOCK:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(load_json(CONFIG_PATH, {}))
        return cfg


def save_config(cfg):
    with CONFIG_LOCK:
        save_json(CONFIG_PATH, cfg)


def get_roster():
    with ROSTER_LOCK:
        return load_json(ROSTER_PATH, [])


def save_roster(roster):
    with ROSTER_LOCK:
        save_json(ROSTER_PATH, roster)


def get_state():
    with STATE_LOCK:
        return load_json(STATE_PATH, {"call_count": 0})


def save_state(state):
    with STATE_LOCK:
        save_json(STATE_PATH, state)


def get_calls():
    with CALLS_LOCK:
        return load_json(CALLS_PATH, [])


def add_call_record(record):
    with CALLS_LOCK:
        calls = load_json(CALLS_PATH, [])
        calls.append(record)
        if len(calls) > 500:
            calls = calls[-500:]
        save_json(CALLS_PATH, calls)


def log_call(number, name, note, teacher, phrase):
    try:
        line = time.strftime("%Y-%m-%d %H:%M:%S") + " | " + \
               json.dumps({"number": number, "name": name, "note": note,
                           "teacher": teacher, "phrase": phrase}, ensure_ascii=False)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TTS（调用 Windows 内置 SAPI，无需额外依赖）与提示音
# ---------------------------------------------------------------------------
EDGE_VOICE = "zh-CN-XiaoxiaoNeural"


def speak_sapi(text):
    """使用 Windows 内置 SAPI 语音引擎朗读（优先选择中文语音，语速略慢更清晰）。"""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1;"
        "if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) };"
        "$s.Rate = -1;"
        "$t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'));"
        "$s.Speak($t)" % b64
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        creationflags=creationflags,
        timeout=180,
        check=False,
    )


def play_mp3_mci(path):
    """用 Windows MCI 播放 mp3（stdcall winmm，无额外依赖）。"""
    import ctypes
    mci = ctypes.windll.winmm
    mci.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    mci.mciSendStringW.restype = ctypes.c_int
    alias = "cqt" + uuid.uuid4().hex[:6]
    if mci.mciSendStringW('open "%s" type mpegvideo alias %s' % (path.replace('"', ''), alias),
                          None, 0, None) != 0:
        raise RuntimeError("无法打开音频文件")
    try:
        mci.mciSendStringW("play %s wait" % alias, None, 0, None)
    finally:
        mci.mciSendStringW("close %s" % alias, None, 0, None)


def speak_edge(text):
    """使用 edge-tts（微软晓晓自然语音）合成并播放，需联网。"""
    import asyncio
    import tempfile
    import edge_tts
    path = os.path.join(tempfile.gettempdir(), "cqt_tts_%s.mp3" % uuid.uuid4().hex)

    async def gen():
        communicate = edge_tts.Communicate(text, voice=EDGE_VOICE, rate="-4%")
        await communicate.save(path)

    try:
        asyncio.run(asyncio.wait_for(gen(), timeout=25))
        play_mp3_mci(path)
    finally:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


def _prepare_comtypes_env():
    """让 comtypes 把自动生成的代码写入应用 data 目录，避免对系统目录的写权限要求。"""
    import sys
    import types
    import comtypes
    gen_dir = os.path.join(DATA_DIR, "comtypes_gen")
    os.makedirs(gen_dir, exist_ok=True)
    if "comtypes.gen" not in sys.modules:
        mod = types.ModuleType("comtypes.gen")
        mod.__path__ = [gen_dir]
        sys.modules["comtypes.gen"] = mod
        comtypes.gen = mod
    else:
        mod = sys.modules["comtypes.gen"]
        paths = list(getattr(mod, "__path__", []))
        if gen_dir not in paths:
            paths.insert(0, gen_dir)
            mod.__path__ = paths
    return gen_dir


def speak_comtypes_sapi(text):
    """离线语音：通过开源库 comtypes 调用 Windows SAPI（同步朗读）。"""
    _prepare_comtypes_env()
    import comtypes.client
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    tokens = voice.GetVoices()
    target = None
    if tokens and tokens.Count:
        for i in range(tokens.Count):
            tok = tokens.Item(i)
            tid = str(tok.Id or "").lower()
            lang = str(tok.GetAttribute("Language") or "").lower()
            if "zh" in lang or "chinese" in tid or "huihui" in tid:
                target = tok
                break
        if target is None:
            target = tokens.Item(0)
        voice.Voice = target
    voice.Rate = -2
    voice.Speak(text, 0)  # 0 = SVSFDefault，同步朗读


def speak_text(text, cfg):
    """按配置选择语音引擎：优先开源自然语音，失败回退开源离线语音，再回退系统 SAPI。"""
    engine = str(cfg.get("tts_engine", "auto"))
    if engine in ("auto", "edge"):
        try:
            speak_edge(text)
            return
        except Exception as exc:
            print("[TTS] 自然语音合成失败，改用离线语音:", exc)
    if engine in ("auto", "comtypes", "sapi", "pyttsx3"):
        try:
            speak_comtypes_sapi(text)
            return
        except Exception as exc:
            print("[TTS] comtypes SAPI 失败，回退 PowerShell SAPI:", exc)
    speak_sapi(text)


# ---------------------------------------------------------------------------
# 系统音量（叫号时切换到设置音量，播完还原）
# ---------------------------------------------------------------------------
def set_system_volume(percent):
    """把系统主音量设为 percent(0-100)，返回之前的音量(0-100)；失败返回 None。

    在子进程中执行并加超时：即使音频设备异常也不会卡住主程序。
    """
    try:
        code = (
            "from pycaw.utils import AudioUtilities\n"
            "d=AudioUtilities.GetSpeakers()\n"
            "prev=d.volume_percent\n"
            "d.volume_percent=" + str(int(percent)) + "\n"
            "print(repr(prev))\n"
        )
        exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if not os.path.isfile(exe):
            exe = sys.executable
        proc = subprocess.run([exe, "-c", code], capture_output=True, text=True, timeout=8)
        if proc.returncode != 0:
            print("[音量] 子进程返回错误:", proc.stderr.strip()[:200])
            return None
        prev = float(proc.stdout.strip())
        return prev
    except Exception as exc:
        print("[音量] 切换失败:", exc)
        return None


def restore_volume(prev):
    if prev is None:
        return
    try:
        set_system_volume(prev)
    except Exception as exc:
        print("[音量] 还原失败:", exc)


# ---------------------------------------------------------------------------
# 开机自启
# ---------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_RUN_NAME = "班级叫号器"
TASK_NAME = "班级叫号器"


def _is_admin():
    """当前进程是否拥有管理员权限。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True


def _relaunch_as_admin():
    """以管理员身份重新拉起自己（弹一次 UAC）；成功返回 True。"""
    import ctypes
    try:
        exe = sys.executable
        args = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + list(sys.argv[1:]))
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, os.getcwd(), 1)
        return int(rc) > 32
    except Exception as exc:
        print("[提权] 申请管理员失败:", exc)
        return False


def ensure_admin():
    """启动时自动申请管理员权限；已提权或用户拒绝时直接继续。"""
    try:
        if not get_config().get("require_admin", False):
            return
    except Exception:
        pass
    if _is_admin():
        return
    print("[提权] 当前不是管理员，正在申请…")
    if _relaunch_as_admin():
        os._exit(0)  # 已拉起提权实例，本进程退出
    print("[提权] 未获得管理员权限，继续以普通权限运行")


def _task_target():
    """自启命令：优先用隐藏启动器（它每次读 version.json，OTA 后仍正确）。"""
    vbs = os.path.join(ROOT_DIR, "启动-隐藏窗口.vbs")
    if os.path.isfile(vbs):
        return "wscript.exe", vbs
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(exe):
        exe = sys.executable
    return exe, os.path.abspath(__file__)


def _schtasks(*args):
    try:
        return subprocess.run(
            ["schtasks"] + list(args), capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000,
            check=False)
    except Exception as exc:
        print("[自启] schtasks 调用失败:", exc)
        return None


def install_autostart_task():
    """用计划任务做“开机自启 + 最高权限”，登录时不再弹 UAC。需要管理员。"""
    if not _is_admin():
        return False
    prog, arg = _task_target()
    tr = '"%s" "%s"' % (prog, arg)
    r = _schtasks("/Create", "/TN", TASK_NAME, "/TR", tr,
                  "/SC", "ONLOGON", "/RL", "HIGHEST", "/F")
    if r is not None and r.returncode == 0:
        print("[自启] 已创建管理员计划任务：%s" % TASK_NAME)
        return True
    if r is not None:
        print("[自启] 计划任务创建失败:", (r.stderr or r.stdout or "").strip()[:200])
    return False


def remove_autostart_task():
    _schtasks("/Delete", "/TN", TASK_NAME, "/F")


def _registry_autostart(enabled):
    """旧方式（注册表 Run）：没有管理员权限时的退路。"""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                prog, arg = _task_target()
                winreg.SetValueEx(key, APP_RUN_NAME, 0, winreg.REG_SZ,
                                  '"%s" "%s"' % (prog, arg))
            else:
                try:
                    winreg.DeleteValue(key, APP_RUN_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as exc:
        print("[自启] 注册表设置失败:", exc)
        return False


def apply_autostart(enabled):
    """开机自启：有管理员权限时用计划任务（最高权限、无 UAC 弹窗），否则退回注册表。"""
    try:
        if enabled:
            if install_autostart_task():
                _registry_autostart(False)   # 避免注册表和任务同时拉起
                return True
            return _registry_autostart(True)
        remove_autostart_task()
        return _registry_autostart(False)
    except Exception as exc:
        print("[自启] 设置失败:", exc)
        return False


def play_prompt(cfg):
    """在叫号前播放一次提示音。"""
    if not HAS_WINSOUND or not cfg.get("prompt_enabled"):
        return
    sound = cfg.get("prompt_sound", "beep")
    if sound == "custom":
        fname = cfg.get("custom_sound_file", "")
        path = os.path.join(SOUNDS_DIR, fname) if fname else ""
        if path and os.path.isfile(path):
            try:
                winsound.PlaySound(path, winsound.SND_FILENAME)
                return
            except Exception:
                pass
    if sound in ("beep", ""):
        try:
            winsound.Beep(880, 180)
        except Exception:
            pass


# 播放队列：叫号请求快速返回，实际语音按顺序播放
_play_queue = queue.Queue()


def playback_worker():
    while True:
        item = _play_queue.get()
        prev_vol = None
        try:
            cfg = get_config()
            if cfg.get("volume_enabled", True):
                try:
                    prev_vol = set_system_volume(int(cfg.get("volume", 100)))
                except Exception as exc:
                    print("[音量] 切换失败:", exc)
            play_prompt(cfg)
            record = item.get("record")
            if _desktop is not None and record:
                try:
                    if record.get("kind") == "message":
                        _desktop.show_message_popup(record)
                    else:
                        _desktop.show_popup(record)
                except Exception:
                    pass
            times = max(1, min(10, int(cfg.get("play_count", 1))))
            for _ in range(times):
                speak_text(item["phrase"], cfg)
        except Exception as exc:
            print("[TTS] 播放失败:", exc)
        finally:
            if prev_vol is not None:
                restore_volume(prev_vol)
            if _desktop is not None:
                try:
                    # 播完当前号，且没有下一个号时，关闭弹窗
                    if _play_queue.empty():
                        _desktop.close_popup()
                except Exception:
                    pass
            _play_queue.task_done()


threading.Thread(target=playback_worker, daemon=True, name="tts-worker").start()


# ---------------------------------------------------------------------------
# mDNS：让局域网设备通过 http://主机名.local:端口 访问
# ---------------------------------------------------------------------------
def get_lan_ips():
    """本机可用于局域网访问的 IPv4 列表（真实网卡优先，主网卡排最前）。

    用 netif（GetAdaptersAddresses）取网卡，排除虚拟网卡（Hyper-V / WSL /
    Wi-Fi Direct 等）和 169.254 地址；取不到再退回默认路由 + 主机名解析。
    """
    out = []
    try:
        for it in netif.usable_adapters():
            for ip in it.get("ips", []):
                if ip not in out:
                    out.append(ip)
    except Exception as exc:
        print("[网卡] 读取网卡失败:", exc)
    if out:
        return out
    ip = netif.default_route_ip()
    if ip:
        out.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip2 = info[4][0]
            if netif.ip_ok(ip2) and ip2 not in out:
                out.append(ip2)
    except Exception:
        pass
    return out


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000


def get_network_interfaces():
    """枚举网卡（用于设置界面选择 mDNS 绑定网卡，以及诊断显示）。

    返回 [{id, name, ips, virtual, usable, gateway, desc, reason}]，
    id/name 用 Windows 网卡别名（如 以太网 / WLAN）。虚拟网卡会标注出来，
    以免老师误选一块手机根本连不上的网卡。
    """
    interfaces = []
    try:
        for it in netif.enum_adapters():
            ips = it.get("ips") or it.get("all_ips") or []
            if not ips:
                continue
            alias = it.get("alias") or ("网卡%s" % it.get("index"))
            if it.get("virtual"):
                tag = "（虚拟网卡）"
            elif not it.get("usable"):
                tag = "（%s）" % (it.get("reason") or "不可用")
            elif not it.get("gateway"):
                tag = "（无默认网关）"
            else:
                tag = ""
            interfaces.append({
                "id": alias,
                "name": alias + tag,
                "ips": ips,
                "virtual": bool(it.get("virtual")),
                "usable": bool(it.get("usable")),
                "gateway": bool(it.get("gateway")),
                "desc": it.get("desc") or "",
                "reason": it.get("reason") or "",
            })
    except Exception as exc:
        print("[网卡] 枚举网卡失败:", exc)
    return interfaces


def _mdns_ip_ok(ip):
    """mDNS 广播时该 IP 有没有意义（内部走 netif 的判定规则）。"""
    return netif.ip_ok(ip)


def _mdns_domain(hostname, n):
    """第 n 块网卡用哪个域名：主机名、主机名-2、主机名-3……

    绝不让两块网卡共用同一个域名 —— zeroconf 会把同域名（server）的 A 记录
    合并成一条记录集，那样手机上就会拿到"一半能连、一半连不上"的地址列表。
    """
    hostname = normalize_hostname(hostname)
    return hostname if n <= 1 else "%s-%d" % (hostname, n)


def get_mdns_plan(cfg):
    """生成 mDNS 广播计划：[{hostname, ips, iface, primary, virtual}]。

    每块**真实**网卡一个独立域名，并且每个域名只带 1 个 IP。
    虚拟网卡（Hyper-V / WSL / Wi-Fi Direct / VPN …）一律不广播。
    """
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    wanted = str(cfg.get("mdns_interface", "") or "").strip()
    try:
        adapters = netif.enum_adapters()
    except Exception as exc:
        print("[mDNS] 枚举网卡失败:", exc)
        adapters = []

    usable = [a for a in adapters if a.get("usable") and a.get("ips")]
    if not usable:
        ip = netif.default_route_ip()
        if netif.ip_ok(ip):
            print("[mDNS] 没有识别到可用真实网卡，退回默认路由地址 %s" % ip)
            return [{"hostname": hostname, "ips": [ip], "iface": "默认路由",
                     "primary": True, "virtual": False}]
        return []

    if wanted and wanted.lower() not in ("auto", "自动", "全部"):
        picked = [a for a in usable
                  if wanted in (a.get("alias") or "", a.get("desc") or "")]
        if picked:
            usable = picked
        else:
            print("[mDNS] 设置里指定的网卡「%s」不存在，改用自动选择" % wanted)

    plan = []
    n = 0
    for a in usable:
        ips = [ip for ip in a.get("ips", []) if netif.ip_ok(ip)]
        if not ips:
            continue
        n += 1
        plan.append({"hostname": _mdns_domain(hostname, n), "ips": ips[:1],
                     "iface": a.get("alias") or "-", "primary": n == 1,
                     "virtual": bool(a.get("virtual"))})
    return plan


def get_mdns_targets(cfg):
    """按网卡返回分组信息（设置界面、自检、诊断用）。"""
    out = []
    for p in get_mdns_plan(cfg):
        out.append({"id": p["iface"], "name": p["iface"], "ips": p["ips"],
                    "virtual": p["virtual"], "hostname": p["hostname"],
                    "primary": p["primary"]})
    return out


def get_mdns_ips(cfg):
    """mDNS 实际广播出去的 IP（所有域名合并、去重，主网卡在最前）。"""
    out = []
    for p in get_mdns_plan(cfg):
        for ip in p["ips"]:
            if ip not in out:
                out.append(ip)
    return out


def normalize_hostname(name):
    name = (name or "").strip().lower()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    name = "".join(ch for ch in name if ch in allowed)
    return name or socket.gethostname().lower()


_MDNS_STATE = {
    "ok": False,        # 最近一次注册是否成功
    "error": "",        # 失败原因（网页直接显示给老师看）
    "solution": "",     # 对应的解决办法
    "at": 0.0,          # 最近一次尝试的时间
    "plan": [],         # [{hostname, ips, iface, primary}]
    "names": [],        # 实际注册上去的服务名（可能因撞名被自动加了后缀）
}
_MDNS_STOP = threading.Event()
_MDNS_WATCHER = None
_MDNS_WATCH_INTERVAL = 8.0     # 每 8 秒看一眼网卡有没有变化（换网/插拔/Wi-Fi 连上）


def mdns_status():
    """mDNS 状态汇总（设置界面、启动自检、诊断接口共用，不做耗时解析）。"""
    cfg = get_config()
    port = int(_running_port or cfg.get("port", 8000))
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    plan = list(_MDNS_STATE.get("plan") or [])
    st = {
        "ok": bool(_MDNS_STATE.get("ok")),
        "enabled": bool(cfg.get("mdns_enabled")),
        "error": _MDNS_STATE.get("error") or "",
        "solution": _MDNS_STATE.get("solution") or "",
        "at": _MDNS_STATE.get("at") or 0,
        "at_text": (time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(_MDNS_STATE["at"]))
                    if _MDNS_STATE.get("at") else ""),
        "zeroconf_ok": HAS_ZEROCONF,
        "hostname": hostname,
        "port": port,
        "plan": plan,
        "names": list(_MDNS_STATE.get("names") or []),
        "urls": ["http://%s.local:%d" % (p.get("hostname") or hostname, port) for p in plan],
        "lan_ips": get_lan_ips(),
    }
    return st


def mdns_diagnose():
    """完整诊断：网卡判定（含每块网卡被跳过的原因）+ 注册计划 + 本机解析结果。"""
    cfg = get_config()
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    adapters = []
    try:
        adapters = netif.enum_adapters(force=True)
    except Exception as exc:
        print("[mDNS] 诊断时枚举网卡失败:", exc)
    plan = get_mdns_plan(cfg)
    resolved = []
    for p in plan:
        ip = _resolve_mdns(p["hostname"])
        resolved.append({"host": "%s.local" % p["hostname"], "ip": ip or "",
                         "ok": bool(ip and ip in p["ips"])})
    hints = []
    if not HAS_ZEROCONF:
        hints.append("缺少 zeroconf 组件，mDNS 不可用")
    if not plan:
        hints.append("没有可广播的网卡：请确认网线/Wi-Fi 已连接并且拿到了 IP")
    for a in adapters:
        if a.get("usable") and not a.get("gateway"):
            hints.append("网卡「%s」没有默认网关，通常说明这块网卡没接到路由器" % a.get("alias"))
    if hostname.lower() == socket.gethostname().lower():
        hints.append("主机名与 Windows 计算机名相同，可能与系统自带的网络发现服务抢名字，"
                     "建议改成别的名字（例如 %s-cls）" % hostname)
    if plan and not any(r["ok"] for r in resolved):
        hints.append("本机暂时解析不到自己的 .local 名字：其它设备一般仍可访问，"
                     "若手机也打不开，多半是路由器屏蔽了 mDNS 多播（可用 IP 直连）")
    return {
        "ok": True,
        "status": mdns_status(),
        "adapters": [{
            "alias": a.get("alias"), "desc": a.get("desc"), "ips": a.get("ips"),
            "all_ips": a.get("all_ips"), "gateway": a.get("gateway"),
            "usable": a.get("usable"), "virtual": a.get("virtual"),
            "reason": a.get("reason"), "iftype": a.get("iftype"),
        } for a in adapters],
        "adapters_text": netif.describe(adapters),
        "plan": plan,
        "resolved": resolved,
        "hints": hints,
        "computer_name": socket.gethostname(),
        "default_route_ip": netif.default_route_ip(),
        "log_file": runlog.log_file_path(),
    }


def register_mdns(cfg, force=False):
    """注册（或更新）mDNS 服务：**一个域名只带一块网卡的地址**。

    重复调用时状态没变就直接返回；变了就 update_service / 重新注册，避免
    "注销后立刻注册同名服务"被自身缓存挡住（NonUniqueNameException）。
    """
    global _mdns_zc, _mdns_infos
    if not HAS_ZEROCONF:
        _MDNS_STATE.update({"ok": False, "at": time.time(), "plan": [], "names": [],
                            "error": "缺少 zeroconf 组件，mDNS 无法使用",
                            "solution": "请使用完整版软件（内含 mDNS 组件）"})
        return False
    with MDNS_LOCK:
        try:
            if not cfg.get("mdns_enabled"):
                for info in _mdns_infos:
                    try:
                        _mdns_zc.unregister_service(info)
                    except Exception:
                        pass
                _mdns_infos = []
                register_mdns._key = None
                _MDNS_STATE.update({"ok": False, "error": "", "solution": "",
                                    "at": time.time(), "plan": [], "names": []})
                return False

            port = int(_running_port or cfg.get("port", 8000))
            secure_port = int(_running_secure_port or cfg.get("secure_port", 9443))
            secure_on = bool(cfg.get("secure_enabled", True))
            plan = get_mdns_plan(cfg)
            if not plan:
                msg = "没有可广播的局域网地址（网卡未连接或没拿到 IPv4）"
                print("[mDNS] " + msg)
                _MDNS_STATE.update({
                    "ok": False, "error": msg, "at": time.time(),
                    "solution": "确认网线/Wi-Fi 已连接、能上网；或在设置里换一块网卡后重启软件",
                    "plan": [], "names": []})
                return False

            # ---- 期望的服务表：name -> (type, addrs, port, hostname, iface, ips)
            wanted = {}
            for p in plan:
                try:
                    addrs = [socket.inet_aton(ip) for ip in p["ips"]]
                except Exception:
                    addrs = []
                if not addrs:
                    continue
                tag = " (%s)" % p["hostname"]     # 实例名带域名：同一局域网多台电脑不会撞名
                wanted["班级叫号器%s._http._tcp.local." % tag] = (
                    "_http._tcp.local.", addrs, port, p["hostname"], p["iface"], p["ips"])
                if secure_on:
                    wanted["班级叫号器%s-安全._https._tcp.local." % tag] = (
                        "_https._tcp.local.", addrs, secure_port, p["hostname"],
                        p["iface"], p["ips"])
            if not wanted:
                _MDNS_STATE.update({"ok": False, "at": time.time(), "plan": plan, "names": [],
                                    "error": "网卡地址无效，无法广播",
                                    "solution": "在设置里更换网卡或重启软件"})
                return False

            # ---- 状态完全没变就不动（最常见：向导里连续保存两次）
            state_key = (secure_on, secure_port, tuple(sorted(
                (n, t, tuple(a), sp) for n, (t, a, sp, _h, _i, _ips) in wanted.items())))
            if (not force and _mdns_infos
                    and getattr(register_mdns, "_key", None) == state_key):
                return True

            # ---- 响应器：只绑定真实网卡（虚拟网卡不参与收发）
            ifaces = []
            for p in plan:
                for ip in p["ips"]:
                    if ip not in ifaces:
                        ifaces.append(ip)
            if _mdns_zc is None:
                try:
                    _mdns_zc = Zeroconf(interfaces=ifaces,
                                        ip_version=(IPVersion.V4Only if IPVersion else None))
                except Exception as exc:
                    print("[mDNS] 按网卡创建响应器失败（%r），改用系统默认" % (exc,))
                    _mdns_zc = Zeroconf()
                register_mdns._ifaces = ifaces
            elif getattr(register_mdns, "_ifaces", None) != ifaces:
                try:
                    _mdns_zc.update_interfaces(
                        interfaces=ifaces,
                        ip_version=(IPVersion.V4Only if IPVersion else None))
                    register_mdns._ifaces = ifaces
                    print("[mDNS] 网卡地址变化，已重新绑定: %s" % (", ".join(ifaces) or "-"))
                except Exception as exc:
                    print("[mDNS] 重新绑定网卡失败（%r），重建响应器" % (exc,))
                    try:
                        _mdns_zc.close()
                    except Exception:
                        pass
                    _mdns_zc = Zeroconf()
                    register_mdns._ifaces = ifaces
            zc = _mdns_zc

            # ---- 注销不再需要的
            keep = []
            for info in list(_mdns_infos):
                if info.name not in wanted:
                    try:
                        zc.unregister_service(info)
                    except Exception:
                        pass
                else:
                    keep.append(info)

            # ---- 更新 / 注册
            new_infos = []
            errors = []
            for name, (stype, addrs, sport, host_name, iface_name, ips) in wanted.items():
                server = "%s.local." % host_name
                existing = next((x for x in keep if x.name == name), None)
                if existing is not None:
                    try:
                        zc.update_service(ServiceInfo(
                            stype, name, addresses=addrs, port=sport,
                            properties={b"path": b"/"}, server=server))
                        new_infos.append(existing)
                        print("[mDNS] 已更新 %s  网卡=%s  IP=%s" % (name, iface_name,
                                                                 ", ".join(ips)))
                        continue
                    except Exception as exc:
                        print("[mDNS] 更新 %s 失败，改为重注册: %r" % (name, exc))
                info = ServiceInfo(stype, name, addresses=addrs, port=sport,
                                   properties={b"path": b"/", b"host": host_name.encode(),
                                               b"ver": _app_version().encode("utf-8", "ignore")},
                                   server=server)
                ok = False
                for attempt in (1, 2):
                    try:
                        # allow_name_change：万一和别的电脑撞名，自动改成 名字-2，
                        # 而不是整台电脑的 mDNS 直接注册失败
                        zc.register_service(info, allow_name_change=True)
                        ok = True
                        break
                    except Exception as exc:
                        kind = type(exc).__name__
                        if attempt == 1 and "NonUniqueName" in kind:
                            try:
                                zc.unregister_service(info)
                            except Exception:
                                pass
                            time.sleep(0.3)
                            continue
                        errors.append("%s：%s" % (name, exc))
                        print("[mDNS] 注册 %s 失败（%s）: %r" % (name, iface_name, exc))
                        break
                if ok:
                    new_infos.append(info)
                    print("[mDNS] 已注册 %s  网卡=%s  IP=%s" % (info.name, iface_name,
                                                             ", ".join(ips)))

            _mdns_infos = new_infos
            register_mdns._key = state_key
            ok = bool(new_infos)
            for p in plan:
                print("[mDNS] 手机访问地址：http://%s.local:%d（网卡 %s，IP %s）"
                      % (p["hostname"], port, p["iface"], ", ".join(p["ips"])))
            _MDNS_STATE.update({
                "ok": ok, "at": time.time(), "plan": plan,
                "names": [i.name for i in new_infos],
                "error": "" if ok else (errors[0] if errors else "mDNS 注册失败"),
                "solution": "" if ok else "到设置里点「mDNS 诊断」查看原因；"
                                          "手机也可直接用 http://IP:端口 访问",
            })
            return ok
        except Exception as exc:
            print("[mDNS] 注册失败（不影响叫号功能）:", exc)
            _MDNS_STATE.update({"ok": False, "at": time.time(),
                                "error": str(exc),
                                "solution": "到设置里点「mDNS 诊断」查看网卡情况；"
                                            "手机也可直接用 http://IP:端口 访问"})
            return False


def _mdns_watcher_loop():
    """盯着网卡变化：换网、插网线、Wi-Fi 连上、IP 变了 → 自动重新注册。

    以前只在启动时注册一次，所以"开机时 Wi-Fi 还没连上"或"中途换了网络"
    之后 mDNS 就一直不生效 —— 这也是"有时能解析有时不能"的原因之一。
    """
    last_sig = None
    while not _MDNS_STOP.wait(_MDNS_WATCH_INTERVAL):
        try:
            cfg = get_config()
            if not cfg.get("mdns_enabled"):
                last_sig = None
                continue
            sig = netif.signature()
            if sig == last_sig and _MDNS_STATE.get("ok"):
                continue
            if last_sig is not None and sig != last_sig:
                print("[mDNS] 检测到网络变化，重新注册…")
            ok = register_mdns(cfg, force=True)
            last_sig = sig if ok else None
        except Exception as exc:
            print("[mDNS] 监听线程异常:", exc)


def start_mdns_watcher():
    """启动 mDNS 监听线程（幂等）。"""
    global _MDNS_WATCHER
    if not HAS_ZEROCONF:
        return
    if _MDNS_WATCHER is not None and _MDNS_WATCHER.is_alive():
        return
    _MDNS_STOP.clear()
    _MDNS_WATCHER = threading.Thread(target=_mdns_watcher_loop, daemon=True,
                                     name="mdns-watcher")
    _MDNS_WATCHER.start()


def _excluded_tcp_ranges():
    """Windows 保留/排除的 TCP 端口段（Hyper-V、WSL、WinNAT 等会占用）。

    落在这些段里的端口绑定会报 WinError 10013，直接跳过可少走弯路。
    """
    ranges = []
    try:
        proc = subprocess.run(
            ["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000,
            check=False)
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                ranges.append((int(parts[0]), int(parts[1])))
    except Exception:
        pass
    return ranges


def _port_excluded(port, ranges):
    for lo, hi in ranges:
        if lo <= port <= hi:
            return True
    return False


def _net_service(action, name):
    """停止/启动一个 Windows 服务（需要管理员）。"""
    try:
        r = subprocess.run(["net", action, name], capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000,
                           check=False)
        return r.returncode == 0
    except Exception as exc:
        print("[HTTPS] %s %s 失败: %s" % (action, name, exc))
        return False


def _release_reserved_port(port):
    """临时释放被 WinNAT 动态保留的端口（停 winnat -> 端口可用 -> 绑完再启动）。

    返回 (是否已释放, 需要恢复的服务名或 None)。
    注意：Hyper-V/WSL2 的 NAT 网络会短暂中断几秒。
    """
    if not _is_admin():
        return False, None
    if not _port_excluded(port, _excluded_tcp_ranges()):
        return True, None
    for svc in ("winnat", "hns"):
        if not _net_service("stop", svc):
            continue
        time.sleep(1.0)
        if not _port_excluded(port, _excluded_tcp_ranges()):
            print("[HTTPS] 已临时释放端口 %s（暂时停止 %s）" % (port, svc))
            return True, svc
        _net_service("start", svc)  # 没释放成功，立刻恢复
    return False, None


def _restore_net_service(name):
    if name:
        _net_service("start", name)


def _cert_desired_names():
    """证书里应该包含的名字：电脑名 + 设置的主机名（含 .local 形式）。"""
    names = {"localhost", socket.gethostname().lower()}
    names.add(socket.gethostname().lower() + ".local")
    try:
        host = normalize_hostname(get_config().get("mdns_hostname", "") or "")
    except Exception:
        host = ""
    if host:
        names.add(host)
        names.add(host + ".local")
    return sorted(n for n in names if n)


CA_CERT_PATH = os.path.join(DATA_DIR, "ca.crt")   # 本机根证书（手机装这个）
CA_KEY_PATH = os.path.join(DATA_DIR, "ca.key")


def _cert_desired_ips():
    """证书里应该包含的 IP：本机所有局域网 IP + 127.0.0.1。

    二维码/接入地址用的是 IP，所以证书里必须有 IP，否则 iPhone 打开
    https://IP:9443 会报"证书名称不匹配"（这个错误不像"不受信任"那样能点继续）。
    """
    ips = set()
    try:
        for ip in get_lan_ips():
            ips.add(ip)
    except Exception:
        pass
    ips.add("127.0.0.1")
    return sorted(ips)


def _load_cert(cert_path):
    from cryptography import x509
    with open(cert_path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


def _ensure_ca():
    """确保存在一份稳定的「本机根证书」。返回 (cert, key)。"""
    import datetime as _dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    try:
        if os.path.isfile(CA_CERT_PATH) and os.path.isfile(CA_KEY_PATH):
            with open(CA_KEY_PATH, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
            return _load_cert(CA_CERT_PATH), key
    except Exception as exc:
        print("[HTTPS] 现有根证书读取失败，重新生成:", exc)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "班级叫号器"),
        x509.NameAttribute(NameOID.COMMON_NAME,
                           "班级叫号器本机根证书 %s" % (socket.gethostname() or "").upper()),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CA_KEY_PATH, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(CA_CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    print("[HTTPS] 已生成根证书（手机装一次即可长期信任）：%s" % CA_CERT_PATH)
    return cert, key


def _cert_covers(cert_path, names, ips=None):
    """现有证书是否覆盖全部名字 + 当前局域网 IP，且由本机根证书签发。"""
    try:
        from cryptography import x509
        cert = _load_cert(cert_path)
        try:
            ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        except x509.ExtensionNotFound:
            return False
        have = {str(n).lower() for n in ext.value.get_values_for_type(x509.DNSName)}
        if not all(n.lower() in have for n in names):
            return False
        if ips:
            import ipaddress as _ip
            have_ip = set()
            for v in ext.value.get_values_for_type(x509.IPAddress):
                try:
                    have_ip.add(str(_ip.ip_address(v)))
                except Exception:
                    pass
            if not all(ip in have_ip for ip in ips):
                return False
        if os.path.isfile(CA_CERT_PATH):
            ca = _load_cert(CA_CERT_PATH)
            if cert.issuer != ca.subject:      # 换过根证书 → 手机上的信任会失效，必须重签
                return False
        return True
    except Exception:
        return False


def _cert_needs_renew(cert_path):
    """iOS 兼容性体检：有效期过长 / 缺 serverAuth / 缺 SAN 都算不合格。"""
    try:
        from cryptography import x509
        cert = _load_cert(cert_path)
        try:
            span = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
        except Exception:
            span = (cert.not_valid_after - cert.not_valid_before).days
        if span > 825:
            print("[HTTPS] 现有证书有效期 %d 天，超过 iOS 允许的 825 天，重新签发" % span)
            return True
        try:
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            if x509.oid.ExtendedKeyUsageOID.SERVER_AUTH not in eku:
                print("[HTTPS] 现有证书缺少 serverAuth 用途，重新签发")
                return True
        except x509.ExtensionNotFound:
            print("[HTTPS] 现有证书缺少 ExtendedKeyUsage，重新签发")
            return True
        try:
            cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        except x509.ExtensionNotFound:
            print("[HTTPS] 现有证书缺少 SAN，重新签发")
            return True
        return False
    except Exception as exc:
        print("[HTTPS] 证书检查失败，按需重签:", exc)
        return True


def _write_self_signed_cert(cert_path, key_path, names):
    """用本机根证书签发服务器证书（DNS 名字 + 局域网 IP 都写进 SAN），并写入证书链。"""
    import datetime as _dt
    import ipaddress as _ip
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    ca_cert, ca_key = _ensure_ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = [x509.DNSName(n) for n in names]
    for ip in _cert_desired_ips():
        try:
            sans.append(x509.IPAddress(_ip.ip_address(ip)))
        except Exception:
            pass
    cn = names[0] if names else socket.gethostname().lower()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    # 有效期 ≤825 天：iOS/iPadOS 与新版 macOS 会直接拒绝过长的服务器证书
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))   # 带上链


def refresh_cert_if_needed():
    """主机名/局域网 IP 变了就重签证书。返回 True 表示证书已更新（HTTPS 要重启才会用上）。"""
    cert_path = os.path.join(DATA_DIR, "ssl.crt")
    key_path = os.path.join(DATA_DIR, "ssl.key")
    names = _cert_desired_names()
    ips = _cert_desired_ips()
    if (os.path.isfile(cert_path) and os.path.isfile(key_path)
            and _cert_covers(cert_path, names, ips)):
        return False
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _write_self_signed_cert(cert_path, key_path, names)
        print("[HTTPS] 已重签证书：%s（IP: %s），重启后 https 生效"
              % (", ".join(names), ", ".join(ips)))
        return True
    except Exception as exc:
        print("[HTTPS] 重签证书失败:", exc)
        return False


def ensure_ssl_context():
    """生成/加载证书（本机根证书 + 服务器证书链），供 HTTPS 安全端口使用。"""
    cert_path = os.path.join(DATA_DIR, "ssl.crt")
    key_path = os.path.join(DATA_DIR, "ssl.key")
    names = _cert_desired_names()
    ips = _cert_desired_ips()
    try:
        _ensure_ca()
    except Exception as exc:
        print("[HTTPS] 生成根证书失败:", exc)
    # iOS/iPadOS 会拒绝有效期过长的证书（>825 天）；旧版本签的 10 年证书在
    # iPhone 上表现就是"Safari 打不开 / 服务器已停止响应"，这里体检并重签。
    if os.path.isfile(cert_path) and _cert_needs_renew(cert_path):
        for _f in (cert_path, key_path):
            try:
                os.remove(_f)
            except Exception:
                pass
    if not (os.path.isfile(cert_path) and os.path.isfile(key_path)):
        _write_self_signed_cert(cert_path, key_path, names)
    elif not _cert_covers(cert_path, names, ips):
        # 名字 / IP 变了，或还是旧的自签名证书 → 用根证书重签
        try:
            _write_self_signed_cert(cert_path, key_path, names)
            print("[HTTPS] 证书已更新（含当前 IP），https 重启后生效")
        except Exception as exc:
            print("[HTTPS] 补签证书失败（继续用旧证书）:", exc)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


@app.route("/cert")
def page_cert():
    """手机装证书引导页：iOS 白屏的根治办法（装根证书 + 开启完全信任）。"""
    cfg = get_config()
    return render_template(
        "cert.html", version=_app_version(),
        hostname=normalize_hostname(cfg.get("mdns_hostname", "")),
        lan_ips=get_lan_ips(),
        secure_port=int(_running_secure_port or cfg.get("secure_port", 9443)),
        port=int(_running_port or cfg.get("port", 8000)),
        has_ca=os.path.isfile(CA_CERT_PATH))


@app.route("/cert.crt")
def cert_crt():
    """下载本机根证书（.crt）。iOS/Android 都能装。"""
    try:
        if not os.path.isfile(CA_CERT_PATH):
            _ensure_ca()
        with open(CA_CERT_PATH, "rb") as f:
            data = f.read()
    except Exception as exc:
        return Response("根证书还没生成：%s" % exc, status=500,
                        mimetype="text/plain; charset=utf-8")
    resp = Response(data, mimetype="application/x-x509-ca-cert")
    resp.headers["Content-Disposition"] = 'attachment; filename="classcaller-ca.crt"'
    return resp


@app.route("/cert.mobileconfig")
def cert_mobileconfig():
    """下载 iOS/macOS 描述文件（.mobileconfig）：装上后在证书信任设置里开启完全信任。"""
    import plistlib
    import uuid as _uuid
    try:
        if not os.path.isfile(CA_CERT_PATH):
            _ensure_ca()
        with open(CA_CERT_PATH, "r", encoding="utf-8") as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
        payload = {
            "PayloadType": "Configuration",
            "PayloadVersion": 1,
            "PayloadIdentifier": "com.xiaoli.classcaller.rootca",
            "PayloadUUID": str(_uuid.uuid4()).upper(),
            "PayloadDisplayName": "班级叫号器 本机根证书",
            "PayloadOrganization": "班级叫号器",
            "PayloadDescription": "安装后在「设置 → 通用 → 关于本机 → 证书信任设置」里"
                                  "为「班级叫号器本机根证书」开启完全信任，手机访问就不会白屏。",
            "PayloadRemovalDisallowed": False,
            "PayloadContent": [{
                "PayloadType": "com.apple.security.root",
                "PayloadVersion": 1,
                "PayloadIdentifier": "com.xiaoli.classcaller.rootca.cert",
                "PayloadUUID": str(_uuid.uuid4()).upper(),
                "PayloadDisplayName": "班级叫号器 根证书",
                "PayloadCertificateFileName": "classcaller-ca.cer",
                "PayloadContent": der,
            }],
        }
        data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    except Exception as exc:
        return Response("生成描述文件失败：%s" % exc, status=500,
                        mimetype="text/plain; charset=utf-8")
    resp = Response(data, mimetype="application/x-apple-aspen-config")
    resp.headers["Content-Disposition"] =         'attachment; filename="classcaller-ca.mobileconfig"'
    return resp


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/settings/<key>")
def page_settings_section(key):
    """设置的四个板块，各是一个子页面。"""
    m = {"personal": "settings_personal.html", "network": "settings_network.html",
         "control": "settings_control.html", "tunnel": "settings_tunnel.html"}
    tpl = m.get(str(key or "").lower())
    if not tpl:
        return redirect(url_for("page_settings"))
    return render_template(tpl)


@app.route("/roster")
def page_roster():
    """名条管理子页面（含导入 CSV）。"""
    return render_template("roster.html")


@app.route("/settings")
def page_settings():
    """设置子页面（四分组，左上角返回，左边缘悬停出应用列表）。"""
    return render_template("settings.html")


@app.route("/monitor")
def page_monitor():
    return render_template("monitor.html")


@app.route("/remote")
def page_remote():
    return render_template("remote.html")


@app.route("/files")
def page_files():
    return render_template("files.html")


@app.route("/account")
def page_account():
    """我的账号：改密 / 退出登录（原来的密码管理页在这里）。"""
    return render_template("account.html")


@app.route("/admin")
def page_admin():
    """管理员后台：账号、在线设备、审计日志。"""
    # 全新安装（一个账号都没有）时后台是空的：先去登录页创建管理员，
    # 否则用户看到的是「账号管理 0 / 加载中…」，点「新建账号」也没反应。
    try:
        if accounts.count_users() == 0:
            return redirect("/login?next=" + urllib.parse.quote("/admin"))
    except Exception:
        pass
    return render_template("admin.html")


@app.route("/login")
def page_login():
    """统一登录页（公网访问与局域网都走这里）。"""
    return render_template("login.html")


# ---------------------------------------------------------------------------
# 公网访问（微软 Dev Tunnels）——只在电脑本机上可操作
# ---------------------------------------------------------------------------
def _tunnel_local_only():
    """隧道类操作只允许本机：公网口上的人不能自己开关隧道。"""
    try:
        if security.is_public_request() or not is_local():
            return jsonify({"ok": False,
                            "error": "隧道设置只能在电脑本机上操作"}), 403
    except Exception:
        return jsonify({"ok": False, "error": "无法确认本机身份"}), 403
    return None


@app.route("/api/tunnel/status")
def api_tunnel_status():
    err = check_access()
    if err:
        return err
    err = _tunnel_local_only()
    if err:
        return err
    cfg = get_config()
    st = tunnel.status()
    return jsonify({"ok": True, "tunnel": st,
                    "enabled": bool(cfg.get("public_enabled")),
                    "public_port": int(cfg.get("public_port", 9444) or 9444),
                    "autostart": bool(cfg.get("tunnel_autostart", True)),
                    "headed": bool(cfg.get("autologin_headed", True)),
                    "creds": creds.status()})


@app.route("/api/tunnel/login/start", methods=["POST"])
def api_tunnel_login_start():
    err = check_access()
    if err:
        return err
    err = _tunnel_local_only()
    if err:
        return err
    st = tunnel.login_start(log=lambda m: print(m))
    return jsonify({"ok": True, "tunnel": st})


@app.route("/api/tunnel/start", methods=["POST"])
def api_tunnel_start():
    err = check_access()
    if err:
        return err
    err = _tunnel_local_only()
    if err:
        return err
    cfg = get_config()
    data = request.get_json(silent=True) or {}
    # 开通公网前必须有账号：否则隧道一开，谁也登不进去（公网口强制登录）
    try:
        if accounts.count_users() == 0:
            return jsonify({"ok": False,
                            "error": "请先创建管理员账号（打开「我的账号」页），再开通公网访问"}), 400
    except Exception:
        pass
    tid = str(data.get("tunnel_id") or cfg.get("tunnel_id") or "").strip().lower()
    port = int(cfg.get("public_port", 9444) or 9444)
    _ids_cache_clear()
    st = tunnel.start(port, tid,
                      int(cfg.get("tunnel_expiration_hours", 720) or 720),
                      log=lambda m: print(m), prepare=False)
    if st.get("host_state") == "error":
        return jsonify({"ok": False, "error": st.get("last_error") or "启动失败",
                        "tunnel": st}), 400
    cfg["public_enabled"] = True
    if st.get("tunnel_id"):
        cfg["tunnel_id"] = st["tunnel_id"]
    save_config(cfg)
    return jsonify({"ok": True, "tunnel": st})


@app.route("/api/tunnel/stop", methods=["POST"])
def api_tunnel_stop():
    err = check_access()
    if err:
        return err
    err = _tunnel_local_only()
    if err:
        return err
    tunnel.stop(log=lambda m: print(m))
    cfg = get_config()
    cfg["public_enabled"] = False
    save_config(cfg)
    return jsonify({"ok": True, "tunnel": tunnel.status()})


@app.route("/update")
def page_update():
    return render_template("update.html")


# ---------------------------------------------------------------------------
# 首次使用向导：没设置完之前，所有页面都重定向到 /wizard；设置完就再也进不去
# ---------------------------------------------------------------------------
WIZARD_EXEMPT = ("/static/", "/api/wizard", "/wizard", "/favicon.ico",
                 "/api/auth/create", "/api/auth/passwords", "/api/auth/me", "/api/health",
                 "/activate", "/api/auth/activate_token", "/api/auth/qr_create",
                 # 向导期间手机也要能配对：配对页 + 手动输配对码；设置页也要能进
                 "/pair", "/api/auth/activate",
                 # 手机白屏排查用的极简页（不含脚本，最不容易被拦）
                 "/plain", "/ping", "/cert", "/manual")


def wizard_done():
    """是否已完成首次设置（看 data/state.json 里的 wizard_done 标记）。"""
    try:
        return bool(load_json(STATE_PATH, {}).get("wizard_done"))
    except Exception:
        return True          # 读不出来就不要把人锁在向导里


@app.before_request
def _wizard_gate():
    try:
        p = request.path or "/"
        if wizard_done():
            return None
        for pre in WIZARD_EXEMPT:
            if p.startswith(pre):
                return None
        from auth import is_local
        if not is_local():
            # 手机等其它设备：给一个会自动刷新的友好提示页，电脑那边设置完就能用
            if p.startswith("/api/"):
                return jsonify({"ok": False, "need_wizard": True,
                                "error": "请先在电脑上完成首次设置"}), 428
            cfg = get_config()
            host = normalize_hostname(cfg.get("mdns_hostname", "")) or "这台电脑"
            html = (
                "<!doctype html><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>请先在电脑上完成设置</title>"
                "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
                "justify-content:center;background:linear-gradient(160deg,#e0f2fe,#a7f3d0);"
                "font-family:'Microsoft YaHei',system-ui,sans-serif;color:#0f172a}"
                ".box{background:#fff;border-radius:20px;padding:28px 24px;max-width:440px;"
                "box-shadow:0 18px 50px rgba(15,23,42,.18);text-align:center}"
                "h1{font-size:20px;margin:0 0 10px}p{color:#475569;font-size:14.5px;line-height:1.8;margin:6px 0}"
                ".t{font-size:40px;margin-bottom:6px}code{background:#f1f5f9;padding:2px 6px;border-radius:6px}</style>"
                "<div class=box><div class=t>🖥️➡️📱</div>"
                "<h1>请先在电脑上完成首次设置</h1>"
                "<p>这台电脑（<b>%s</b>）还没有完成「首次设置向导」。"
                "请在电脑上打开叫号器，走完 7 步设置后，这台手机就能直接使用了。</p>"
                "<p>本页会<b>每 3 秒自动刷新</b>，设置完成就自动进来。</p></div>"
                "<script>setTimeout(function(){location.reload()},3000)</script>"
                % host)
            return (html, 200)
        if p.startswith("/api/"):
            return jsonify({"ok": False, "need_wizard": True,
                            "error": "请先完成首次设置向导"}), 428
        return redirect("/wizard")
    except Exception as exc:
        print("[向导] 拦截失败:", exc)
        return None


@app.route("/wizard")
def page_wizard():
    if wizard_done():
        return redirect("/")            # 设置完就不允许再回到向导
    return render_template("wizard.html")


@app.route("/pair")
def page_pair():
    """旧版配对页：已由统一登录取代，跳过去（老书签不至于 404）。"""
    return redirect("/login")


@app.route("/activate")
def page_activate():
    """旧版扫码激活页：已由统一登录取代，跳过去。"""
    return redirect("/login")


@app.route("/api/wizard/state")
def api_wizard_state():
    cfg = get_config()
    port = _running_port or cfg.get("port", 8000)
    host = normalize_hostname(cfg.get("mdns_hostname", ""))
    secure_enabled = bool(cfg.get("secure_enabled", True))
    secure_port = int(cfg.get("secure_port") or 0) or 9443
    return jsonify({
        "ok": True,
        "done": wizard_done(),
        "hostname": host,
        "port": port,
        "computer_name": os.environ.get("COMPUTERNAME", "") or "这台电脑",
        "local_ips": get_lan_ips(),      # 手机接入用 IP（安卓不解析 .local）
        "roster_count": len(get_roster()),
        "autostart": bool(cfg.get("autostart")),
        "update_channel": cfg.get("update_channel", "stable"),
        "update_policy": cfg.get("update_policy", "none"),
        # 手机端激活/监控必须走 https（浏览器只在安全上下文里给 crypto.subtle）
        "secure_enabled": secure_enabled,
        "secure_port": secure_port,
        # 向导进度（刷新/重启后回到原来那一步）
        "step": int(load_json(STATE_PATH, {}).get("wizard_step", 0) or 0),
        "license_agreed": bool(load_json(STATE_PATH, {}).get("license_agreed")),
        # 个性化设置（向导里可以恢复）
        "phrase": cfg.get("phrase", ""),
        "play_count": int(cfg.get("play_count", 1) or 1),
        "volume_enabled": bool(cfg.get("volume_enabled", True)),
        "volume": int(cfg.get("volume", 100) or 0),
        "prompt_enabled": bool(cfg.get("prompt_enabled", True)),
        "prompt_sound": cfg.get("prompt_sound", "beep"),
        "tts_engine": cfg.get("tts_engine", "auto"),
        "http_url": "http://%s:%d" % (host or "127.0.0.1", port),
        "https_url": ("https://%s:%d" % (host or "127.0.0.1", secure_port)
                      if secure_enabled else ""),
    })


@app.route("/api/wizard/step", methods=["POST"])
def api_wizard_step():
    """向导每点一次上/下一步：保存进度 + 这一步填过的配置（随时可中断、可刷新）。"""
    from auth import is_local
    if not is_local():
        return jsonify({"ok": False, "error": "只能在本机操作"}), 403
    data = request.get_json(silent=True) or {}
    try:
        step = int(data.get("step", 0))
    except Exception:
        step = 0
    step = max(0, min(step, 6))
    fields = data.get("fields") or {}
    cfg = get_config()
    changed = False
    need_mdns = False

    host = str(fields.get("mdns_hostname", "") or "").strip().lower()
    if host:
        host = "".join(ch for ch in host if ch in "abcdefghijklmnopqrstuvwxyz0123456789-")
        host = host.strip("-")
        if host and host != normalize_hostname(cfg.get("mdns_hostname", "")):
            cfg["mdns_hostname"] = host
            cfg["wizard_renamed_host"] = True
            cfg["mdns_enabled"] = True
            changed = True
            need_mdns = True

    if "autostart" in fields:
        want = bool(fields.get("autostart"))
        if want != bool(cfg.get("autostart")):
            cfg["autostart"] = want
            changed = True
            try:
                apply_autostart(want)
            except Exception as exc:
                print("[向导] 开机自启设置失败:", exc)

    ch = str(fields.get("update_channel", "") or "")
    if ch in ("stable", "beta", "dev") and ch != cfg.get("update_channel"):
        cfg["update_channel"] = ch
        changed = True
    pol = str(fields.get("update_policy", "") or "")
    if pol in ("none", "check", "download", "install") and pol != cfg.get("update_policy"):
        cfg["update_policy"] = pol
        changed = True

    # 个性化（向导/设置页共用同一套键）
    if "phrase" in fields:
        cfg["phrase"] = str(fields.get("phrase") or "")[:200]
        changed = True
    if "play_count" in fields:
        try:
            cfg["play_count"] = max(1, min(10, int(fields.get("play_count"))))
            changed = True
        except Exception:
            pass
    if "volume_enabled" in fields:
        cfg["volume_enabled"] = bool(fields.get("volume_enabled"))
        changed = True
    if "volume" in fields:
        try:
            cfg["volume"] = max(0, min(100, int(fields.get("volume"))))
            changed = True
        except Exception:
            pass
    if "prompt_enabled" in fields:
        cfg["prompt_enabled"] = bool(fields.get("prompt_enabled"))
        changed = True
    if "prompt_sound" in fields:
        ps = str(fields.get("prompt_sound") or "")
        if ps in ("none", "beep", "custom"):
            cfg["prompt_sound"] = ps
            changed = True
    if "tts_engine" in fields:
        te = str(fields.get("tts_engine") or "")
        if te in ("auto", "edge", "comtypes"):
            cfg["tts_engine"] = te
            changed = True

    if changed:
        save_config(cfg)
    if need_mdns:
        try:
            threading.Thread(target=lambda: register_mdns(get_config()), daemon=True).start()
        except Exception:
            pass

    roster = fields.get("roster")
    if isinstance(roster, list) and roster:
        try:
            save_roster(roster)
        except Exception as exc:
            print("[向导] 保存名条失败:", exc)

    st = load_json(STATE_PATH, {})
    st["wizard_step"] = step
    if fields.get("license_agreed") or data.get("license_agreed"):
        st["license_agreed"] = True
    st["wizard_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(STATE_PATH, st)
    print("[向导] 步骤已保存 step=%d license=%s config_changed=%s"
          % (step, bool(st.get("license_agreed")), changed))

    return jsonify({"ok": True, "step": step, "config_saved": changed,
                    "license_agreed": bool(st.get("license_agreed")),
                    "roster_count": len(get_roster())})


@app.route("/api/wizard/hostname", methods=["POST"])
def api_wizard_hostname():
    """向导第 3 步：设备名一填就保存并重新注册 mDNS（不用等到最后一步）。"""
    from auth import is_local
    if not is_local():
        return jsonify({"ok": False, "error": "只能在本机修改设备名"}), 403
    data = request.get_json(silent=True) or {}
    raw = str(data.get("mdns_hostname", "") or "").strip().lower()
    # 只允许字母、数字、连字符，且不能以连字符开头
    raw = "".join(ch for ch in raw if ch in "abcdefghijklmnopqrstuvwxyz0123456789-")
    raw = raw.strip("-")
    if not raw:
        return jsonify({"ok": False, "error": "设备名不能为空"}), 400
    if len(raw) > 24:
        raw = raw[:24]

    cfg = get_config()
    old_host = normalize_hostname(cfg.get("mdns_hostname", ""))
    changed = raw != old_host
    if changed:
        cfg["mdns_hostname"] = raw
        cfg["mdns_enabled"] = True
        save_config(cfg)
        try:
            register_mdns(get_config())     # mDNS 立刻换成新名字
        except Exception as exc:
            print("[向导] 应用主机名失败:", exc)
        refresh_cert_if_needed()            # 证书按新名字补签（https 要重启才用上）
        # 记下"改过名字"，供 /api/wizard/finish 判断要不要提示重启
        # （放在 config 里：state.json 会被后台线程读写，不可靠）
        try:
            cfg2 = get_config()
            cfg2["wizard_renamed_host"] = True
            save_config(cfg2)
        except Exception as exc:
            print("[向导] 记录改名状态失败:", exc)
    port = _running_port or cfg.get("port", 8000)
    return jsonify({"ok": True, "hostname": raw, "changed": changed,
                    "http_url": "http://%s.local:%d" % (raw, port),
                    "restart_needed": changed})


@app.route("/api/wizard/license")
def api_wizard_license():
    """给向导返回两份文本：中文全文译文 + GNU 英文原文（两个独立文件）。"""
    out = {"ok": True, "zh": "", "en": "", "legal": True}
    # 安装包里许可证在版本目录下（如 <安装目录>/2.0.36/LICENSE），所以要把这些位置都找一遍
    bases = [BASE_DIR, ROOT_DIR]
    try:
        for name in sorted(os.listdir(ROOT_DIR)):
            d = os.path.join(ROOT_DIR, name)
            if os.path.isdir(d) and re.match(r"^\d+\.\d+", name):
                bases.append(d)
    except Exception:
        pass
    try:
        for name in sorted(os.listdir(BASE_DIR)):
            d = os.path.join(BASE_DIR, name)
            if os.path.isdir(d) and re.match(r"^\d+\.\d+", name):
                bases.append(d)
    except Exception:
        pass
    for key, names in (("zh", ("LICENSE-中文全文译文.txt", "LICENSE-中文参考译文.txt")),
                       ("en", ("LICENSE", "COPYING"))):
        for name in names:
            for base in bases:
                p = os.path.join(base, name)
                if os.path.isfile(p):
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as f:
                            out[key] = f.read()
                    except Exception:
                        pass
                    break
            if out[key]:
                break
    return jsonify(out)


@app.route("/api/wizard/finish", methods=["POST"])
def api_wizard_finish():
    from auth import is_local
    if not is_local():
        return jsonify({"ok": False, "error": "只能在本机完成首次设置"}), 403
    data = request.get_json(silent=True) or {}
    cfg = get_config()
    need_mdns = False
    # 第 3 步可能已经保存过名字了，所以看"向导过程中是否改过名字"的标记，
    # 而不是拿当前 config 比（那会永远相等）
    if cfg.get("wizard_renamed_host"):
        host_changed = True
    else:
        orig_host = normalize_hostname(cfg.get("mdns_hostname", ""))
        host = normalize_hostname(str(data.get("mdns_hostname", "") or ""))
        host_changed = bool(host and host != orig_host)

    host = normalize_hostname(str(data.get("mdns_hostname", "") or ""))
    if host and host != normalize_hostname(cfg.get("mdns_hostname", "")):
        cfg["mdns_hostname"] = host
        need_mdns = True
    if data.get("mdns_enabled") and not cfg.get("mdns_enabled"):
        cfg["mdns_enabled"] = True
        need_mdns = True

    if "autostart" in data:
        want = bool(data.get("autostart"))
        if want != bool(cfg.get("autostart")):
            cfg["autostart"] = want
            try:
                apply_autostart(want)
            except Exception as exc:
                print("[向导] 设置开机自启失败:", exc)

    ch = str(data.get("update_channel", "") or "")
    if ch in ("stable", "beta", "dev"):
        cfg["update_channel"] = ch
    pol = str(data.get("update_policy", "") or "")
    if pol in ("none", "check", "download", "install"):
        cfg["update_policy"] = pol

    roster = data.get("roster") or []
    if isinstance(roster, list) and roster:
        clean = []
        for it in roster[:5000]:
            if not isinstance(it, dict):
                continue
            num = str(it.get("number", "")).strip()
            nm = str(it.get("name", "")).strip()
            if not num and not nm:
                continue
            clean.append({"number": num, "name": nm})
        if clean:
            save_roster(clean)

    save_config(cfg)
    cert_updated = False
    if need_mdns:
        try:
            register_mdns(get_config())     # mDNS 名字立即生效（http://新名字.local 马上能用）
        except Exception as exc:
            print("[向导] 应用主机名失败:", exc)
        cert_updated = refresh_cert_if_needed()   # 证书补上新名字，https 要重启才用上

    st = load_json(STATE_PATH, {"call_count": 0})
    st["wizard_done"] = True
    st["wizard_done_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(STATE_PATH, st)
    try:
        cfg3 = get_config()
        cfg3.pop("wizard_renamed_host", None)
        save_config(cfg3)
    except Exception:
        pass

    cfg2 = get_config()
    port = _running_port or cfg2.get("port", 8000)
    host2 = normalize_hostname(cfg2.get("mdns_hostname", ""))
    return jsonify({"ok": True, "hostname": host2, "port": port,
                    "http_url": "http://%s.local:%d" % (host2, port),
                    "secure_port": cfg2.get("secure_port", 0),
                    "host_changed": bool(host_changed),
                    "cert_updated": bool(cert_updated),
                    # 名字改了：手机上的 http 立刻可用，https（监控/文件/更新）要重启一次
                    "restart_needed": bool(host_changed)})


# ---------------------------------------------------------------------------
# 状态与配置 API
# ---------------------------------------------------------------------------
def _app_version():
    try:
        with open(os.path.join(ROOT_DIR, "version.json"), "r", encoding="utf-8") as f:
            return str(json.load(f).get("version", "2.0.0"))
    except Exception:
        return "2.0.0"


def app_edition():
    """当前版本类型：stable / beta / dev。稳定版隐藏主页的「查看日志」入口。"""
    try:
        with open(os.path.join(ROOT_DIR, "version.json"), "r", encoding="utf-8") as f:
            return str(json.load(f).get("edition", "stable") or "stable").strip().lower()
    except Exception:
        return "stable"


def _safe_host(text):
    """只保留主机名字符，防止把奇怪的东西塞进诊断页面。"""
    text = str(text or "")
    return "".join(ch for ch in text if ch.isalnum() or ch in ".:-[]")[:80]


@app.route("/api/abr/stats")
def api_abr_stats():
    """当前所有实时流的自适应码率状态（排障/自动化测试用）。

    返回每路流当前的档位（分辨率/质量/帧率）、在途帧数、帧 RTT、最小 RTT、
    实测吞吐、目标码率、降档/升档次数等。手机卡的时候看这里最直接。
    """
    err = check_access()
    if err:
        return err
    return jsonify({"ok": True, "streams": abr.snapshot(),
                    "abr_enabled": bool(get_config().get("abr_enabled", True))})


@app.route("/api/health")
def api_health():
    """启动自检 / 手机排障接口：告诉调用者"你到底连到了谁"。

    手机白屏时直接打开 http://IP:端口/api/health 看这段 JSON：
    能出 JSON 说明服务和网络都正常，白屏就是页面/证书/权限的问题。
    """
    return jsonify({
        "ok": True, "app": "班级叫号器", "version": _app_version(),
        "time": int(time.time()),
        "scheme": request.scheme,
        "host": _safe_host(request.host),
        "server_ip": _safe_host((request.host or "").split(":")[0]),
        "client_ip": _safe_host(request.remote_addr),
        "ua": (request.headers.get("User-Agent") or "")[:200],
    })


@app.route("/api/status")
def api_status():
    cfg = get_config()
    state = get_state()
    roster = get_roster()
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    mdns = mdns_status()
    return jsonify({
        "ok": True,
        "port": _running_port or cfg.get("port", 8000),
        "mdns_enabled": bool(cfg.get("mdns_enabled")),
        "mdns_hostname": hostname,
        "mdns_url": "http://%s.local:%d" % (hostname, _running_port or cfg.get("port", 8000)),
        "mdns_ok": mdns.get("ok"),
        "mdns_error": mdns.get("error") or "",
        "mdns_urls": mdns.get("urls") or [],
        "secure_enabled": bool(cfg.get("secure_enabled", True)),
        "secure_port": int(_running_secure_port or cfg.get("secure_port", 9443)),
        "secure_url": "https://%s.local:%d" % (hostname, int(_running_secure_port or cfg.get("secure_port", 9443))),
        "local_ips": get_lan_ips(),
        "roster_count": len(roster),
        "call_count": state.get("call_count", 0),
        "zeroconf_ok": HAS_ZEROCONF,
        "tts_ok": True,
        "edition": app_edition(),
        "version": _app_version(),
    })


@app.route("/ping")
def page_ping():
    """极简连通性测试（纯文本）：手机白屏时先测这个。"""
    return Response("OK %s %s\n" % (_app_version(), time.strftime("%Y-%m-%d %H:%M:%S")),
                    mimetype="text/plain; charset=utf-8")


@app.route("/plain")
def page_plain():
    """极简页面（不含 JS/CSS/图片），用来区分"连不上服务"和"页面渲染不出来"。"""
    cfg = get_config()
    port = int(_running_port or cfg.get("port", 8000))
    sp = int(_running_secure_port or cfg.get("secure_port", 9443))
    items = "".join("<li>http://%s:%d</li>" % (_safe_host(ip), port) for ip in get_lan_ips())
    html = (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>连接正常 - 班级叫号器</title>"
        "<style>body{font-family:'Microsoft YaHei',system-ui,sans-serif;padding:18px;"
        "line-height:1.9;color:#0f172a}h1{font-size:19px}code{background:#f1f5f9;"
        "padding:2px 5px;border-radius:5px}li{margin:2px 0}</style>"
        "<h1>✅ 已经连上这台电脑，网络和服务都正常</h1>"
        "<p>你访问的地址：<code>%s://%s</code></p>"
        "<p>你的 IP：<code>%s</code>　软件版本：<code>%s</code></p>"
        "<p>这台电脑的局域网地址：</p><ul>%s</ul>"
        "<p>如果<b>这一页能看到</b>，但主页白屏，说明是浏览器/证书/缓存的问题：<br>"
        "① <b>iPhone / iPad 请先到 <a href=/cert>/cert</a> 装一次证书</b>并开启「完全信任」；"
        "② 再试 <code>https://%s:%d</code>（主机名或 IP 都行）；"
        "③ 清一下浏览器缓存；④ 设置 → 隐私与安全性 → 本地网络，允许浏览器访问局域网。</p>"
        "<p>手机访问的每个地址、是否走名字解析，都能在 <a href=/api/health>/api/health</a> "
        "里看到（纯 JSON）；电脑端可开 <a href=/logs>/logs</a> 看运行日志。</p>"
        % (_safe_host(request.scheme), _safe_host(request.host),
           _safe_host(request.remote_addr), _app_version(), items,
           _safe_host(normalize_hostname(cfg.get("mdns_hostname", "")) or "主机名"), sp)
    )
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/logs")
def page_logs():
    """运行日志查看页。稳定版不在主页显示入口，但直接输网址仍可进入。"""
    return render_template("logs.html", edition=app_edition(), version=_app_version())


@app.route("/api/logs")
def api_logs():
    err = check_access()
    if err:
        return err
    try:
        after = int(request.args.get("after", "-1"))
    except Exception:
        after = -1
    try:
        limit = int(request.args.get("limit", "400"))
    except Exception:
        limit = 400
    data = runlog.snapshot(after=after, limit=limit,
                           keyword=request.args.get("q", ""),
                           level=request.args.get("level", ""))
    data["ok"] = True
    data["file"] = runlog.log_file_path()
    data["edition"] = app_edition()
    return jsonify(data)


@app.route("/api/logs/export")
def api_logs_export():
    """导出日志（UTF-8 BOM，记事本打开不乱码）。"""
    err = check_access()
    if err:
        return err
    try:
        text = runlog.export_text()
    except Exception as exc:
        text = "导出失败：%r" % (exc,)
    fn = "classcaller-log-%s.txt" % time.strftime("%Y%m%d-%H%M%S")
    resp = Response("\ufeff" + text, mimetype="text/plain; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        "attachment; filename=%s; filename*=UTF-8''%s"
        % (fn, urllib.parse.quote("班级叫号器-日志-%s.txt"
                                  % time.strftime("%Y%m%d-%H%M%S"))))
    return resp


# 说明书的位置：优先当前版本目录（随 OTA 一起下发），其次安装根目录（安装包放的）
MANUAL_SEARCH_DIRS = (BASE_DIR, ROOT_DIR)
_manual_cache = {"key": None, "html": ""}


def _manual_path(name, want_dir=False):
    """在版本目录 / 安装根目录里找说明书文件，找到就返回绝对路径。"""
    for d in MANUAL_SEARCH_DIRS:
        p = os.path.join(d, name)
        if (os.path.isdir(p) if want_dir else os.path.isfile(p)):
            return p
    return ""


def _manual_html_path():
    return _manual_path("使用说明-图文版.html")


def _manual_pdf_path():
    return _manual_path("使用说明-图文版.pdf")


def _manual_txt_path():
    return _manual_path("使用说明.txt")


def _manual_img_dir():
    return _manual_path("使用说明-图片", want_dir=True)


def _manual_html_text():
    """读图文说明书并把图片路径改成 /manual-img/（带缓存，文件更新后自动失效）。"""
    path = _manual_html_path()
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = (path, int(st.st_mtime), st.st_size)
        if _manual_cache["key"] == key and _manual_cache["html"]:
            return _manual_cache["html"]
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        html = (html.replace('src="使用说明-图片/', 'src="/manual-img/')
                    .replace("src='使用说明-图片/", "src='/manual-img/"))
        _manual_cache.update({"key": key, "html": html})
        return html
    except Exception:
        return ""


@app.route("/manual")
def page_manual():
    """图文使用说明（托盘「📖 使用说明」直接打开这里）。

    向下兼容：这份安装里没有说明书文件时（旧版本升级上来、或 OTA 只更新了代码目录），
    退回到一个同样有用的兜底页，而不是报错。
    """
    html = _manual_html_text()
    if html:
        return Response(html, mimetype="text/html; charset=utf-8")
    if _manual_pdf_path():
        return redirect("/manual.pdf")
    cfg = get_config()
    port = int(_running_port or cfg.get("port", 8000))
    sp = int(_running_secure_port or cfg.get("secure_port", 9443))
    urls = "".join("<li><code>http://%s:%d</code></li>" % (_safe_host(ip), port)
                   for ip in get_lan_ips())
    body = (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>使用说明 - 班级叫号器</title>"
        "<style>body{font-family:'Microsoft YaHei',system-ui,sans-serif;padding:20px;"
        "line-height:1.9;color:#0f172a;max-width:640px;margin:0 auto}"
        "h1{font-size:19px}h2{font-size:15.5px;margin-top:20px}code{background:#f1f5f9;"
        "padding:1px 5px;border-radius:5px}a{color:#1d4ed8}</style>"
        "<h1>📖 班级叫号器 使用说明</h1>"
        "<p>当前版本 <b>%s</b>。这份安装目录里没有放图文说明书文件"
        "（<code>使用说明-图文版.pdf</code> / <code>.html</code>），所以这里显示速查页。</p>"
        "<h2>叫号</h2><p>手机与电脑连同一个 Wi-Fi，用下面任一地址打开即可：</p><ul>%s</ul>"
        "<p>也可以：<code>http://主机名.local:%d</code>（iPhone / 电脑支持；"
        "部分安卓/鸿蒙不解析 .local，请用 IP）。</p>"
        "<h2>激活 / 监控 / 远控 / 文件传输</h2>"
        "<p>必须走安全端口：<code>https://本机IP:%d</code>；"
        "iPhone 白屏请先到 <a href='/cert'>/cert</a> 装一次证书并开启「完全信任」。</p>"
        "<h2>排障入口</h2><ul>"
        "<li><a href='/plain'>/plain</a>　极简页（白屏时先试它）</li>"
        "<li><a href='/api/health'>/api/health</a>　看连到的是哪台电脑</li>"
        "<li><a href='/cert'>/cert</a>　手机装证书（iOS 白屏根治）</li>"
        "<li><a href='/logs'>/logs</a>　运行日志（可导出）</li>"
        "<li><a href='/manual.txt'>使用说明.txt</a>　纯文字版</li>"
        "</ul>"
        "<p><a href='/'>返回主页</a></p>"
        % (_app_version(), urls, port, sp)
    )
    return Response(body, mimetype="text/html; charset=utf-8")


@app.route("/manual.pdf")
def page_manual_pdf():
    pdf = _manual_pdf_path()
    if pdf:
        try:
            return send_file(pdf, mimetype="application/pdf", as_attachment=False,
                             download_name="班级叫号器-使用说明.pdf")
        except Exception as exc:
            print("[说明] 发送 PDF 失败:", exc)
    return redirect("/manual")


@app.route("/manual.txt")
def page_manual_txt():
    txt = _manual_txt_path()
    if txt:
        try:
            return send_file(txt, mimetype="text/plain; charset=utf-8",
                             as_attachment=False)
        except Exception:
            pass
    return redirect("/manual")


@app.route("/manual-img/<path:name>")
def page_manual_img(name):
    """图文说明书的图片。"""
    img_dir = _manual_img_dir()
    if not img_dir:
        return Response("", status=404)
    try:
        return send_from_directory(img_dir, name, max_age=3600)
    except Exception:
        return Response("", status=404)


@app.route("/api/mdns/status")
def api_mdns_status():
    st = mdns_status()
    st["registered"] = st.pop("ok")
    st["ok"] = True          # 接口本身成功；是否注册成功看 registered
    return jsonify(st)


@app.route("/api/mdns/diag")
def api_mdns_diag():
    """mDNS 完整诊断：网卡判定 + 注册计划 + 本机解析 + 提示。"""
    err = check_access()
    if err:
        return err
    return jsonify(mdns_diagnose())


@app.route("/api/mdns/refresh", methods=["POST"])
def api_mdns_refresh():
    """重新注册一次 mDNS（设置页「立即重试」用）。"""
    err = check_access()
    if err:
        return err
    ok = register_mdns(get_config(), force=True)
    st = mdns_status()
    st["registered"] = bool(ok)
    st["ok"] = True
    return jsonify(st)


@app.route("/api/port_check")
def api_port_check():
    """检查端口能否作为 HTTP 端口（设置页保存前调用）。"""
    try:
        p = int(request.args.get("port", "0"))
    except Exception:
        return jsonify({"ok": False, "reason": "端口必须是数字"})
    if not (1 <= p <= 65535):
        return jsonify({"ok": False, "reason": "端口范围应为 1~65535"})
    try:
        if _port_excluded(p, _excluded_tcp_ranges()):
            return jsonify({"ok": False, "excluded": True,
                            "reason": "该端口落在 Windows 保留端口段里（Hyper-V/WSL 等占用）"})
    except Exception:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0", p))
        finally:
            sock.close()
    except OSError as exc:
        return jsonify({"ok": False, "busy": True,
                        "reason": "端口被占用或不可用（%s）" % (exc.strerror or exc)})
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    err = check_access()
    if err:
        return err
    if request.method == "GET":
        return jsonify({"ok": True, "config": get_config(),
                        "interfaces": get_network_interfaces()})
    data = request.get_json(silent=True) or {}
    cfg = get_config()
    old_host = normalize_hostname(cfg.get("mdns_hostname", ""))
    changed = False
    if "port" in data:
        try:
            cfg["port"] = max(1, min(65535, int(data["port"])))
        except Exception:
            pass
    if "mdns_enabled" in data:
        cfg["mdns_enabled"] = bool(data["mdns_enabled"])
        changed = True
    if "mdns_hostname" in data:
        cfg["mdns_hostname"] = normalize_hostname(data["mdns_hostname"])
        changed = True
    if "mdns_interface" in data:
        cfg["mdns_interface"] = str(data["mdns_interface"]).strip() or "auto"
        changed = True
    if "remote_gesture_mode" in data:
        gm = str(data["remote_gesture_mode"]).strip().lower()
        if gm in ("zoom_scroll", "touch"):
            cfg["remote_gesture_mode"] = gm
    if "remote_touch_mode" in data:
        tm = str(data["remote_touch_mode"]).strip().lower()
        if tm in ("auto", "touch", "mouse"):
            cfg["remote_touch_mode"] = tm
    if "monitor_quality" in data:
        qv = str(data["monitor_quality"]).strip().lower()
        if qv in ("smooth", "standard", "hd"):
            cfg["monitor_quality"] = qv
    if "abr_enabled" in data:
        cfg["abr_enabled"] = bool(data["abr_enabled"])
    for _k in ("latency_budget_monitor", "latency_budget_remote"):
        if _k in data:
            try:
                cfg[_k] = max(0.2, min(8.0, float(data[_k])))
            except Exception:
                pass
    if "video_window" in data:
        try:
            cfg["video_window"] = max(1, min(8, int(data["video_window"])))
        except Exception:
            pass
    if "monitor_audio_rate" in data:
        try:
            rv = int(data["monitor_audio_rate"])
        except Exception:
            rv = 0
        if rv in (8000, 12000, 16000, 24000, 32000, 48000):
            cfg["monitor_audio_rate"] = rv
    if "phrase" in data:
        phrase = str(data["phrase"]).strip()
        if phrase:
            cfg["phrase"] = phrase
    if "play_count" in data:
        try:
            cfg["play_count"] = max(1, min(10, int(data["play_count"])))
        except Exception:
            pass
    if "prompt_enabled" in data:
        cfg["prompt_enabled"] = bool(data["prompt_enabled"])
    if "prompt_sound" in data:
        cfg["prompt_sound"] = str(data["prompt_sound"])
    if "custom_sound_file" in data:
        cfg["custom_sound_file"] = str(data["custom_sound_file"])
    if "tts_engine" in data and str(data["tts_engine"]) in ("auto", "edge", "comtypes", "sapi", "pyttsx3"):
        cfg["tts_engine"] = str(data["tts_engine"])
    if "volume" in data:
        try:
            cfg["volume"] = max(0, min(100, int(data["volume"])))
        except Exception:
            pass
    if "volume_enabled" in data:
        cfg["volume_enabled"] = bool(data["volume_enabled"])
    if "autostart" in data:
        cfg["autostart"] = bool(data["autostart"])
        apply_autostart(cfg["autostart"])
    if "secure_enabled" in data:
        cfg["secure_enabled"] = bool(data["secure_enabled"])
    if "secure_port" in data:
        try:
            cfg["secure_port"] = max(1, min(65535, int(data["secure_port"])))
        except Exception:
            pass
    save_config(cfg)
    host_changed = normalize_hostname(cfg.get("mdns_hostname", "")) != old_host
    if changed:
        register_mdns(cfg, force=True)  # 主机名 / 开关 / 网卡变化立即重新生效
    if host_changed:
        refresh_cert_if_needed()   # 证书补上新名字，https 需重启才用得上
    notes = ["端口修改后需重启软件生效"]
    if host_changed:
        notes.insert(0, "主机名已修改：手机上的 http://新名字.local 立刻可用，"
                        "https（监控 / 文件 / 更新）需要重启软件一次才会换用新名字的证书")
    mdns = mdns_status()
    if cfg.get("mdns_enabled") and not mdns.get("ok"):
        notes.insert(0, "mDNS 没注册成功：%s（手机可用 http://IP:端口 直连）"
                        % (mdns.get("error") or "原因见日志"))
    return jsonify({"ok": True, "config": cfg, "host_changed": host_changed,
                    "mdns": mdns,
                    "note": "；".join(notes)})


def _restart_app(version=None):
    """重启软件：派生一个无窗口的 pythonw 子进程，延迟 2 秒后重新拉起 app.py，
    再结束当前进程。version 非空时启动 ROOT_DIR/<version>/app.py（OTA 更新后切换版本）。"""
    try:
        pyw = sys.executable
        if os.path.basename(pyw).lower().startswith("python.exe"):
            cand = os.path.join(os.path.dirname(pyw), "pythonw.exe")
            if os.path.isfile(cand):
                pyw = cand
        if not os.path.isfile(pyw):
            pyw = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if version:
            app_path = os.path.join(ROOT_DIR, str(version), "app.py")
            if not os.path.isfile(app_path):
                app_path = os.path.abspath(__file__)
        else:
            app_path = os.path.abspath(__file__)
        relaunch = ("import time, subprocess, sys\n"
                    "time.sleep(2)\n"
                    "subprocess.Popen([sys.executable, %r])\n" % app_path)
        subprocess.Popen([pyw, "-c", relaunch],
                         creationflags=_CREATE_NO_WINDOW, close_fds=True)
    except Exception as exc:
        print("[重启] 启动重启命令失败:", exc)
    time.sleep(0.6)
    os._exit(0)


@app.route("/api/restart", methods=["POST"])
def api_restart():
    err = check_access()
    if err:
        return err

    def do_restart():
        time.sleep(1.0)   # 先让响应发回浏览器
        _restart_app()

    threading.Thread(target=do_restart, daemon=True, name="restart").start()
    return jsonify({"ok": True, "message": "软件正在重启，网页即将断开"})


@app.route("/api/calls")
def api_calls():
    calls = get_calls()
    return jsonify({"ok": True, "calls": calls[-100:], "total": len(calls)})


@app.route("/api/calls/clear", methods=["POST"])
def api_calls_clear():
    with CALLS_LOCK:
        save_json(CALLS_PATH, [])
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
    if _desktop is not None:
        try:
            _desktop.clear_records()
        except Exception:
            pass
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 名条 API
# ---------------------------------------------------------------------------
@app.route("/api/roster", methods=["GET", "POST"])
def api_roster():
    if request.method == "GET":
        return jsonify({"ok": True, "roster": get_roster()})
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    number = str(data.get("number", "")).strip()
    name = str(data.get("name", "")).strip()
    if not number or not name:
        return jsonify({"ok": False, "error": "学号和姓名都不能为空"}), 400
    roster = get_roster()
    found = False
    for item in roster:
        if item["number"] == number:
            item["name"] = name
            found = True
            break
    if not found:
        roster.append({"number": number, "name": name})
    save_roster(roster)
    return jsonify({"ok": True, "roster": roster})


@app.route("/api/roster/delete", methods=["POST"])
def api_roster_delete():
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    number = str(data.get("number", "")).strip()
    roster = get_roster()
    new_roster = [item for item in roster if item["number"] != number]
    save_roster(new_roster)
    return jsonify({"ok": True, "roster": new_roster, "deleted": len(roster) - len(new_roster)})


@app.route("/api/roster/clear", methods=["POST"])
def api_roster_clear():
    err = check_access()
    if err:
        return err
    save_roster([])
    return jsonify({"ok": True, "roster": []})


# ---------------------------------------------------------------------------
# CSV 导入 API
# ---------------------------------------------------------------------------
def decode_csv_bytes(raw):
    """自动适配常见中文编码：UTF-8(BOM)、UTF-16(BOM)、GB18030、Big5、GBK 等。"""
    # BOM 检测
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except Exception:
            pass
    candidates = ("utf-8", "gb18030", "big5", "gbk")
    best, best_score = None, -999999
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except Exception:
            continue
        # 替换字符越少越好
        score = -text.count("\ufffd")
        if score > best_score:
            best, best_score = text, score
    return best if best is not None else raw.decode("utf-8", errors="replace")


def sniff_csv(raw):
    """返回 (行列表[首行可能是表头], 建议的学号列/姓名列)。"""
    text = decode_csv_bytes(raw)
    rows = []
    for row in csv.reader(io.StringIO(text)):
        rows.append([cell.strip() for cell in row])
    rows = [r for r in rows if any(cell for cell in r)]
    suggest_number, suggest_name = None, None
    if rows:
        header = rows[0]
        for i, cell in enumerate(header):
            low = cell.lower()
            if suggest_number is None and ("学号" in cell or "编号" in cell or low in ("id", "number", "no")):
                suggest_number = i
            if suggest_name is None and ("姓名" in cell or "名字" in cell or low in ("name",)):
                suggest_name = i
    return rows, suggest_number, suggest_name


@app.route("/api/csv/upload", methods=["POST"])
def api_csv_upload():
    err = check_access()
    if err:
        return err
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "未收到文件"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"ok": False, "error": "文件为空"}), 400
    has_header = request.form.get("has_header", "true") != "false"
    rows, suggest_number, suggest_name = sniff_csv(raw)
    if not rows:
        return jsonify({"ok": False, "error": "CSV 内容为空或无法解析"}), 400
    file_id = uuid.uuid4().hex
    path = os.path.join(UPLOAD_DIR, file_id + ".csv")
    with open(path, "wb") as out:
        out.write(raw)
    if has_header:
        columns = rows[0]
        preview = rows[1:9] if len(rows) > 1 else []
        total = max(0, len(rows) - 1)
    else:
        ncols = max(len(r) for r in rows)
        columns = ["列%d" % (i + 1) for i in range(ncols)]
        preview = rows[0:8]
        total = len(rows)
        if suggest_number is None:
            suggest_number = 0
        if suggest_name is None:
            suggest_name = 1 if ncols > 1 else 0
    return jsonify({
        "ok": True,
        "file_id": file_id,
        "total_rows": total,
        "columns": columns,
        "preview": preview,
        "suggest_number": suggest_number,
        "suggest_name": suggest_name,
    })


@app.route("/api/csv/import", methods=["POST"])
def api_csv_import():
    err = check_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    file_id = str(data.get("file_id", "")).strip()
    try:
        number_col = int(data.get("number_col", -1))
        name_col = int(data.get("name_col", -1))
    except Exception:
        return jsonify({"ok": False, "error": "请选择学号列和姓名列"}), 400
    has_header = bool(data.get("has_header", True))
    mode = str(data.get("mode", "append"))
    if number_col < 0 or name_col < 0:
        return jsonify({"ok": False, "error": "请选择学号列和姓名列"}), 400
    if number_col == name_col:
        return jsonify({"ok": False, "error": "学号列和姓名列不能相同"}), 400
    path = os.path.join(UPLOAD_DIR, file_id + ".csv")
    if not file_id or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件已过期，请重新上传"}), 400

    with open(path, "rb") as f:
        raw = f.read()
    text = decode_csv_bytes(raw)
    imported = []
    skipped = 0
    for idx, row in enumerate(csv.reader(io.StringIO(text))):
        if has_header and idx == 0:
            continue
        cells = [cell.strip() for cell in row]
        if number_col >= len(cells) or name_col >= len(cells):
            skipped += 1
            continue
        number = cells[number_col].strip()
        name = cells[name_col].strip()
        if not number or not name:
            skipped += 1
            continue
        imported.append({"number": number, "name": name})

    if not imported:
        return jsonify({"ok": False, "error": "没有读到有效数据"}), 400

    roster = get_roster()
    if mode == "replace":
        new_roster = []
        seen = set()
        for item in imported:
            if item["number"] not in seen:
                seen.add(item["number"])
                new_roster.append(item)
        save_roster(new_roster)
        return jsonify({"ok": True, "imported": len(new_roster),
                        "skipped": skipped + len(imported) - len(new_roster),
                        "total": len(new_roster), "mode": mode})
    else:
        existing = {item["number"] for item in roster}
        added = 0
        dup = 0
        for item in imported:
            if item["number"] in existing:
                dup += 1
                continue
            roster.append(item)
            existing.add(item["number"])
            added += 1
        save_roster(roster)
        return jsonify({"ok": True, "imported": added, "skipped": skipped + dup,
                        "total": len(roster), "mode": mode})


# ---------------------------------------------------------------------------
# 叫号 API
# ---------------------------------------------------------------------------
def fill_phrase(template, number, name, note, teacher):
    """填充叫号语，并返回学号、老师名称在结果中的位置（用于弹窗高亮）。
    只替换模板里的占位符，不替换备注中恰好包含的相同文字。"""
    tokens = {"%number%": number, "%name%": name, "%note%": note, "%teacher%": teacher}
    matches = []
    for tok in tokens:
        i = template.find(tok)
        if i >= 0:
            matches.append((i, tok))
    matches.sort()
    parts = []
    num_hl = None
    teacher_hl = None
    prev = 0
    for pos, tok in matches:
        parts.append(template[prev:pos])
        start = sum(len(x) for x in parts)
        value = tokens[tok]
        parts.append(value)
        end = start + len(value)
        if tok == "%number%":
            num_hl = [start, end]
        elif tok == "%teacher%":
            teacher_hl = [start, end]
        prev = pos + len(tok)
    parts.append(template[prev:])
    return "".join(parts), num_hl, teacher_hl


DUP_WINDOW_SECONDS = 300


def find_recent_duplicate(number, teacher):
    """同一个人 5 分钟内被同一位老师叫过则返回该记录，否则返回 None。"""
    try:
        calls = get_calls()
    except Exception:
        return None
    now = time.time()
    for c in reversed(calls):
        if c.get("number") != number or c.get("teacher") != teacher:
            continue
        try:
            t = time.mktime(time.strptime(str(c.get("time", "")),
                                          "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if now - t <= DUP_WINDOW_SECONDS:
            return c
        return None  # 最近一次已超窗，更早的必然也超窗
    return None


@app.route("/api/call", methods=["POST"])
def api_call():
    data = request.get_json(silent=True) or {}
    number = str(data.get("number", "")).strip()
    note = str(data.get("note", "")).strip()
    teacher = str(data.get("teacher", "")).strip() or "老师"
    name = str(data.get("name", "")).strip()
    force = bool(data.get("force", False))

    if not number:
        return jsonify({"ok": False, "error": "请输入学号"}), 400

    # 公网口上一律要求登录（局域网口按原样：本机/已授权设备可直接叫号）
    err = check_access()
    if err:
        return err

    # 叫号最小间隔（默认 2 秒）：多个老师同时连点也只会过一个，
    # 避免喇叭连珠炮。0 表示不限制。
    allowed, wait = security.CALL_LIMITER.take("__global__")
    if not allowed:
        return jsonify({"ok": False, "throttled": True,
                        "retry_after": round(wait, 1),
                        "error": "叫号太快了，请 %.1f 秒后再试" % wait}), 429

    # 老师身份以登录会话为准，前端传值只作显示兜底（防伪造"某老师"）
    try:
        from login import current_user as _cur
        _u = _cur()
        if _u:
            teacher = (_u.get("display_name") or _u.get("username") or teacher)
    except Exception:
        pass

    # 以名条中的姓名为准
    roster = get_roster()
    for item in roster:
        if item["number"] == number:
            name = item["name"]
            break
    if not name:
        name = "同学"

    # 重复叫号检查：同一个人在 5 分钟内被同一老师叫过时，前端弹窗确认
    if not force:
        recent = find_recent_duplicate(number, teacher)
        if recent:
            return jsonify({
                "ok": False,
                "duplicate": True,
                "error": "5 分钟内该学生已被同一位老师叫过，是否继续叫号？",
                "recent": recent,
            }), 409

    cfg = get_config()
    phrase, num_hl, teacher_hl = fill_phrase(cfg.get("phrase", DEFAULT_CONFIG["phrase"]),
                                             number, name, note, teacher)

    # 叫号次数 +1（注意 STATE_LOCK 不可重入，此处直接读写文件）
    with STATE_LOCK:
        state = load_json(STATE_PATH, {"call_count": 0})
        state["call_count"] = state.get("call_count", 0) + 1
        save_json(STATE_PATH, state)
        call_count = state["call_count"]

    log_call(number, name, note, teacher, phrase)
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "number": number,
        "name": name,
        "note": note,
        "teacher": teacher,
        "phrase": phrase,
        "num_hl": num_hl,
        "teacher_hl": teacher_hl,
        "call_count": call_count,
    }
    add_call_record(record)
    if _desktop is not None:
        try:
            _desktop.add_record(record)
        except Exception:
            pass
    _play_queue.put({"phrase": phrase, "record": record})

    return jsonify({"ok": True, "phrase": phrase, "number": number, "name": name,
                    "note": note, "teacher": teacher, "call_count": call_count})


# ---------------------------------------------------------------------------
# 文本消息 API（接收端电脑 TTS 读出文本）
# ---------------------------------------------------------------------------
@app.route("/api/message", methods=["POST"])
def api_message():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    teacher = str(data.get("teacher", "")).strip() or "老师"
    if not text:
        return jsonify({"ok": False, "error": "请输入消息内容"}), 400
    if len(text) > 500:
        return jsonify({"ok": False, "error": "消息过长（最多 500 字）"}), 400
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": "message",
        "number": "消息",
        "name": "",
        "note": "",
        "teacher": teacher,
        "phrase": text,
        "call_count": None,
    }
    _play_queue.put({"phrase": text, "record": record})
    return jsonify({"ok": True, "phrase": text})


# ---------------------------------------------------------------------------
# 提示音上传 API
# ---------------------------------------------------------------------------
@app.route("/api/sound/upload", methods=["POST"])
def api_sound_upload():
    err = check_access()
    if err:
        return err
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "未收到文件"}), 400
    fname = f.filename.lower()
    if not fname.endswith(".wav"):
        return jsonify({"ok": False, "error": "提示音仅支持 .wav 文件"}), 400
    safe = "prompt_" + uuid.uuid4().hex[:8] + ".wav"
    f.save(os.path.join(SOUNDS_DIR, safe))
    cfg = get_config()
    cfg["custom_sound_file"] = safe
    cfg["prompt_sound"] = "custom"
    cfg["prompt_enabled"] = True
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def main():
    try:
        _main()
    except Exception as exc:
        print("\n[错误] 程序异常退出:", exc)
        print("请截图本窗口内容反馈。")
        try:
            input("按回车键退出...")
        except Exception:
            pass


def _http_get_ok(url, timeout=4, context=None):
    """启动自检用：请求测试接口，返回 (是否正常, 详情)。"""
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            body = resp.read(256)
            if resp.status == 200 and b'"ok"' in body:
                return True, "正常"
            return False, "HTTP %s" % resp.status
    except Exception as exc:
        return False, str(exc)


def _resolve_mdns(hostname):
    """在本机尝试解析 hostname.local，返回 IP 或 None（带超时）。"""
    target = "%s.local" % hostname
    box = {}

    def work():
        try:
            for info in socket.getaddrinfo(target, None, socket.AF_INET):
                box["ip"] = info[4][0]
                break
        except Exception:
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(3.0)
    return box.get("ip")


def run_startup_checks(cfg, hostname, port, secure_port, progress=None):
    """启动自检，返回 [{level, name, detail, solution}]。level: ok/warn/error。
    progress(name) 会在每项检查开始前被调用，用于在启动动画上显示“正在检查什么”。"""
    checks = []

    def step(name):
        if progress:
            try:
                progress(name)
            except Exception:
                pass

    step("网页服务")
    ok, detail = _http_get_ok("http://127.0.0.1:%d/api/health" % port)
    checks.append({
        "level": "ok" if ok else "error",
        "name": "网页服务",
        "detail": ("HTTP 接口正常" if ok else ("HTTP 接口异常: " + detail)),
        "solution": "请重启软件；若仍失败，检查端口是否被占用或被防火墙拦截。",
    })

    if secure_port:
        step("安全服务")
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ok2, detail2 = _http_get_ok(
                "https://127.0.0.1:%d/api/health" % secure_port, timeout=4, context=ctx)
            checks.append({
                "level": "ok" if ok2 else "warn",
                "name": "安全服务",
                "detail": ("HTTPS 接口正常" if ok2 else ("HTTPS 接口异常: " + detail2)),
                "solution": "手机端敏感功能需要 HTTPS；可在设置中更换安全端口后重启。",
            })
        except Exception as exc:
            checks.append({"level": "warn", "name": "安全服务",
                           "detail": "HTTPS 自检失败: %s" % exc,
                           "solution": "可在设置中更换安全端口后重启。"})

    if cfg.get("mdns_enabled"):
        step("mDNS 域名解析")
        mdns = mdns_status()
        plan = mdns.get("plan") or []
        host_list = "、".join("%s.local → %s" % (p.get("hostname"),
                                              ", ".join(p.get("ips") or [])) for p in plan)
        if not HAS_ZEROCONF:
            checks.append({"level": "warn", "name": "mDNS",
                           "detail": "未安装 zeroconf 组件",
                           "solution": "请使用完整版软件（含 mDNS 组件）。"})
        elif not mdns.get("ok"):
            checks.append({"level": "warn", "name": "mDNS",
                           "detail": "mDNS 未注册成功：%s" % (mdns.get("error") or "未知原因"),
                           "solution": mdns.get("solution") or
                                         "到设置里点「mDNS 诊断」查看网卡情况；"
                                         "手机也可用 http://IP:端口 直连。"})
        else:
            # 本机解析只作参考：刚注册完 Windows 解析器常有缓存/延迟，查不到不代表手机打不开
            first = plan[0] if plan else {}
            ip = None
            for _ in range(3):
                ip = _resolve_mdns(first.get("hostname") or "") if first else None
                if ip:
                    break
                time.sleep(0.8)
            level = "ok"
            detail = "已注册 %s" % host_list
            solution = ""
            if ip and ip in (first.get("ips") or []):
                detail += "（本机解析一致：%s）" % ip
            elif ip:
                level = "warn"
                detail += "（本机解析到 %s，与预期 %s 不一致）" % (
                    ip, ", ".join(first.get("ips") or []) or "无")
                solution = "到设置里点「mDNS 诊断」查看网卡；也可让手机直接用 http://IP:端口。"
            else:
                detail += "（本机暂未解析到 .local 名字，其它设备通常仍可访问）"
                solution = "若手机也打不开，多半是路由器屏蔽了 mDNS 多播，可让手机用 http://IP:端口 直连。"
            checks.append({"level": level, "name": "mDNS", "detail": detail,
                           "solution": solution})

    step("数据目录")
    try:
        # data 目录可能被误删（比如卸载后又从快捷方式启动）：先尝试自愈创建
        for d in (DATA_DIR, SOUNDS_DIR, UPLOAD_DIR):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        probe = os.path.join(DATA_DIR, ".wtest")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        checks.append({"level": "ok", "name": "数据目录",
                       "detail": "数据目录可正常读写", "solution": ""})
    except Exception as exc:
        checks.append({"level": "error", "name": "数据目录",
                       "detail": "数据目录不可写: %s" % exc,
                       "solution": "确认 %s 存在、未被占用；若程序装在 C:\\Program Files 下，"
                                   "请用管理员身份运行一次（安装程序已自动授权），"
                                   "或把整个程序目录搬到 D 盘等可写位置。" % DATA_DIR})

    step("名条数据")
    try:
        n = len(get_roster())
        checks.append({"level": "ok", "name": "名条数据",
                       "detail": "已加载 %d 条名条" % n, "solution": ""})
    except Exception as exc:
        checks.append({"level": "warn", "name": "名条数据",
                       "detail": "名条读取异常: %s" % exc,
                       "solution": "检查 data/roster.json 是否损坏。"})
    return checks


_SINGLE_INSTANCE_MUTEX = None
# 互斥体名带上安装路径指纹：不同安装目录（如测试版/正式版）互不影响
_SINGLE_INSTANCE_NAME = "Local\\CQT_ClassCaller_" + \
    __import__("hashlib").md5(ROOT_DIR.encode("utf-8")).hexdigest()[:12]


def acquire_single_instance():
    """用 Windows 命名互斥体实现单实例：已有实例在运行返回 False。"""
    global _SINGLE_INSTANCE_MUTEX
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_NAME)
        if not handle:
            return True  # 创建失败时放行，避免误拦
        _SINGLE_INSTANCE_MUTEX = handle
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            _SINGLE_INSTANCE_MUTEX = None
            return False
        return True
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 教室端凭据（定时拉取）：只在电脑本机上可配置
# ---------------------------------------------------------------------------
def _creds_local_only():
    try:
        if security.is_public_request() or not is_local():
            return jsonify({"ok": False, "error": "只能在这台电脑上操作"}), 403
    except Exception:
        return jsonify({"ok": False, "error": "只能在这台电脑上操作"}), 403
    return None


@app.route("/api/creds/status")
def api_creds_status():
    """凭据状态：有没有配、拉没拉到、还剩多久到期。"""
    return jsonify({"ok": True, "creds": creds.status()})


@app.route("/api/creds/config", methods=["POST"])
def api_creds_config():
    """保存教室端凭据配置（激活码只写本机 config.json）。"""
    denied = _creds_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    cfg = get_config()
    for key in ("creds_slot", "creds_code", "creds_repo", "creds_branch",
                "creds_base", "creds_path"):
        if key in data:
            cfg[key] = str(data.get(key) or "").strip()
    if "creds_enabled" in data:
        cfg["creds_enabled"] = bool(data.get("creds_enabled"))
    if "creds_poll_minutes" in data:
        try:
            cfg["creds_poll_minutes"] = max(5, int(data.get("creds_poll_minutes") or 180))
        except Exception:
            pass
    save_config(cfg)
    try:
        creds.restart(get_config, on_update=tunnel.set_token,
                      log=lambda m: print(m))
    except Exception as exc:
        print("[凭据] 重启定时拉取失败:", exc)
    if cfg.get("creds_enabled"):
        threading.Thread(
            target=lambda: tunnel.start(int(cfg.get("public_port") or 9444),
                                        cfg.get("tunnel_id", ""), log=print),
            daemon=True, name="creds-tunnel").start()
    return jsonify({"ok": True, "creds": creds.status()})


@app.route("/api/creds/refresh", methods=["POST"])
def api_creds_refresh():
    """立刻拉一次（调试用）。"""
    denied = _creds_local_only()
    if denied:
        return denied
    ok, msg = creds.refresh_once(log=lambda m: print(m))
    if ok:
        tunnel.set_token(creds.token_info(), log=lambda m: print(m))
    return jsonify({"ok": ok, "error": "" if ok else msg, "creds": creds.status()})



# ---------------------------------------------------------------------------
# 自动登录：GitHub 账号 + 密码 → 浏览器自动完成设备码授权
# ---------------------------------------------------------------------------
_AUTO = {"state": "idle", "message": "", "user": "", "started": 0, "tries": 0}
_AUTO_LOCK = threading.Lock()
_AUTO_THREAD = None


def _auto_snapshot():
    with _AUTO_LOCK:
        st = dict(_AUTO)
    cred = autologin.load_credentials() or {}
    st["has_credentials"] = bool(cred)
    st["cred_user"] = cred.get("username", "")      # 只回用户名，密码绝不出库
    st["logged_in"] = tunnel.login_user()
    return st


def _auto_run(log=None):
    """后台跑一轮自动登录。"""
    with _AUTO_LOCK:
        _AUTO.update({"state": "running", "message": "正在自动登录…",
                      "started": time.time()})
    try:
        _headed = bool(get_config().get("autologin_headed", True))

        def _prog(text, br=None):
            page = ""
            try:
                page = br.url() if br is not None else ""
            except Exception:
                page = ""
            with _AUTO_LOCK:
                _AUTO["message"] = text
                _AUTO["step"] = text
                _AUTO["page"] = page
                _AUTO["updated"] = time.time()
            if log:
                log("[自动登录] %s %s" % (text, page))

        ok, msg = autologin.run(tunnel.EXE_PATH, tunnel._env, log=log or print,
                                headless=not _headed, progress=_prog)
    except Exception as exc:
        ok, msg = False, repr(exc)
    with _AUTO_LOCK:
        _AUTO.update({"state": "ok" if ok else "error",
                      "message": "" if ok else str(msg),
                      "user": str(msg) if ok else "",
                      "tries": _AUTO.get("tries", 0) + 1})
    if ok and log:
        log("[自动登录] 完成：%s" % msg)
    return ok, msg


def _auto_start(log=None):
    global _AUTO_THREAD
    with _AUTO_LOCK:
        if _AUTO.get("state") == "running":
            return False
        # 先在锁内把状态置为 running（占位），否则连点两次会同时通过检查、开两个浏览器
        _AUTO.update({"state": "running", "message": "正在自动登录…",
                      "step": "排队中", "page": "", "started": time.time(),
                      "tries": 0, "ok": False, "error": ""})
    _AUTO_THREAD = threading.Thread(target=_auto_run, kwargs={"log": log},
                                    daemon=True, name="auto-login")
    _AUTO_THREAD.start()
    return True


def _auto_watch():
    """守候：登录掉了就用保存的密码自己登回来（只在配了凭据且开关打开时）。"""
    while True:
        time.sleep(300)
        try:
            cfg = get_config()
            if not cfg.get("tunnel_auto_login"):
                continue
            if not autologin.has_credentials():
                continue
            if tunnel.login_user():
                continue
            with _AUTO_LOCK:
                running = _AUTO.get("state") == "running"
                tries = _AUTO.get("tries", 0)
            if running or tries >= 3:
                continue
            print("[自动登录] 检测到登录已失效，正在自动重登…")
            _auto_run(log=lambda m: print(m))
        except Exception as exc:
            print("[自动登录] 守候线程异常：%r" % (exc,))


@app.route("/api/tunnel/login_auto", methods=["POST"])
def api_tunnel_login_auto():
    """用 GitHub 账号密码自动登录（凭据 DPAPI 加密存本机）。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    user = str(data.get("username", "") or "").strip()
    pwd = str(data.get("password", "") or "")
    if user and pwd:
        try:
            ok, err = autologin.save_credentials(user, pwd)
        except Exception as exc:
            return jsonify({"ok": False, "error": "保存账号密码失败（可能被占用）：%r" % (exc,)})
        if not ok:
            return jsonify({"ok": False, "error": err})
    elif not autologin.has_credentials():
        return jsonify({"ok": False, "error": "请输入 GitHub 账号和密码"})
    if tunnel.login_user():
        return jsonify({"ok": True, "auto": _auto_snapshot(),
                        "message": "已经是登录状态"})
    started = _auto_start(log=lambda m: print(m))
    if not started:
        return jsonify({"ok": True, "queued": True,
                        "message": "正在登录中，请稍候…", "auto": _auto_snapshot()})
    return jsonify({"ok": True, "auto": _auto_snapshot()})


@app.route("/api/tunnel/auto_status")
def api_tunnel_auto_status():
    return jsonify({"ok": True, "auto": _auto_snapshot()})


@app.route("/api/tunnel/credentials", methods=["POST"])
def api_tunnel_credentials():
    """只保存账号密码，不立刻登录；clear=true 则删除。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    if data.get("clear"):
        autologin.clear_credentials()
        with _AUTO_LOCK:
            _AUTO.update({"state": "idle", "message": "", "user": "", "tries": 0})
        return jsonify({"ok": True, "auto": _auto_snapshot()})
    user = str(data.get("username", "") or "").strip()
    pwd = str(data.get("password", "") or "")
    if not user or not pwd:
        return jsonify({"ok": False, "error": "账号和密码都要填"})
    ok, err = autologin.save_credentials(user, pwd)
    if not ok:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "auto": _auto_snapshot()})


# ---------------------------------------------------------------------------
# 隧道命名与管理（不用发布工具，直接在叫号器里建/改/删）
# ---------------------------------------------------------------------------
def _norm_tid(name):
    return re.sub(r"[^a-z0-9\-]", "", str(name or "").strip().lower()
                  .split(".")[0])[:32].strip("-")


_IDS_CACHE = {"ts": 0.0, "ids": set()}


def _my_tunnel_ids(max_age=20.0):
    """列表带 20 秒缓存：界面点一次刷新只算一次。建/删后调 _ids_cache_clear()。"""
    now = time.time()
    if _IDS_CACHE["ids"] and (now - _IDS_CACHE["ts"]) < max_age:
        return set(_IDS_CACHE["ids"])
    rc, out = tunnel._run(["list"], timeout=60)
    ids = set()
    for line in (out or "").splitlines():
        tok = line.strip().split()[0] if line.strip() else ""
        if re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,40}(\.[a-z0-9]+)?", tok or ""):
            ids.add(tok.split(".")[0])
    if ids:
        _IDS_CACHE.update({"ts": time.time(), "ids": set(ids)})
    return ids


def _ids_cache_clear():
    _IDS_CACHE.update({"ts": 0.0, "ids": set()})


@app.route("/api/tunnel/probe_name", methods=["POST"])
def api_tunnel_probe_name():
    """检查自定义隧道名能不能用。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    tid = _norm_tid(data.get("tunnel_id"))
    if len(tid) < 3:
        return jsonify({"ok": False, "error": "名字至少 3 个字符（小写字母/数字/横线）"})
    if not tunnel.login_user():
        return jsonify({"ok": False, "error": "还没登录隧道账号"})
    if tid in _my_tunnel_ids():
        info = tunnel.show_tunnel(tid).get("tunnel") or {}
        ports = [int(p.get("portNumber") or 0) for p in (info.get("ports") or [])]
        return jsonify({"ok": True, "state": "mine", "tunnel_id": tid,
                        "ports": ports,
                        "label": "本账号已有这个隧道，可直接复用"})
    rc, out = tunnel._run(["show", tid], timeout=60)
    low = (out or "").lower()
    if "not found" in low:
        return jsonify({"ok": True, "state": "free", "tunnel_id": tid,
                        "label": "名字可用，点「创建并使用」即可"})
    if rc == 0:
        return jsonify({"ok": True, "state": "taken", "tunnel_id": tid,
                        "label": "这个名字已被其他账号占用，换一个"})
    return jsonify({"ok": False, "error": (out or "").strip()[-160:] or "查询失败"})


@app.route("/api/tunnel/create_name", methods=["POST"])
def api_tunnel_create_name():
    """按自定义名字创建（或复用）隧道，并立刻用它开公网访问。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    tid = _norm_tid(data.get("tunnel_id"))
    if len(tid) < 3:
        return jsonify({"ok": False, "error": "名字至少 3 个字符"})
    if not tunnel.login_user():
        return jsonify({"ok": False, "error": "还没登录隧道账号"})
    port = int(get_config().get("public_port", 9444) or 9444)
    created = False
    if tid not in _my_tunnel_ids():
        rc, out = tunnel._run(["show", tid], timeout=60)
        if rc == 0:
            return jsonify({"ok": False,
                            "error": "这个名字已被其他账号占用，换一个"})
        rc, out = tunnel._run(["create", tid, "--expiration",
                               "%dh" % int(get_config().get("tunnel_expiration_hours", 720) or 720)],
                              timeout=120)
        if rc != 0:
            txt = (out or "").strip()
            if "rate limit" in txt.lower():
                return jsonify({"ok": False,
                                "error": "微软限流了：创建隧道太频繁，等 1 分钟再试（已经建好的不会重复扣）"})
            return jsonify({"ok": False, "error": "创建失败：%s" % txt[-200:]})
        created = True
    cfg = get_config()
    cfg["tunnel_id"] = tid
    cfg["public_enabled"] = True
    save_config(cfg)
    _ids_cache_clear()
    st = tunnel.start(port, tid,
                      int(cfg.get("tunnel_expiration_hours", 720) or 720),
                      log=lambda m: print(m), prepare=False)
    return jsonify({"ok": True, "created": created, "reused": not created,
                    "tunnel_id": tid, "tunnel": st})


@app.route("/api/tunnel/create_batch", methods=["POST"])
def api_tunnel_create_batch():
    """批量按规则建隧道：前缀 + 起始号 … 结束号（一次把一个年级占好名字）。

    每个名字都先探一次：本账号已有 → 复用；没人用 → 创建；被别人占 → 记失败。
    返回逐条结果，前端标出"新建 / 复用 / 被占用"。
    """
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    # 注意：前缀不能走 _norm_tid（它会把结尾的横线去掉，cc- + 105 就变成 cc105）
    prefix = re.sub(r"[^a-z0-9\-]", "", str(data.get("prefix") or "cc-").lower())[:24]
    try:
        start = int(data.get("start") or 1)
        end = int(data.get("end") or 8)
    except Exception:
        return jsonify({"ok": False, "error": "起始号和结束号要是数字"})
    if end < start:
        return jsonify({"ok": False, "error": "结束号不能小于起始号"})
    if end - start > 40:
        return jsonify({"ok": False, "error": "一次最多 41 个（年级够用了）"})
    if not tunnel.login_user():
        return jsonify({"ok": False, "error": "还没登录隧道账号"})

    cfg = get_config()
    expire = int(cfg.get("tunnel_expiration_hours", 720) or 720)
    mine = _my_tunnel_ids()
    rows = []
    for n in range(start, end + 1):
        name = ("%s%d" % (prefix, n))
        if not prefix.endswith("-"):
            name = prefix + "-" + str(n)
        name = re.sub(r"[^a-z0-9\-]", "", name).strip("-")[:32]
        if len(name) < 3:
            rows.append({"name": name, "state": "error", "message": "名字太短"})
            continue
        if name in mine:
            rows.append({"name": name, "state": "reused", "message": "本账号已有"})
            continue
        rc, out = tunnel._run(["show", name], timeout=60)
        if rc == 0:
            rows.append({"name": name, "state": "taken",
                         "message": "被其他账号占用"})
            continue
        # 微软对"建隧道"有频率限制：被限流就等一分钟重试，最多重试两次
        created_ok = False
        last_err = ""
        for attempt in range(3):
            rc, out = tunnel._run(["create", name, "--expiration", "%dh" % expire],
                                  timeout=120)
            if rc == 0:
                created_ok = True
                break
            last_err = (out or "").strip()
            if "rate limit" in last_err.lower() and attempt < 2:
                time.sleep(60)
                continue
            break
        if not created_ok:
            if "rate limit" in last_err.lower():
                rows.append({"name": name, "state": "error",
                             "message": "微软限流：稍后重跑一次即可，已建好的会自动复用"})
                break
            rows.append({"name": name, "state": "error", "message": last_err[-120:]})
            continue
        time.sleep(2.0)          # 别打太快
        tunnel._ensure_port(name, int(cfg.get("public_port", 9444) or 9444))
        tunnel._ensure_anonymous(name)
        mine.add(name)
        rows.append({"name": name, "state": "created", "message": "已新建"})

    ok_n = sum(1 for r in rows if r["state"] in ("created", "reused"))
    return jsonify({"ok": True, "total": len(rows), "ok_count": ok_n,
                    "rows": rows,
                    "tunnels": sorted(mine)})


@app.route("/api/tunnel/delete_name", methods=["POST"])
def api_tunnel_delete_name():
    """删除本账号的隧道（危险操作：地址会变）。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    tid = _norm_tid(data.get("tunnel_id"))
    if not tid:
        return jsonify({"ok": False, "error": "名字不合法"})
    if tid not in _my_tunnel_ids():
        return jsonify({"ok": False, "error": "这不是本账号的隧道"})
    tunnel.stop()
    rc, out = tunnel._run(["delete", tid], timeout=90)
    _ids_cache_clear()
    if rc != 0:
        return jsonify({"ok": False, "error": "删除失败：%s" % (out or "").strip()[-200:]})
    return jsonify({"ok": True})


@app.route("/api/tunnel/list_names")
def api_tunnel_list_names():
    """本账号已有的隧道列表。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    if not tunnel.login_user():
        return jsonify({"ok": False, "error": "还没登录隧道账号", "tunnels": []})
    cfg = get_config()
    return jsonify({"ok": True,
                    "current": cfg.get("tunnel_id", ""),
                    "tunnels": sorted(_my_tunnel_ids())})



@app.route("/api/tunnel/enable", methods=["POST"])
def api_tunnel_enable():
    """③ 穿透策略：一个开关开关内网穿透。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    on = bool(data.get("on"))
    cfg = get_config()
    cfg["public_enabled"] = on
    save_config(cfg)
    port = int(cfg.get("public_port", 9444) or 9444)
    if on:
        if not tunnel.login_user():
            return jsonify({"ok": False, "error": "还没登录 GitHub 账号（见上面 ①）"})
        st = tunnel.start(port, cfg.get("tunnel_id", ""),
                          int(cfg.get("tunnel_expiration_hours", 720) or 720),
                          log=lambda m: print(m))
    else:
        st = tunnel.stop(log=lambda m: print(m))
    return jsonify({"ok": True, "tunnel": st, "enabled": on})


@app.route("/api/tunnel/autostart", methods=["POST"])
def api_tunnel_autostart():
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    cfg = get_config()
    cfg["tunnel_autostart"] = bool(data.get("on"))
    save_config(cfg)
    return jsonify({"ok": True, "autostart": cfg["tunnel_autostart"]})


@app.route("/api/tunnel/logout", methods=["POST"])
def api_tunnel_logout():
    """退出 GitHub 登录（可选同时清掉本机保存的账号密码）。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    clear = bool(data.get("clear_credentials"))
    msgs = []
    try:
        tunnel.stop(log=lambda m: msgs.append(m))
    except Exception:
        pass
    rc, out = tunnel._run(["user", "logout"], timeout=90)
    msgs.append("logout rc=%s %s" % (rc, (out or "").strip()[:120]))
    if clear:
        try:
            autologin.clear_credentials()
            msgs.append("已清除本机保存的密码")
        except Exception as exc:
            msgs.append("清除密码失败：%r" % (exc,))
    with _AUTO_LOCK:
        _AUTO.update({"state": "idle", "message": "", "user": "", "tries": 0})
    cfg = get_config()
    cfg["public_enabled"] = False
    save_config(cfg)
    print("[隧道] 退出登录：%s" % " | ".join(msgs))
    return jsonify({"ok": True, "logged_in": tunnel.login_user(),
                    "cleared": clear, "detail": " | ".join(msgs)[-300:]})


@app.route("/api/tunnel/autologin_mode", methods=["POST"])
def api_tunnel_autologin_mode():
    """自动登录时是否显示浏览器窗口。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    headed = bool(data.get("headed"))
    cfg = get_config()
    cfg["autologin_headed"] = headed
    save_config(cfg)
    return jsonify({"ok": True, "headed": headed})


@app.route("/api/tunnel/relogin", methods=["POST"])
def api_tunnel_relogin():
    """① 强制重新登录：先退掉旧登录态，再用保存的账号密码登一次。"""
    denied = _tunnel_local_only()
    if denied:
        return denied
    if not autologin.has_credentials():
        return jsonify({"ok": False, "error": "还没保存 GitHub 账号密码"})
    try:
        tunnel._run(["user", "logout"], timeout=60)
    except Exception:
        pass
    tunnel.stop()
    with _AUTO_LOCK:
        _AUTO.update({"state": "idle", "message": "", "tries": 0})
    _auto_start(log=lambda m: print(m))
    return jsonify({"ok": True, "auto": _auto_snapshot()})



# ---------------------------------------------------------------------------
# 后台任务：新建隧道 / 开关穿透（CLI 每次要 5~6 秒，必须异步）
# ---------------------------------------------------------------------------
_JOB = {"running": False, "kind": "", "ok": False, "error": "", "done_at": 0}
_JOB_LOCK = threading.Lock()


def _job_state():
    with _JOB_LOCK:
        return dict(_JOB)


def _job_run(kind, fn):
    """立刻返回，实际动作丢到后台线程。"""
    with _JOB_LOCK:
        if _JOB.get("running"):
            return False, "上一个操作还在进行中，稍等一下"
        _JOB.update({"running": True, "kind": kind, "ok": False,
                     "error": "", "done_at": 0})

    def _w():
        err = ""
        try:
            fn()
        except Exception as exc:
            err = repr(exc)[:200]
        finally:
            try:
                _ids_cache_clear()
            except Exception:
                pass
            with _JOB_LOCK:
                _JOB.update({"running": False, "ok": not err, "error": err,
                             "done_at": time.time()})
            print("[后台] %s 结束%s" % (kind, ("：" + err) if err else ""))

    threading.Thread(target=_w, daemon=True, name="job-" + kind).start()
    return True, ""


def _ensure_port_once(tid, port):
    """幂等：端口已存在时 CLI 会报错，忽略即可（一次调用 ~2 秒）。"""
    tunnel._run(["port", "create", tid, "-p", str(int(port)),
                 "--protocol", "http"], timeout=90)
    tunnel._run(["access", "create", tid, "--anonymous"], timeout=90)


def _work_create(tid):
    cfg = get_config()
    port = int(cfg.get("public_port", 9444) or 9444)
    ids = _my_tunnel_ids(max_age=0)
    if tid not in ids:
        rc, out = tunnel._run(["create", tid, "--expiration",
                               "%dh" % int(cfg.get("tunnel_expiration_hours", 720) or 720)],
                              timeout=120)
        if rc != 0 and "rate limit" in (out or "").lower():
            raise RuntimeError("微软限流：等一分钟再试（已建好的不会重复）")
    _ensure_port_once(tid, port)
    cfg["tunnel_id"] = tid
    cfg["public_enabled"] = True
    save_config(cfg)
    tunnel.start(port, tid, int(cfg.get("tunnel_expiration_hours", 720) or 720),
                 log=lambda m: print(m), prepare=False)


def _work_enable(on):
    cfg = get_config()
    cfg["public_enabled"] = bool(on)
    save_config(cfg)
    port = int(cfg.get("public_port", 9444) or 9444)
    tid = _clean_tid(cfg.get("tunnel_id", ""))
    if not on:
        tunnel.stop(log=lambda m: print(m))
        return
    if not tid:
        raise RuntimeError("先在②里选一条隧道")
    if not tunnel.login_user():
        raise RuntimeError("还没登录 GitHub 账号")
    _ensure_port_once(tid, port)
    tunnel.start(port, tid, int(cfg.get("tunnel_expiration_hours", 720) or 720),
                 log=lambda m: print(m), prepare=False)


def _clean_tid(name):
    return re.sub(r"[^a-z0-9\-]", "", str(name or "").split(".")[0].lower())[:32]


@app.route("/api/tunnel/job")
def api_tunnel_job():
    return jsonify({"ok": True, "job": _job_state()})


@app.route("/api/tunnel/create_async", methods=["POST"])
def api_tunnel_create_async():
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    tid = _clean_tid(data.get("tunnel_id"))
    if len(tid) < 3:
        return jsonify({"ok": False, "error": "名字至少 3 个字符"})
    if not tunnel.login_user():
        return jsonify({"ok": False, "error": "还没登录 GitHub 账号"})
    ok, err = _job_run("create:" + tid, lambda: _work_create(tid))
    if not ok:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "queued": True, "tunnel_id": tid})


@app.route("/api/tunnel/enable_async", methods=["POST"])
def api_tunnel_enable_async():
    denied = _tunnel_local_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    on = bool(data.get("on"))
    ok, err = _job_run("enable" if on else "disable", lambda: _work_enable(on))
    if not ok:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "queued": True, "on": on})



def _main():
    global _running_port, _running_secure_port, _desktop

    # 启动即申请管理员权限（用户拒绝则继续以普通权限运行）
    ensure_admin()

    set_config_getter(get_config)
    try:
        remote_set_config_getter(get_config)
    except Exception:
        pass
    update_mod.set_config_getter(get_config)
    update_mod.set_restart_callback(_restart_app)

    # 单实例保护：已有实例运行时直接提示并退出
    if not acquire_single_instance():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "班级叫号器已经在运行了，请查看屏幕右下角的托盘图标。",
                "班级叫号器", 0x40 | 0x0)
        except Exception:
            pass
        return

    # 桌面组件（启动动画 / 错误弹窗）
    SplashWindow = show_fatal_error = DesktopUI = None
    try:
        from desktop import SplashWindow, show_fatal_error, DesktopUI
    except Exception as exc:
        print("[桌面] 桌面组件不可用，将仅使用控制台输出:", exc)

    splash = None
    if SplashWindow is not None:
        try:
            splash = SplashWindow()
            splash.show()
        except Exception as exc:
            print("[启动动画] 显示失败:", exc)
            splash = None

    def status(text):
        print("[启动] " + text)
        if splash is not None:
            splash.set_status(text)

    cfg = get_config()
    port = int(cfg.get("port", 8000))
    _running_port = port
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))

    # 自启：启动后确保“最高权限计划任务”存在（已是管理员才建得成）
    if cfg.get("autostart"):
        threading.Thread(target=lambda: apply_autostart(True), daemon=True,
                         name="autostart-sync").start()

    status("正在启动网页服务…")
    from werkzeug.serving import make_server

    # HTTP 服务（叫号主功能）
    # 端口被占用 / 落在系统保留段时，自动往后找可用端口再启动
    # （原来会直接弹错误退出，用户改成这类端口后软件就起不来，看起来像"端口改不了"）
    server = None
    bind_err = None
    try:
        excl = _excluded_tcp_ranges()
    except Exception:
        excl = []
    cands = [port]
    cands += [p for p in range(port + 1, port + 11)]
    for p in (8000, 8080, 8888, 9000, 10080):
        if p not in cands:
            cands.append(p)
    for p in cands:
        if not (1 <= p <= 65535) or p in excl:
            continue
        try:
            server = make_server("0.0.0.0", p, app, threaded=True)
            if p != port:
                print("[启动] 端口 %d 不可用，已改用 %d" % (port, p))
                status("端口 %d 被占用，已改用 %d" % (port, p))
            port = p
            _running_port = p
            break
        except BaseException as exc:
            bind_err = exc
            server = None
    try:
        if server is None:
            raise RuntimeError(bind_err or "所有候选端口都不可用")
    except BaseException as exc:  # werkzeug 绑定失败会 sys.exit(1)，必须接住
        cause = "HTTP 端口 %d 启动失败：%s" % (port, exc)
        print("\n[错误] " + cause)
        solution = ("端口可能被其他程序占用，或被系统保留。"
                    "请到设置中更换端口，或修改 data/config.json 中的 port 后重新运行。")
        if splash is not None:
            try:
                splash.close()
            except Exception:
                pass
        if show_fatal_error is not None:
            try:
                show_fatal_error(cause, solution)
            except Exception:
                pass
        else:
            print(solution)
            try:
                input("按回车键退出...")
            except Exception:
                pass
        return

    def _serve():
        try:
            server.serve_forever()
        except Exception as exc:
            print("[网页服务] 异常退出:", exc)

    threading.Thread(target=_serve, daemon=True, name="web-server").start()

    # ------------------------------------------------------------------
    # 公网访问：安全层初始化 + 公网专用端口 + 隧道
    #
    # 公网口（默认 9444）**只绑 127.0.0.1**：局域网任何人都连不到，
    # 只有本机的隧道进程能进；该端口上的请求永不被当作"本机"，
    # 一切操作都要登录（见 auth.check_access / login.current_user）。
    # ------------------------------------------------------------------
    try:
        security.init_from_config(cfg)
        public_port = int(cfg.get("public_port", 9444) or 9444)
        security.set_public_port(public_port)
        print("[公网] 公网专用端口 %d（仅 127.0.0.1，供隧道使用）" % public_port)

        def _serve_public():
            try:
                pub = make_server("127.0.0.1", public_port, app, threaded=True)
                pub.serve_forever()
            except BaseException as exc:
                print("[公网] 端口 %d 启动失败（不影响局域网使用）：%s"
                      % (public_port, exc))

        threading.Thread(target=_serve_public, daemon=True,
                         name="public-server").start()

        # 教室端凭据：先起"定时拉取"，再把隧道挂上（令牌模式，不需要登录账号）
        # 预热隧道列表缓存（CLI 一次要 5~9 秒，先跑一次，界面打开就不卡）
        def _warm_ids():
            try:
                if tunnel.login_user():
                    _my_tunnel_ids(max_age=0)
            except Exception:
                pass
        threading.Thread(target=_warm_ids, daemon=True, name="ids-warm").start()

        if cfg.get("creds_enabled"):
            try:
                creds.start(get_config, on_update=tunnel.set_token,
                            log=lambda m: print(m))
                status("已启用教室凭据（定时拉取）")
            except Exception as exc:
                print("[凭据] 启动失败:", exc)

        # 登录失效自动重登（有保存的账号密码才生效）
        if cfg.get("tunnel_auto_login", True) and autologin.has_credentials():
            threading.Thread(target=_auto_watch, daemon=True,
                             name="auto-login-watch").start()

        if (cfg.get("public_enabled") and cfg.get("tunnel_autostart", True)) or cfg.get("creds_enabled"):
            def _boot_tunnel():
                time.sleep(2.0)
                st = tunnel.start(public_port,
                                  cfg.get("tunnel_id", ""),
                                  int(cfg.get("tunnel_expiration_hours", 720) or 720),
                                  log=lambda m: print(m))
                print("[公网] " + tunnel.summary_line())
            threading.Thread(target=_boot_tunnel, daemon=True,
                             name="tunnel-boot").start()
    except Exception as exc:
        print("[公网] 初始化失败（不影响局域网使用）：", exc)

    # 首次使用（还没完成设置向导）时自动打开浏览器，省得用户找不到入口
    def _auto_open_browser():
        try:
            st = load_json(STATE_PATH, {})
            # 首次设置没完成：每次启动都把设置向导弹出来（不再只弹一次）
            if st.get("wizard_done"):
                return
            time.sleep(2.5)
            import webbrowser
            url = "http://127.0.0.1:%d/" % port
            webbrowser.open(url)
            st["browser_auto_opened"] = True
            st["browser_auto_opened_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_json(STATE_PATH, st)
            print("[启动] 已自动打开设置向导页面:", url)
        except Exception as exc:
            print("[启动] 自动打开浏览器失败:", exc)

    threading.Thread(target=_auto_open_browser, daemon=True, name="auto-browser").start()

    # 后台预热摄像头/麦克风列表，避免第一次打开监控页要等很久
    try:
        import monitor as _mon
        _mon.warm_devices()
    except Exception:
        pass

    # HTTPS 安全端口（手机端 WebCrypto 需要安全上下文；端口被占用/保留时自动回退）
    https_server = None
    _running_secure_port = None
    if cfg.get("secure_enabled", True):
        status("正在准备安全服务…")
        try:
            ssl_ctx = ensure_ssl_context()
            excluded = _excluded_tcp_ranges()
            candidates = []
            try:
                candidates.append(int(cfg.get("secure_port", 9443)))
            except Exception:
                candidates.append(9443)
            for p in (9443, 10443, 12443, 14443, 15443, 16443, 17443, 18443, 0):
                if p not in candidates:
                    candidates.append(p)
            for sp in candidates:
                relief = None
                if sp and _port_excluded(sp, _excluded_tcp_ranges()):
                    # 端口被系统保留：只有开启“抢占保留端口”且是管理员时才尝试
                    if True:   # 端口抢占功能已移除（不再停 WinNAT / Hyper-V 网络）
                        print("[HTTPS] 端口 %s 被系统保留，跳过（可在设置里开启抢占）" % sp)
                        continue
                    ok, relief = _release_reserved_port(sp)
                    if not ok:
                        print("[HTTPS] 端口 %s 无法释放，跳过" % sp)
                        continue
                try:
                    https_server = make_server("0.0.0.0", sp, app,
                                               threaded=True, ssl_context=ssl_ctx)
                    _running_secure_port = https_server.server_port
                    threading.Thread(target=https_server.serve_forever,
                                     daemon=True, name="web-https").start()
                    _restore_net_service(relief)
                    relief = None
                    break
                except BaseException as exc:
                    print("[HTTPS] 端口 %s 启动失败，尝试下一个: %s" % (sp, exc))
                finally:
                    _restore_net_service(relief)
            if https_server is not None:
                print("手机安全访问: https://%s:%d" % (hostname, _running_secure_port))
                # 把实际用上的端口写回配置，下次启动直接命中，地址也稳定
                try:
                    if int(cfg.get("secure_port", 0) or 0) != int(_running_secure_port):
                        cfg["secure_port"] = int(_running_secure_port)
                        save_config(cfg)
                        print("[HTTPS] 已把安全端口记录为 %s" % _running_secure_port)
                except Exception:
                    pass
            else:
                print("[HTTPS] 所有安全端口均启动失败")
        except BaseException as exc:
            print("[HTTPS] 安全服务启动失败（不影响 HTTP 叫号）:", exc)

    # mDNS（同步注册，便于后续自检）
    if cfg.get("mdns_enabled"):
        status("正在注册 mDNS…")
        register_mdns(cfg)
        start_mdns_watcher()   # 之后网卡一变（换网/插网线/Wi-Fi 连上）就自动重新注册
    else:
        print("[mDNS] 已关闭")

    # 启动自检（逐项显示“正在检查什么”）
    checks = run_startup_checks(
        cfg, hostname, port, _running_secure_port,
        progress=lambda name: status("正在检查：%s…" % name))
    for c in checks:
        print("[自检] %s %s: %s" % (c["name"], c["level"], c["detail"]))

    fatal = [c for c in checks if c.get("level") == "error"]
    if fatal:
        cause = "；".join("%s：%s" % (c["name"], c["detail"]) for c in fatal)
        solution = "；".join(c["solution"] for c in fatal if c.get("solution")) or "请重启软件重试。"
        if splash is not None:
            try:
                splash.close()
            except Exception:
                pass
        if show_fatal_error is not None:
            try:
                show_fatal_error(cause, solution)
            except Exception:
                pass
        else:
            print("\n[错误] " + cause + "\n解决办法: " + solution)
            try:
                input("按回车键退出...")
            except Exception:
                pass
        try:
            server.shutdown()
        except Exception:
            pass
        if https_server is not None:
            try:
                https_server.shutdown()
            except Exception:
                pass
        remote_shutdown()
        monitor_shutdown()
        return

    status("启动完成，正在打开面板…")
    if splash is not None:
        try:
            splash.close()
        except Exception:
            pass

    print("=" * 46)
    print("班级叫号器已启动")
    print("本机访问:   http://127.0.0.1:%d" % port)
    for ip in get_lan_ips():
        print("局域网访问: http://%s:%d" % (ip, port))
    if cfg.get("mdns_enabled") and HAS_ZEROCONF:
        print("mDNS 访问:  http://%s.local:%d" % (hostname, port))
    print("=" * 46)

    # 桌面组件（面板 + 托盘）在主线程运行消息循环
    try:
        _desktop = DesktopUI(DATA_DIR, port, get_records=get_calls)
        set_desktop(_desktop)
        update_mod.set_desktop(_desktop)
        update_mod.maybe_auto_update()
        _desktop.run()
    except Exception as exc:
        print("[桌面] 桌面组件不可用，仅运行网页服务:", exc)
        _desktop = None
        try:
            _serve()
        except Exception:
            pass
    finally:
        _MDNS_STOP.set()
        remote_shutdown()
        monitor_shutdown()
        try:
            server.shutdown()
        except Exception:
            pass
        if https_server is not None:
            try:
                https_server.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
