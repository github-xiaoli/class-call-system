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
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from flask import Flask, jsonify, render_template, request, redirect
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
from remote import remote_bp, remote_shutdown, set_desktop, ws_remote
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

for _d in (DATA_DIR, SOUNDS_DIR, UPLOAD_DIR):
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
    "require_admin": True,          # 启动时自动申请管理员（自启用计划任务，登录不再弹 UAC）
    "secure_enabled": True,         # 是否启用 HTTPS 安全端口（手机端激活/敏感功能用）
    "secure_port": 9443,            # HTTPS 安全端口（8443 常被 Windows 保留，故默认 9443）
    "secure_port_force": False,     # 目标端口被系统保留时，是否临时停止 WinNAT 抢占该端口（需管理员）
    "update_channel": "stable",     # OTA 更新通道：stable | beta | dev
    "update_policy": "none",        # OTA 更新策略：none | check | download | install
}

try:
    import winsound
    HAS_WINSOUND = True
except Exception:
    HAS_WINSOUND = False

try:
    from zeroconf import ServiceInfo, Zeroconf
    HAS_ZEROCONF = True
except Exception:
    HAS_ZEROCONF = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB，支持文件传输上传
sock = Sock(app)

app.register_blueprint(auth_bp)
app.register_blueprint(monitor_bp)
app.register_blueprint(remote_bp)
app.register_blueprint(fs_bp)
app.register_blueprint(update_mod.update_bp)

# WebSocket 实时通道（低延迟：摄像头/麦克风/对讲/远程控制）
sock.route("/ws/monitor/camera")(ws_monitor_camera)
sock.route("/ws/monitor/audio")(ws_monitor_audio)
sock.route("/ws/monitor/intercom")(ws_monitor_intercom)
sock.route("/ws/remote")(ws_remote)

# 生成令牌签名密钥（首次运行自动写入 config.json）
ensure_auth_secret()


@app.after_request
def _no_cache(resp):
    """禁止浏览器缓存页面/脚本，避免更新后前端仍用旧文件。"""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

CONFIG_LOCK = threading.Lock()
ROSTER_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
CALLS_LOCK = threading.Lock()
MDNS_LOCK = threading.Lock()

_mdns_zc = None      # 当前 Zeroconf 实例
_mdns_info = None    # 当前 ServiceInfo
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
        if not get_config().get("require_admin", True):
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
    """返回本机局域网 IPv4 列表，默认路由网卡 IP 排在最前（多网卡时优先可用）。"""
    primary = None
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            ips.add(primary)
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.discard("127.0.0.1")
    ordered = []
    if primary and primary in ips:
        ordered.append(primary)
    for ip in sorted(ips):
        if ip != primary:
            ordered.append(ip)
    return ordered


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000


def get_network_interfaces():
    """枚举带有 IPv4 地址的网卡（用于设置界面选择 mDNS 绑定网卡）。
    返回 [{id, name, ips}]，id/name 使用 Windows 网卡别名（如 以太网 / WLAN）。"""
    interfaces = []
    try:
        ps = ("Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
              "Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } | "
              "Select-Object InterfaceAlias,IPAddress | ConvertTo-Json -Compress")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, text=True, timeout=25,
            creationflags=_CREATE_NO_WINDOW, check=False)
        items = json.loads(proc.stdout.strip() or "[]")
        if isinstance(items, dict):
            items = [items]
        by_name = {}
        for it in items:
            name = str(it.get("InterfaceAlias", "")).strip()
            ip = str(it.get("IPAddress", "")).strip()
            if not name or not ip:
                continue
            by_name.setdefault(name, []).append(ip)
        for name in sorted(by_name.keys()):
            interfaces.append({"id": name, "name": name,
                               "ips": sorted(by_name[name])})
    except Exception as exc:
        print("[网卡] 枚举网卡失败:", exc)
    if not interfaces:
        ips = get_lan_ips()
        if ips:
            interfaces.append({"id": "auto", "name": "自动（全部网卡）", "ips": ips})
    return interfaces


def get_mdns_ips(cfg):
    """按 mdns_interface 配置返回 mDNS 应广播的 IP 列表。"""
    all_ips = get_lan_ips()
    iface = str(cfg.get("mdns_interface", "") or "").strip()
    if not iface or iface == "auto":
        return all_ips
    for it in get_network_interfaces():
        if it.get("id") == iface or it.get("name") == iface:
            ips = [ip for ip in it.get("ips", []) if ip not in ("127.0.0.1",)]
            if ips:
                return ips
    return all_ips


def normalize_hostname(name):
    name = (name or "").strip().lower()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    name = "".join(ch for ch in name if ch in allowed)
    return name or socket.gethostname().lower()


