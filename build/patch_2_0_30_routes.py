# -*- coding: utf-8 -*-
"""patch 2.0.30 - 3：健康检查/状态接口增强 + 日志页面与接口 + mDNS 诊断接口 + /plain /ping。"""
import io

P = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
src = io.open(P, encoding="utf-8").read()

# ---- 1. 引入 urllib.parse（导出文件名用） ----
old_imp = "import urllib.request\nimport uuid"
assert old_imp in src
src = src.replace(old_imp, "import urllib.parse\nimport urllib.request\nimport uuid", 1)

# ---- 2. 替换 /api/health 与 /api/status 两个接口 ----
start = src.index('@app.route("/api/health")')
end = src.index('@app.route("/api/port_check")')
NEW = '''def _app_version():
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
    return Response("OK %s %s\\n" % (_app_version(), time.strftime("%Y-%m-%d %H:%M:%S")),
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
        "① 改用 <code>https://主机名.local:%d</code> 再试；"
        "② 清一下浏览器缓存或换个浏览器；③ 在 iPhone 上给浏览器开「本地网络」权限。</p>"
        % (_safe_host(request.scheme), _safe_host(request.host),
           _safe_host(request.remote_addr), _app_version(), items, sp)
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
    resp = Response("\\ufeff" + text, mimetype="text/plain; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        "attachment; filename=%s; filename*=UTF-8''%s"
        % (fn, urllib.parse.quote("班级叫号器-日志-%s.txt"
                                  % time.strftime("%Y%m%d-%H%M%S"))))
    return resp


@app.route("/api/mdns/status")
def api_mdns_status():
    return jsonify(dict(mdns_status(), ok=True))


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
    st["ok"] = bool(ok)
    return jsonify(st)


'''

src = src[:start] + NEW + src[end:]

# ---- 3. 向导白名单里放行 /plain /ping ----
old_ex = '                 "/pair", "/api/auth/activate")'
assert old_ex in src
src = src.replace(old_ex, '                 "/pair", "/api/auth/activate",\n'
                          '                 # 手机白屏排查用的极简页（不含脚本，最不容易被拦）\n'
                          '                 "/plain", "/ping")', 1)

io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("ok, routes added")
