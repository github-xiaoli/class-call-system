# -*- coding: utf-8 -*-
"""
OTA 软件更新模块：
- GitHub Release 检查（按通道：dev / beta / stable）
- CDN 自动测速选最快
- 下载(0-50%) -> 解压安装(50-99%) -> 更新版本记录(99-100%)
- 手动上传 zip 更新
- 网页进度查询 + 电脑端更新窗口
"""
import json
import os
import shutil
import subprocess
import threading
import time
import zipfile

from flask import Blueprint, jsonify, request

update_bp = Blueprint("update", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")

GITHUB_REPO = "github-xiaoli/class-call-system"
GITHUB_API = "https://api.github.com/repos/" + GITHUB_REPO
GITHUB_WEB = "https://github.com/" + GITHUB_REPO

# 更新通道
CHANNELS = ("stable", "beta", "dev")

# CDN 加速前缀（自动测速选最快）
CDN_BASES = [
    "https://gh-proxy.com/",
    "https://gh.monlor.com/",
    "https://down.mxw.xx.kg/",
    "https://777.z321.cc.cd/",
    "https://gh.acmsz.top/",
    "https://tvv.tw/",
    "https://gg.z321.cc.cd/",
    "https://ghproxy.imciel.com/",
    "https://gh.my-website.ccwu.cc/",
    "https://g.z321.cc.cd/",
    "https://git.yylx.win/",
    "https://xsadwsd.kdns.fr/",
    "https://gh.idayer.com/",
    "https://cdn.akaere.online/",
    "https://fastgit.cc/",
    "https://ghfile.geekertao.top/",
    "https://gh.07150721.xyz/",
    "https://github.tbap.top/",
    "https://gh.noki.icu/",
    "https://cdn.gh-proxy.com/",
    "https://ghfast.top/",
    "https://free.cn.eu.org/",
    "https://gh.dpik.top/",
    "https://git.669966.xyz/",
    "https://gh.sixyin.com/",
    "https://ghproxy.net/",
    "https://gitproxy.127731.xyz/",
    "https://ghproxy.monkeyray.net/",
    "https://github.ednovas.xyz/",
    "https://github.chenc.dev/",
    "https://g.blfrp.cn/",
]

# 全局进度状态
_lock = threading.Lock()
_state = {
    "running": False,
    "stage": "idle",      # idle | check | speedtest | download | extract | final | done | error
    "percent": 0,
    "detail": "",
    "error": "",
    "version": "",
    "current_version": "",
}

_config_getter = None
_restart_callback = None
_desktop = None


def set_config_getter(fn):
    global _config_getter
    _config_getter = fn


def set_restart_callback(fn):
    global _restart_callback
    _restart_callback = fn


def set_desktop(d):
    global _desktop
    _desktop = d


def _cfg():
    try:
        return (_config_getter or (lambda: {}))()
    except Exception:
        return {}


def _set(percent=None, stage=None, detail=None, error=None, version=None, running=None):
    with _lock:
        if percent is not None:
            _state["percent"] = int(percent)
        if stage is not None:
            _state["stage"] = stage
        if detail is not None:
            _state["detail"] = str(detail)
        if error is not None:
            _state["error"] = str(error)
        if version is not None:
            _state["version"] = str(version)
        if running is not None:
            _state["running"] = bool(running)
        snap = dict(_state)
    if _desktop is not None:
        try:
            _desktop.set_update_progress(snap["percent"], snap["detail"])
        except Exception:
            pass
    return snap


def _get_state():
    with _lock:
        return dict(_state)


def _http_get(url, timeout=20, binary=False, headers=None):
    import urllib.request
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    hdrs = {"User-Agent": "ClassCaller-OTA/2.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8-sig", errors="replace")


def _github_api(path):
    """GitHub API：直连失败时走第一个 CDN 代理。"""
    try:
        return json.loads(_http_get(GITHUB_API + path, timeout=15))
    except Exception:
        try:
            return json.loads(_http_get("https://gh-proxy.com/" + GITHUB_API + path, timeout=15))
        except Exception:
            return None


def _find_latest(channel):
    """返回匹配通道的最新 release 信息；找不到返回 None。"""
    releases = _github_api("/releases?per_page=20")
    if not isinstance(releases, list):
        return None
    for rel in releases:
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and channel == "stable":
            continue
        info = None
        info_url = ""
        zip_asset = None
        for asset in rel.get("assets", []):
            name = asset.get("name", "")
            if name == "release-info.json":
                info_url = asset.get("browser_download_url", "")
                try:
                    info = json.loads(_http_get(info_url, timeout=15))
                except Exception:
                    info = None
            elif name.lower().endswith(".zip"):
                zip_asset = asset
        if not info or not zip_asset:
            continue
        if info.get("channel", "stable") == channel:
            return {
                "version": str(info.get("version", "")),
                "channel": str(info.get("channel", channel)),
                "notes": str(info.get("notes", "")),
                "zip_url": zip_asset.get("browser_download_url", ""),
                "zip_asset": zip_asset.get("name", ""),
                "tag": rel.get("tag_name", ""),
                "info_url": info_url,
            }
    return None


def _cdn_speedtest(info_url):
    """逐个 CDN 下载 release-info.json 测速，返回最快的 CDN 前缀。"""
    if not info_url:
        return CDN_BASES[0]
    _set(stage="speedtest", percent=1, detail="正在测速选择最快线路…")
    best = None
    best_ms = None
    for base in CDN_BASES:
        url = base + info_url
        t0 = time.time()
        try:
            data = _http_get(url, timeout=8, binary=True)
            if not data:
                continue
            ms = (time.time() - t0) * 1000
            if best_ms is None or ms < best_ms:
                best_ms = ms
                best = base
        except Exception:
            continue
    return best or CDN_BASES[0]


def _download(url, dest, p0, p1):
    """带进度下载，p0..p1 百分比区间。"""
    import urllib.request
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "ClassCaller-OTA/2.0"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total > 0:
                    pct = p0 + int((done / total) * (p1 - p0))
                else:
                    pct = p0
                _set(percent=pct, stage="download",
                     detail="正在下载 %d / %d KB" % (done // 1024, total // 1024))
        return total or done


def _install_zip(zip_path, new_version):
    """解压并安装新版本，返回新版本目录名。"""
    _set(stage="extract", percent=50, detail="正在解压更新包…")
    tmp = os.path.join(ROOT_DIR, "_update_tmp")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        total = len(names) or 1
        tmp_abs = os.path.abspath(tmp)
        for i, n in enumerate(names):
            # 防 zip 路径穿越
            target = os.path.abspath(os.path.join(tmp, n))
            if not target.startswith(tmp_abs + os.sep) and target != tmp_abs:
                raise RuntimeError("更新包包含非法路径")
            zf.extract(n, tmp)
            if i % 200 == 0 or i == total - 1:
                pct = 50 + int((i + 1) / total * 49)
                _set(percent=pct, stage="extract",
                     detail="正在解压 %d/%d" % (i + 1, total))

    # 找新版本目录
    entries = [e for e in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, e))]
    if new_version and new_version in entries:
        src = os.path.join(tmp, new_version)
    elif entries:
        src = os.path.join(tmp, entries[0])
    else:
        src = tmp
        new_version = new_version or "unknown"

    # 校验包含 app.py
    if not os.path.isfile(os.path.join(src, "app.py")):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("更新包内未找到 app.py，不是有效的更新包")

    dest = os.path.join(ROOT_DIR, new_version)
    if os.path.isdir(dest):
        backup = dest + ".old_" + time.strftime("%Y%m%d%H%M%S")
        shutil.move(dest, backup)

    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    shutil.move(src, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return new_version


def _update_version_file(new_version):
    _set(stage="final", percent=99, detail="正在更新版本记录…")
    vpath = os.path.join(ROOT_DIR, "version.json")
    data = {}
    try:
        with open(vpath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass
    data["version"] = new_version
    with open(vpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _do_update(info, zip_path=None, install=True):
    """后台执行更新。install=False 时只下载不安装（自动静默下载策略）。"""
    try:
        if _desktop is not None and install:
            try:
                _desktop.show_update_window()
            except Exception:
                pass
        _set(running=True, stage="start", percent=0, detail="准备更新…", version=info.get("version", ""))

        if zip_path is None:
            base = _cdn_speedtest(info.get("info_url", "") or info.get("zip_url", ""))
            url = base + info.get("zip_url", "")
            _set(stage="download", percent=2, detail="开始下载…")
            cache = os.path.join(DATA_DIR, "update_cache")
            os.makedirs(cache, exist_ok=True)
            local_zip = os.path.join(cache, "update_" + info.get("version", "tmp") + ".zip")
            _download(url, local_zip, 2, 50)
        else:
            local_zip = zip_path

        if not install:
            _set(stage="done", percent=50, detail="已下载，等待手动安装",
                 version=info.get("version", ""))
            return

        new_version = _install_zip(local_zip, info.get("version", ""))
        _update_version_file(new_version)

        _set(stage="done", percent=100, detail="更新完成，正在重启…", version=new_version)
        time.sleep(1.2)
        if _desktop is not None:
            try:
                _desktop.close_update_window()
            except Exception:
                pass
        if _restart_callback is not None:
            try:
                _restart_callback(new_version)
            except Exception:
                pass
        else:
            os._exit(0)
    except Exception as exc:
        _set(stage="error", percent=_get_state().get("percent", 0),
             error=str(exc), detail="更新失败：" + str(exc))
        if _desktop is not None:
            try:
                _desktop.close_update_window()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _need_auth():
    from auth import check_access
    return check_access()


@update_bp.route("/api/update/config", methods=["GET", "POST"])
def api_update_config():
    err = _need_auth()
    if err:
        return err
    cfg = _cfg()
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "update_channel": cfg.get("update_channel", "stable"),
            "update_policy": cfg.get("update_policy", "none"),
            "current_version": _current_version(),
        })
    data = request.get_json(silent=True) or {}
    new_cfg = {}
    if "update_channel" in data and data["update_channel"] in CHANNELS:
        new_cfg["update_channel"] = data["update_channel"]
    if "update_policy" in data and data["update_policy"] in ("none", "check", "download", "install"):
        new_cfg["update_policy"] = data["update_policy"]
    if new_cfg:
        try:
            from app import get_config, save_config
            c = get_config()
            c.update(new_cfg)
            save_config(c)
        except Exception:
            pass
    return jsonify({"ok": True})


def _current_version():
    try:
        with open(os.path.join(ROOT_DIR, "version.json"), "r", encoding="utf-8") as f:
            return str(json.load(f).get("version", "2.0.0"))
    except Exception:
        return "2.0.0"


@update_bp.route("/api/update/check", methods=["GET"])
def api_update_check():
    err = _need_auth()
    if err:
        return err
    channel = _cfg().get("update_channel", "stable")
    _set(stage="check", percent=1, detail="正在检查更新…", running=True)
    try:
        info = _find_latest(channel)
        _set(stage="idle", percent=0, running=False, detail="检查完成")
        if not info:
            return jsonify({"ok": True, "latest": None,
                            "current": _current_version(), "channel": channel})
        return jsonify({"ok": True, "latest": info,
                        "current": _current_version(), "channel": channel})
    except Exception as exc:
        _set(stage="idle", percent=0, running=False, detail="检查失败")
        return jsonify({"ok": False, "error": str(exc)}), 500


@update_bp.route("/api/update/start", methods=["POST"])
def api_update_start():
    err = _need_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    channel = data.get("channel") or _cfg().get("update_channel", "stable")
    _set(stage="check", percent=0, detail="正在获取更新信息…", running=True)
    try:
        info = _find_latest(channel)
        if not info:
            _set(stage="idle", running=False, detail="已是最新版本")
            return jsonify({"ok": False, "error": "已是最新版本，无需更新"}), 400
        if info.get("version") == _current_version():
            _set(stage="idle", running=False, detail="已是最新版本")
            return jsonify({"ok": False, "error": "已是最新版本"}), 400
        threading.Thread(target=_do_update, args=(info,), daemon=True, name="ota-update").start()
        return jsonify({"ok": True, "message": "开始更新", "version": info.get("version")})
    except Exception as exc:
        _set(stage="error", running=False, error=str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 500


@update_bp.route("/api/update/upload", methods=["POST"])
def api_update_upload():
    err = _need_auth()
    if err:
        return err
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "未收到文件"}), 400
    cache = os.path.join(DATA_DIR, "update_cache")
    os.makedirs(cache, exist_ok=True)
    safe = "manual_" + time.strftime("%Y%m%d%H%M%S") + ".zip"
    path = os.path.join(cache, safe)
    f.save(path)
    # 从压缩包猜版本号：取第一层目录名
    try:
        with zipfile.ZipFile(path) as zf:
            top = [n for n in zf.namelist() if "/" in n and not n.startswith("__")]
            ver = top[0].split("/")[0] if top else ""
    except Exception:
        ver = ""
    info = {"version": ver or "", "notes": "手动上传", "channel": "manual"}
    threading.Thread(target=_do_update, args=(info, path), daemon=True, name="ota-upload").start()
    return jsonify({"ok": True, "message": "开始安装手动更新包"})


@update_bp.route("/api/update/progress", methods=["GET"])
def api_update_progress():
    err = _need_auth()
    if err:
        return err
    snap = _get_state()
    snap["current_version"] = _current_version()
    return jsonify({"ok": True, "progress": snap})


def maybe_auto_update():
    """启动时按策略自动检查/下载/安装。"""
    policy = _cfg().get("update_policy", "none")
    if policy == "none":
        return
    channel = _cfg().get("update_channel", "stable")

    def work():
        try:
            info = _find_latest(channel)
            if not info or info.get("version") == _current_version():
                return
            if policy == "check":
                print("[OTA] 发现新版本", info.get("version"))
                return
            if policy == "download":
                threading.Thread(target=_do_update,
                                 args=(info,), kwargs={"install": False},
                                 daemon=True, name="ota-download").start()
            elif policy == "install":
                threading.Thread(target=_do_update, args=(info,),
                                 daemon=True, name="ota-install").start()
        except Exception as exc:
            print("[OTA] 自动检查失败:", exc)

    threading.Thread(target=work, daemon=True, name="ota-auto").start()
