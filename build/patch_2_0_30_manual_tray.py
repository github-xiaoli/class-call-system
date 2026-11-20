# -*- coding: utf-8 -*-
"""patch 2.0.30 - 10：托盘「📖 使用说明」+ 软件内置 /manual 页面（向下兼容：缺文件有兜底）。"""
import io

# ---------------- 1. app.py ----------------
A = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
s = io.open(A, encoding="utf-8").read()

old_imp = "from flask import Flask, Response, jsonify, render_template, request, redirect"
assert old_imp in s
s = s.replace(old_imp,
              "from flask import (Flask, Response, jsonify, render_template, request,\n"
              "                   redirect, send_file, send_from_directory)", 1)

anchor = '@app.route("/api/mdns/status")'
assert anchor in s
ROUTES = '''MANUAL_HTML = os.path.join(ROOT_DIR, "使用说明-图文版.html")
MANUAL_PDF = os.path.join(ROOT_DIR, "使用说明-图文版.pdf")
MANUAL_TXT = os.path.join(ROOT_DIR, "使用说明.txt")
MANUAL_IMG_DIR = os.path.join(ROOT_DIR, "使用说明-图片")
_manual_cache = {"key": None, "html": ""}


def _manual_html_text():
    """读图文说明书并把图片路径改成 /manual-img/（带缓存，文件更新后自动失效）。"""
    try:
        st = os.stat(MANUAL_HTML)
        key = (int(st.st_mtime), st.st_size)
        if _manual_cache["key"] == key and _manual_cache["html"]:
            return _manual_cache["html"]
        with open(MANUAL_HTML, "r", encoding="utf-8", errors="replace") as f:
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
    if os.path.isfile(MANUAL_PDF):
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
    if os.path.isfile(MANUAL_PDF):
        try:
            return send_file(MANUAL_PDF, mimetype="application/pdf", as_attachment=False,
                             download_name="班级叫号器-使用说明.pdf")
        except Exception as exc:
            print("[说明] 发送 PDF 失败:", exc)
    return redirect("/manual")


@app.route("/manual.txt")
def page_manual_txt():
    if os.path.isfile(MANUAL_TXT):
        try:
            return send_file(MANUAL_TXT, mimetype="text/plain; charset=utf-8",
                             as_attachment=False)
        except Exception:
            pass
    return redirect("/manual")


@app.route("/manual-img/<path:name>")
def page_manual_img(name):
    """图文说明书的图片。"""
    try:
        return send_from_directory(MANUAL_IMG_DIR, name, max_age=3600)
    except Exception:
        return Response("", status=404)


'''
s = s.replace(anchor, ROUTES + anchor, 1)

# 向导白名单放行 /manual（首设期间也可能需要查说明）
old_ex = '                 "/plain", "/ping", "/cert")'
assert old_ex in s
s = s.replace(old_ex, '                 "/plain", "/ping", "/cert", "/manual")', 1)
io.open(A, "w", encoding="utf-8", newline="\n").write(s)
print("ok app.py: /manual /manual.pdf /manual.txt /manual-img")

# ---------------- 2. desktop.py：托盘菜单 ----------------
D = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\desktop.py"
d = io.open(D, encoding="utf-8").read()

old_menu = '''            win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "打开网页")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 3,'''
assert old_menu in d, "托盘菜单锚点未找到"
new_menu = '''            win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "打开网页")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 4, "📖 使用说明")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 3,'''
d = d.replace(old_menu, new_menu, 1)

old_disp = '''        elif cmd == 3:
            self._open_update_page()'''
assert old_disp in d
new_disp = '''        elif cmd == 4:
            self._open_manual()
        elif cmd == 3:
            self._open_update_page()'''
d = d.replace(old_disp, new_disp, 1)

old_upd = '''    def _open_update_page(self):'''
assert old_upd in d
new_upd = '''    def _open_manual(self):
        """打开图文使用说明（软件内置页面；缺说明书文件时也有速查兜底页）。"""
        try:
            os.startfile("http://127.0.0.1:%d/manual" % self.port)
        except Exception as exc:
            print("[说明] 打开使用说明失败:", exc)

    def _open_update_page(self):'''
d = d.replace(old_upd, new_upd, 1)
io.open(D, "w", encoding="utf-8", newline="\n").write(d)
print("ok desktop.py: 托盘「📖 使用说明」")

# ---------------- 3. make_installer.py：把说明书打进安装包 ----------------
M = r"C:\Users\李语晨\Desktop\班级叫号器\build\make_installer.py"
m = io.open(M, encoding="utf-8").read()
old_list = '''    for name in ("start.bat", "启动-隐藏窗口.vbs", "示例名条.csv", "使用说明.txt", "README.md",
                 "LICENSE", "NOTICE", "LICENSE-中文全文译文.txt", "LICENSE-中文参考译文.txt"):'''
assert old_list in m, "安装包文件列表锚点未找到"
new_list = '''    for name in ("start.bat", "启动-隐藏窗口.vbs", "示例名条.csv", "使用说明.txt", "README.md",
                 "LICENSE", "NOTICE", "LICENSE-中文全文译文.txt", "LICENSE-中文参考译文.txt",
                 # 图文说明书：托盘「📖 使用说明」直接打开，所以必须随安装包一起装
                 "使用说明-图文版.html", "使用说明-图文版.pdf"):'''
m = m.replace(old_list, new_list, 1)

old_icon = '''    icon = os.path.join(ROOT, "data", "icon.ico")'''
new_icon = '''    # 说明书用到的截图（/manual-img/ 提供）
    img_src = os.path.join(ROOT, "使用说明-图片")
    if os.path.isdir(img_src):
        copy_tree(img_src, os.path.join(PAYLOAD, "使用说明-图片"))

    icon = os.path.join(ROOT, "data", "icon.ico")'''
assert old_icon in m
m = m.replace(old_icon, new_icon, 1)
io.open(M, "w", encoding="utf-8", newline="\n").write(m)
print("ok make_installer.py: 打包图文说明书")
