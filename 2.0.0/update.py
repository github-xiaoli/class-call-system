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
import re
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

# 更新通道（数字越大越"激进"）：高通道能收到低通道的版本
# dev 能收 dev/beta/stable，beta 能收 beta/stable，stable 只收 stable
CHANNELS = ("stable", "beta", "dev")
CHANNEL_RANK = {"stable": 1, "beta": 2, "dev": 3}

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
    "stage": "idle",      # idle | check | speedtest | download | extract | ready | final | done | error
    "percent": 0,
    "detail": "",
    "error": "",
    "version": "",
    "current_version": "",
}

# 已下载待安装的版本（两段式更新：先下载解压，再手动安装重启）
_prep_lock = threading.Lock()
_prepared = {"version": "", "path": "", "python_delta": ""}

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
    """返回该通道可见的最新 release。

    通道向下兼容：dev 收 dev/beta/stable，beta 收 beta/stable，stable 只收 stable。
    在可见范围内取版本号最高的一个（同版本优先更稳定的通道）。
    """
    releases = _github_api("/releases?per_page=50")
    if not isinstance(releases, list):
        return None
    rank = CHANNEL_RANK.get(channel, 1)
    best_key = None
    best = None
    for rel in releases:
        if rel.get("draft"):
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
        rel_channel = str(info.get("channel", "stable") or "stable").strip().lower()
        if rel_channel not in CHANNEL_RANK:
            rel_channel = "stable"
        rel_rank = CHANNEL_RANK[rel_channel]
        # 该版本比当前通道更"激进"→ 不可见
        if rel_rank > rank:
            continue
        # 标了预发布却声明 stable 的，保守起见只给 dev 通道
        if rel.get("prerelease") and rel_channel == "stable" and rank < 3:
            continue
        cand = {
            "version": str(info.get("version", "")),
            "channel": rel_channel,
            "notes": str(info.get("notes", "")),
            "zip_url": zip_asset.get("browser_download_url", ""),
            "zip_asset": zip_asset.get("name", ""),
            "tag": rel.get("tag_name", ""),
            "info_url": info_url,
        }
        key = (_parse_ver(cand["version"]), -rel_rank)
        if best_key is None or key > best_key:
            best_key = key
            best = cand
    return best