def register_mdns(cfg):
    """按当前配置注册（或重新注册）mDNS 服务（HTTP + HTTPS）。"""
    global _mdns_zc, _mdns_info
    if not HAS_ZEROCONF:
        return False
    with MDNS_LOCK:
        try:
            if _mdns_zc is not None:
                try:
                    _mdns_zc.close()
                except Exception:
                    pass
                _mdns_zc = None
                _mdns_info = None
            if not cfg.get("mdns_enabled"):
                return False
            hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
            port = int(_running_port or cfg.get("port", 8000))
            ips = get_mdns_ips(cfg)
            addresses = [socket.inet_aton(ip) for ip in ips]
            if not addresses:
                addresses = [socket.inet_aton("127.0.0.1")]
            zc = Zeroconf()
            info = ServiceInfo(
                "_http._tcp.local.",
                "班级叫号器._http._tcp.local.",
                addresses=addresses,
                port=port,
                properties={b"path": b"/"},
                server=("%s.local." % hostname),
            )
            zc.register_service(info)
            _mdns_zc = zc
            _mdns_info = info
            print("[mDNS] 已注册 http://%s.local:%d  (IP: %s)" % (hostname, port, ", ".join(ips)))
            # HTTPS 安全端口（手机端激活/敏感功能）
            if cfg.get("secure_enabled", True):
                try:
                    secure_port = int(_running_secure_port or cfg.get("secure_port", 9443))
                    info2 = ServiceInfo(
                        "_https._tcp.local.",
                        "班级叫号器-安全._https._tcp.local.",
                        addresses=addresses,
                        port=secure_port,
                        properties={b"path": b"/"},
                        server=("%s.local." % hostname),
                    )
                    zc.register_service(info2)
                    print("[mDNS] 已注册 https://%s.local:%d" % (hostname, secure_port))
                except Exception as exc:
                    print("[mDNS] HTTPS 服务注册失败:", exc)
            return True
        except Exception as exc:
            print("[mDNS] 注册失败（不影响叫号功能）:", exc)
            return False


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


def ensure_ssl_context():
    """生成/加载自签名证书，供 HTTPS 安全端口使用（开源库 cryptography）。"""
    import datetime as _dt
    import ipaddress as _ip
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_path = os.path.join(DATA_DIR, "ssl.crt")
    key_path = os.path.join(DATA_DIR, "ssl.key")
    if not (os.path.isfile(cert_path) and os.path.isfile(key_path)):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        hostname = normalize_hostname("")
        sans = [x509.DNSName("localhost"),
                x509.DNSName(hostname),
                x509.DNSName(hostname + ".local"),
                x509.IPAddress(_ip.ip_address("127.0.0.1"))]
        for ip in get_lan_ips():
            try:
                sans.append(x509.IPAddress(_ip.ip_address(ip)))
            except Exception:
                pass
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - _dt.timedelta(days=1))
                .not_valid_after(now + _dt.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .sign(key, hashes.SHA256()))
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/monitor")
def page_monitor():
    return render_template("monitor.html")


@app.route("/remote")
def page_remote():
    return render_template("remote.html")


@app.route("/files")
def page_files():
    return render_template("files.html")


@app.route("/password")
def page_password():
    return render_template("password.html")


@app.route("/update")
def page_update():
    return render_template("update.html")


# ---------------------------------------------------------------------------
# 首次使用向导：没设置完之前，所有页面都重定向到 /wizard；设置完就再也进不去
# ---------------------------------------------------------------------------
WIZARD_EXEMPT = ("/static/", "/api/wizard", "/wizard", "/favicon.ico",
                 "/api/auth/create", "/api/auth/passwords", "/api/health")


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
            return ("<!doctype html><meta charset=utf-8>"
                    "<h3 style=\"font-family:微软雅黑;padding:40px\">"
                    "请在本机电脑上完成首次设置，手机上暂不可用。</h3>", 403)
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


@app.route("/api/wizard/state")
def api_wizard_state():
    cfg = get_config()
    port = _running_port or cfg.get("port", 8000)
    return jsonify({
        "ok": True,
        "done": wizard_done(),
        "hostname": normalize_hostname(cfg.get("mdns_hostname", "")),
        "port": port,
        "computer_name": os.environ.get("COMPUTERNAME", "") or "这台电脑",
        "roster_count": len(get_roster()),
        "autostart": bool(cfg.get("autostart")),
        "update_channel": cfg.get("update_channel", "stable"),
        "update_policy": cfg.get("update_policy", "none"),
    })