def _cdn_speedtest(info_url):
    """并发探测各 CDN（下载 release-info.json），返回最快的 CDN 前缀。

    并发 12 路 + 单个 4s 超时，整体约 5~10 秒完成，避免串行等待。
    """
    if not info_url:
        return CDN_BASES[0]
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(CDN_BASES)
    _set(stage="speedtest", percent=3, detail="正在测速选择最快线路（0/%d）…" % total)

    def probe(base):
        t0 = time.time()
        try:
            data = _http_get(base + info_url, timeout=4, binary=True)
            if data:
                return base, (time.time() - t0) * 1000.0
        except Exception:
            pass
        return base, None

    best = None
    best_ms = None
    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(probe, b) for b in CDN_BASES]
        for fut in as_completed(futures):
            try:
                base, ms = fut.result()
            except Exception:
                continue
            done += 1
            if ms is not None and (best_ms is None or ms < best_ms):
                best_ms = ms
                best = base
            _set(percent=3 + int(done / total * 12),
                 detail="正在测速选择最快线路（%d/%d，最快 %s）…"
                        % (done, total, ("%.0fms" % best_ms) if best_ms else "—"))
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
    _set(stage="extract", percent=55, detail="正在解压更新包…")
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
                pct = 55 + int((i + 1) / total * 37)
                _set(percent=pct, stage="extract",
                     detail="正在解压 %d/%d" % (i + 1, total))

    # 找新版本目录（python/ 是运行环境增量，不算版本目录）
    entries = [e for e in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, e))]
    py_delta = os.path.join(tmp, "python")
    has_py_delta = os.path.isdir(py_delta)
    ver_entries = [e for e in entries if e.lower() != "python"]
    if new_version and new_version in ver_entries:
        src = os.path.join(tmp, new_version)
    elif ver_entries:
        src = os.path.join(tmp, ver_entries[0])
        if not new_version:
            new_version = ver_entries[0]
    else:
        src = None

    # 校验包含 app.py
    if not src or not os.path.isfile(os.path.join(src, "app.py")):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("更新包内未找到 app.py，不是有效的更新包")

    # 运行环境增量：搬到不会被清理的临时目录，安装阶段再合并
    pending_py = os.path.join(ROOT_DIR, "_update_py_pending")
    if os.path.isdir(pending_py):
        shutil.rmtree(pending_py, ignore_errors=True)
    if has_py_delta:
        shutil.move(py_delta, pending_py)

    dest = os.path.join(ROOT_DIR, new_version)
    if os.path.isdir(dest):
        backup = dest + ".old_" + time.strftime("%Y%m%d%H%M%S")
        shutil.move(dest, backup)

    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    shutil.move(src, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return new_version, (pending_py if has_py_delta else "")


def _apply_python_delta(pending_py):
    """把待安装的 python 增量合并进 ROOT/python（覆盖同名文件，跳过被占用的）。

    返回 (已更新文件数, 跳过文件数)。
    """
    if not pending_py or not os.path.isdir(pending_py):
        return 0, 0
    target_root = os.path.join(ROOT_DIR, "python")
    copied = 0
    skipped = 0
    pending_abs = os.path.abspath(pending_py)
    for dirpath, _dirnames, filenames in os.walk(pending_py):
        rel = os.path.relpath(dirpath, pending_py)
        out_dir = target_root if rel == "." else os.path.join(target_root, rel)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            skipped += len(filenames)
            continue
        for name in filenames:
            src = os.path.join(dirpath, name)
            if not os.path.abspath(src).startswith(pending_abs):
                continue
            dst = os.path.join(out_dir, name)
            try:
                shutil.copy2(src, dst)
                copied += 1
            except Exception:
                skipped += 1
            _set(stage="final", percent=98,
                 detail="正在更新运行环境（%d 个文件）…" % copied)
    shutil.rmtree(pending_py, ignore_errors=True)
    return copied, skipped


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


def _close_desktop_window():
    if _desktop is not None:
        try:
            _desktop.close_update_window()
        except Exception:
            pass


def _clear_prepared():
    with _prep_lock:
        _prepared.clear()
        _prepared.update({"version": "", "path": "", "python_delta": ""})


def _set_prepared(version, path, python_delta=""):
    with _prep_lock:
        _prepared.update({"version": version, "path": path,
                          "python_delta": python_delta or ""})


def _get_prepared():
    with _prep_lock:
        return dict(_prepared)


def _do_prepare(info=None, zip_path=None, channel=None):
    """下载 + 解压到新版本目录，但不安装（不改 version.json、不重启）。

    完成后状态为 stage="ready"，等待用户点「安装并重启」。
    """
    try:
        if _desktop is not None:
            try:
                _desktop.show_update_window()
            except Exception:
                pass

        _set(running=True, stage="check", percent=0, detail="正在检查更新…")

        if info is None:
            info = _find_latest(channel or _cfg().get("update_channel", "stable"))
            if not info:
                _set(stage="idle", percent=0, running=False,
                     detail="检查失败或暂无可用更新")
                _close_desktop_window()
                return False
        new_version = str(info.get("version", "") or "")
        if zip_path is None and new_version and not _is_newer(new_version, _current_version()):
            _set(stage="idle", percent=0, running=False, detail="已是最新版本",
                 version=new_version)
            _close_desktop_window()
            return False

        _set(running=True, stage="start", percent=0,
             detail="准备下载更新包…", version=new_version)

        if zip_path is None:
            base = _cdn_speedtest(info.get("info_url", "") or info.get("zip_url", ""))
            url = base + info.get("zip_url", "")
            _set(stage="download", percent=15, detail="开始下载…")
            cache = os.path.join(DATA_DIR, "update_cache")
            os.makedirs(cache, exist_ok=True)
            local_zip = os.path.join(cache, "update_" + (new_version or "tmp") + ".zip")
            _download(url, local_zip, 15, 55)
        else:
            local_zip = zip_path

        ready_version, py_delta = _install_zip(local_zip, new_version)
        dest = os.path.join(ROOT_DIR, ready_version)
        _set_prepared(ready_version, dest, py_delta)

        _set(stage="ready", percent=100, running=False, version=ready_version,
             detail=("已下载并解压完成（含运行环境更新），请点击「安装并重启」" if py_delta
                     else "已下载并解压完成，请点击「安装并重启」"))
        _close_desktop_window()
        return True
    except Exception as exc:
        _set(stage="error", percent=_get_state().get("percent", 0), running=False,
             error=str(exc), detail="下载失败：" + str(exc))
        _close_desktop_window()
        return False


REQUIRED_MODULES = ("flask", "zeroconf", "win32api", "win32gui", "PIL")


def _verify_env(target_version=None):
    """用目标版本代码检查依赖能否导入，返回 (ok, 错误文本)。

    防止更新后缺少/不兼容新版库导致启动失败。
    """
    pyw = os.path.join(ROOT_DIR, "python", "python.exe")
    if not os.path.isfile(pyw):
        return True, ""
    code_dir = os.path.join(ROOT_DIR, target_version) if target_version else BASE_DIR
    snippet = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "mods = %r\n"
        "bad = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        __import__(m)\n"
        "    except Exception as e:\n"
        "        bad.append(m + ': ' + str(e))\n"
        "print('OK' if not bad else 'BAD|' + '; '.join(bad))\n"
    ) % (code_dir, list(REQUIRED_MODULES))
    try:
        out = subprocess.run([pyw, "-c", snippet], capture_output=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (out.stdout or b"").decode("utf-8", "replace").strip()
        err = (out.stderr or b"").decode("utf-8", "replace").strip()
        if text.startswith("OK"):
            return True, ""
        return False, text or err or "依赖检查失败"
    except Exception as exc:
        return False, "依赖检查异常：%s" % exc


def _do_install():
    """把已解压的版本装上：更新运行环境 + version.json，然后重启。"""
    try:
        prep = _get_prepared()
        version = prep.get("version", "")
        path = prep.get("path", "")
        pending_py = prep.get("python_delta", "")
        if not version or not os.path.isdir(path):
            _set(stage="error", percent=0, running=False,
                 error="没有已下载的更新包", detail="请先点击「开始更新」下载")
            return False

        if _desktop is not None:
            try:
                _desktop.show_update_window()
            except Exception:
                pass

        # 1) 先更新运行环境（如果有 python/ 增量）
        if pending_py:
            _set(running=True, stage="final", percent=96, version=version,
                 detail="正在更新运行环境…")
            copied, skipped = _apply_python_delta(pending_py)
            if skipped:
                _set(percent=97, detail="运行环境已更新（%d 个，%d 个被占用跳过）"
                     % (copied, skipped))

        # 2) 依赖自检：缺库就不切换，避免更新后起不来
        _set(running=True, stage="final", percent=98, version=version,
             detail="正在检查运行环境…")
        ok, err = _verify_env(version)
        if not ok:
            _set(stage="error", percent=98, running=False, version=version,
                 error=err,
                 detail="运行环境检查未通过，已取消安装（原版本继续可用）：" + err)
            _close_desktop_window()
            return False

        # 3) 切换版本
        _set(running=True, stage="final", percent=99, version=version,
             detail="正在安装 %s…" % version)
        _update_version_file(version)
        _clear_prepared()

        _set(stage="done", percent=100, running=False,
             detail="安装完成，正在重启…", version=version)
        time.sleep(1.2)
        _close_desktop_window()
        if _restart_callback is not None:
            try:
                _restart_callback(version)
            except Exception:
                pass
        else:
            os._exit(0)
        return True
    except Exception as exc:
        _set(stage="error", percent=_get_state().get("percent", 0), running=False,
             error=str(exc), detail="安装失败：" + str(exc))
        _close_desktop_window()
        return False


def _do_update(info=None, zip_path=None, install=True, channel=None):
    """兼容入口：prepare（+ 可选立即安装）。install=False 时只下载解压不安装。"""
    ok = _do_prepare(info, zip_path, channel)
    if not ok or not install:
        return ok
    return _do_install()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _need_auth():
    from auth import check_access
    return check_access()


def _json_body():
    """容错取 JSON 请求体：即使前端没带 Content-Type 也能解析。"""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    try:
        raw = request.get_data(cache=True) or b""
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


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
    data = _json_body()
    new_cfg = {}
    if "update_channel" in data and data["update_channel"] in CHANNELS:
        new_cfg["update_channel"] = data["update_channel"]
    if "update_policy" in data and data["update_policy"] in ("none", "check", "download", "install"):
        new_cfg["update_policy"] = data["update_policy"]
    if not new_cfg:
        return jsonify({"ok": False,
                        "error": "没有可保存的设置（请求体未收到）"}), 400
    try:
        from app import get_config, save_config
        c = get_config()
        c.update(new_cfg)
        save_config(c)
    except Exception as exc:
        return jsonify({"ok": False, "error": "保存失败：%s" % exc}), 500
    cfg2 = _cfg()
    return jsonify({
        "ok": True,
        "update_channel": cfg2.get("update_channel", "stable"),
        "update_policy": cfg2.get("update_policy", "none"),
        "saved": list(new_cfg.keys()),
    })


def _current_version():
    try:
        with open(os.path.join(ROOT_DIR, "version.json"), "r", encoding="utf-8") as f:
            return str(json.load(f).get("version", "2.0.0"))
    except Exception:
        return "2.0.0"


def _parse_ver(v):
    """把 "v2.0.10-beta" 解析为 (2, 0, 10)，用于正确比较大小。"""
    s = re.sub(r"^[^\d]+", "", str(v or "").strip())
    parts = []
    for seg in re.split(r"[.\-_+~]", s):
        m = re.match(r"^(\d+)", seg)
        parts.append(int(m.group(1)) if m else 0)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _is_newer(remote, current):
    """remote 是否比 current 新。"""
    return _parse_ver(remote) > _parse_ver(current)


@update_bp.route("/api/update/check", methods=["GET"])
def api_update_check():
    err = _need_auth()
    if err:
        return err
    channel = _cfg().get("update_channel", "stable")
    cur = _current_version()
    _set(stage="check", percent=1, detail="正在检查更新…", running=True)
    try:
        info = _find_latest(channel)
        _set(stage="idle", percent=0, running=False, detail="检查完成")
        if not info:
            return jsonify({"ok": True, "latest": None, "up_to_date": True,
                            "current": cur, "channel": channel,
                            "message": "未找到可用更新"})
        if not _is_newer(info.get("version", ""), cur):
            # 已是最新（或远端更低），不再提示「发现新版本」
            return jsonify({"ok": True, "latest": None, "up_to_date": True,
                            "current": cur, "channel": channel,
                            "remote": {"version": info.get("version", ""),
                                       "channel": info.get("channel", channel),
                                       "notes": info.get("notes", "")},
                            "message": "已是最新版本"})
        return jsonify({"ok": True, "latest": info, "up_to_date": False,
                        "current": cur, "channel": channel})
    except Exception as exc:
        _set(stage="idle", percent=0, running=False, detail="检查失败")
        return jsonify({"ok": False, "error": str(exc)}), 500


@update_bp.route("/api/update/start", methods=["POST"])
def api_update_start():
    """第一段：下载 + 解压（不安装、不改 version.json、不重启）。"""
    err = _need_auth()
    if err:
        return err
    data = _json_body()
    channel = data.get("channel") or _cfg().get("update_channel", "stable")
    if _get_state().get("running"):
        return jsonify({"ok": True, "message": "更新已在进行中"})
    _set(stage="check", percent=0, detail="正在检查更新…", running=True)
    threading.Thread(target=_do_prepare,
                     args=(None, None, channel),
                     daemon=True, name="ota-prepare").start()
    return jsonify({"ok": True, "message": "开始下载更新包", "channel": channel})


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
    return jsonify({"ok": True, "message": "开始下载并安装手动更新包"})


@update_bp.route("/api/update/install", methods=["POST"])
def api_update_install():
    """第二段：把已下载解压的版本安装上并重启。"""
    err = _need_auth()
    if err:
        return err
    prep = _get_prepared()
    if not prep.get("version"):
        return jsonify({"ok": False, "error": "还没有下载好的更新包，请先点击「开始更新」"}), 400
    if not os.path.isdir(prep.get("path", "")):
        _clear_prepared()
        return jsonify({"ok": False, "error": "更新包目录不完整，请重新下载"}), 400
    if _get_state().get("running"):
        return jsonify({"ok": True, "message": "正在处理中"})
    threading.Thread(target=_do_install, daemon=True, name="ota-install").start()
    return jsonify({"ok": True, "message": "开始安装并重启", "version": prep.get("version")})


@update_bp.route("/api/update/progress", methods=["GET"])
def api_update_progress():
    err = _need_auth()
    if err:
        return err
    snap = _get_state()
    snap["current_version"] = _current_version()
    prep = _get_prepared()
    snap["ready"] = bool(prep.get("version")) and os.path.isdir(prep.get("path", ""))
    snap["ready_version"] = prep.get("version", "")
    snap["python_delta"] = bool(prep.get("python_delta"))
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
            if not info or not _is_newer(info.get("version", ""), _current_version()):
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