@app.route("/api/wizard/license")
def api_wizard_license():
    """给向导返回两份文本：中文全文译文 + GNU 英文原文（两个独立文件）。"""
    out = {"ok": True, "zh": "", "en": "", "legal": True}
    for key, names in (("zh", ("LICENSE-中文全文译文.txt", "LICENSE-中文参考译文.txt")),
                       ("en", ("LICENSE", "COPYING"))):
        for name in names:
            for base in (BASE_DIR, ROOT_DIR):
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
    if need_mdns:
        try:
            register_mdns(get_config())     # 立即生效，不用重启
        except Exception as exc:
            print("[向导] 应用主机名失败:", exc)

    st = load_json(STATE_PATH, {"call_count": 0})
    st["wizard_done"] = True
    st["wizard_done_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(STATE_PATH, st)

    cfg2 = get_config()
    port = _running_port or cfg2.get("port", 8000)
    host2 = normalize_hostname(cfg2.get("mdns_hostname", ""))
    return jsonify({"ok": True, "hostname": host2, "port": port,
                    "http_url": "http://%s.local:%d" % (host2, port),
                    "secure_port": cfg2.get("secure_port", 0)})


# ---------------------------------------------------------------------------
# 状态与配置 API
# ---------------------------------------------------------------------------
@app.route("/api/health")
def api_health():
    """启动自检用测试接口：HTTP/HTTPS 服务是否正常响应。"""
    ver = "2.0.0"
    try:
        with open(os.path.join(ROOT_DIR, "version.json"), "r", encoding="utf-8") as f:
            ver = str(json.load(f).get("version", "2.0.0"))
    except Exception:
        pass
    return jsonify({"ok": True, "app": "班级叫号器",
                    "version": ver, "time": int(time.time())})


@app.route("/api/status")
def api_status():
    cfg = get_config()
    state = get_state()
    roster = get_roster()
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    return jsonify({
        "ok": True,
        "port": _running_port or cfg.get("port", 8000),
        "mdns_enabled": bool(cfg.get("mdns_enabled")),
        "mdns_hostname": hostname,
        "mdns_url": "http://%s.local:%d" % (hostname, _running_port or cfg.get("port", 8000)),
        "secure_enabled": bool(cfg.get("secure_enabled", True)),
        "secure_port": int(_running_secure_port or cfg.get("secure_port", 9443)),
        "secure_url": "https://%s.local:%d" % (hostname, int(_running_secure_port or cfg.get("secure_port", 9443))),
        "local_ips": get_lan_ips(),
        "roster_count": len(roster),
        "call_count": state.get("call_count", 0),
        "zeroconf_ok": HAS_ZEROCONF,
        "tts_ok": True,
    })


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
    if "secure_port_force" in data:
        cfg["secure_port_force"] = bool(data["secure_port_force"])
    save_config(cfg)
    if changed:
        register_mdns(cfg)  # 主机名 / 开关变化立即重新注册
    return jsonify({"ok": True, "config": cfg,
                    "note": "端口修改后需重启软件生效"})


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
        if not HAS_ZEROCONF:
            checks.append({"level": "warn", "name": "mDNS",
                           "detail": "未安装 zeroconf 组件",
                           "solution": "请使用完整版软件（含 mDNS 组件）。"})
        else:
            target_ips = get_mdns_ips(cfg)
            ip = _resolve_mdns(hostname)
            if ip:
                if target_ips and ip in target_ips:
                    checks.append({"level": "ok", "name": "mDNS",
                                   "detail": "%s.local 解析到 %s（目标网卡一致）" % (hostname, ip),
                                   "solution": ""})
                else:
                    checks.append({
                        "level": "warn", "name": "mDNS",
                        "detail": "%s.local 解析到 %s，与目标网卡 IP(%s)不一致" % (
                            hostname, ip,
                            ", ".join(target_ips) if target_ips else "无"),
                        "solution": "到设置中选择正确网卡，并确认访问设备与该网卡在同一网络。",
                    })
            else:
                checks.append({"level": "warn", "name": "mDNS",
                               "detail": "本机未能解析 %s.local（其他设备可能仍能访问）" % hostname,
                               "solution": "其他设备也可直接用 http://IP:端口 访问。"})

    step("数据目录")
    try:
        probe = os.path.join(DATA_DIR, ".wtest")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        checks.append({"level": "ok", "name": "数据目录",
                       "detail": "数据目录可正常读写", "solution": ""})
    except Exception as exc:
        checks.append({"level": "error", "name": "数据目录",
                       "detail": "数据目录不可写: %s" % exc,
                       "solution": "确认 data 目录存在、未被占用，且磁盘有写入权限。"})

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


def _main():
    global _running_port, _running_secure_port, _desktop

    # 启动即申请管理员权限（用户拒绝则继续以普通权限运行）
    ensure_admin()

    set_config_getter(get_config)
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
    try:
        server = make_server("0.0.0.0", port, app, threaded=True)
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
                    if not (cfg.get("secure_port_force", False) and _is_admin()):
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
