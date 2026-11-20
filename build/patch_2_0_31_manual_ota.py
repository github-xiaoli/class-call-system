# -*- coding: utf-8 -*-
"""patch 2.0.31：说明书随代码一起下发（OTA 也能拿到），/manual 优先读版本目录。

原因：说明书原来只由安装包放到安装根目录；通过 OTA 升级上来的电脑根目录没有这些文件，
托盘的「📖 使用说明」就只能看到兜底速查页。现在把 HTML + 截图放进版本目录（随 OTA 下发），
/manual 先找版本目录、再找安装根目录，两种安装方式都能拿到完整图文。
"""
import io
import os
import shutil

ROOT = r"C:\Users\李语晨\Desktop\班级叫号器"
APP = os.path.join(ROOT, "2.0.0", "app.py")
s = io.open(APP, encoding="utf-8").read()

old_block = '''MANUAL_HTML = os.path.join(ROOT_DIR, "使用说明-图文版.html")
MANUAL_PDF = os.path.join(ROOT_DIR, "使用说明-图文版.pdf")
MANUAL_TXT = os.path.join(ROOT_DIR, "使用说明.txt")
MANUAL_IMG_DIR = os.path.join(ROOT_DIR, "使用说明-图片")
_manual_cache = {"key": None, "html": ""}'''
assert old_block in s, "说明书常量块未找到"

NEW = '''# 说明书的位置：优先当前版本目录（随 OTA 一起下发），其次安装根目录（安装包放的）
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
    return _manual_path("使用说明-图片", want_dir=True)'''
s = s.replace(old_block, NEW, 1)

# _manual_html_text 使用新路径
old_read = '''    try:
        st = os.stat(MANUAL_HTML)
        key = (int(st.st_mtime), st.st_size)
        if _manual_cache["key"] == key and _manual_cache["html"]:
            return _manual_cache["html"]
        with open(MANUAL_HTML, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()'''
assert old_read in s
new_read = '''    path = _manual_html_path()
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = (path, int(st.st_mtime), st.st_size)
        if _manual_cache["key"] == key and _manual_cache["html"]:
            return _manual_cache["html"]
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()'''
s = s.replace(old_read, new_read, 1)

# 三个下载/图片路由改用查找函数
s = s.replace('''    if os.path.isfile(MANUAL_PDF):
        return redirect("/manual.pdf")''',
              '''    if _manual_pdf_path():
        return redirect("/manual.pdf")''', 1)
s = s.replace('''@app.route("/manual.pdf")
def page_manual_pdf():
    if os.path.isfile(MANUAL_PDF):
        try:
            return send_file(MANUAL_PDF, mimetype="application/pdf", as_attachment=False,
                             download_name="班级叫号器-使用说明.pdf")''',
              '''@app.route("/manual.pdf")
def page_manual_pdf():
    pdf = _manual_pdf_path()
    if pdf:
        try:
            return send_file(pdf, mimetype="application/pdf", as_attachment=False,
                             download_name="班级叫号器-使用说明.pdf")''', 1)
s = s.replace('''@app.route("/manual.txt")
def page_manual_txt():
    if os.path.isfile(MANUAL_TXT):
        try:
            return send_file(MANUAL_TXT, mimetype="text/plain; charset=utf-8",
                             as_attachment=False)''',
              '''@app.route("/manual.txt")
def page_manual_txt():
    txt = _manual_txt_path()
    if txt:
        try:
            return send_file(txt, mimetype="text/plain; charset=utf-8",
                             as_attachment=False)''', 1)
s = s.replace('''    try:
        return send_from_directory(MANUAL_IMG_DIR, name, max_age=3600)
    except Exception:
        return Response("", status=404)''',
              '''    img_dir = _manual_img_dir()
    if not img_dir:
        return Response("", status=404)
    try:
        return send_from_directory(img_dir, name, max_age=3600)
    except Exception:
        return Response("", status=404)''', 1)
io.open(APP, "w", encoding="utf-8", newline="\n").write(s)
print("ok app.py: 说明书双目录查找")

# 说明书 HTML + 截图复制进版本目录（随 OTA 下发）
src_html = os.path.join(ROOT, "使用说明-图文版.html")
src_img = os.path.join(ROOT, "使用说明-图片")
dst_html = os.path.join(ROOT, "2.0.0", "使用说明-图文版.html")
dst_img = os.path.join(ROOT, "2.0.0", "使用说明-图片")
if os.path.isfile(src_html):
    shutil.copy2(src_html, dst_html)
    print("  已放入版本目录:", dst_html)
if os.path.isdir(src_img):
    shutil.rmtree(dst_img, ignore_errors=True)
    shutil.copytree(src_img, dst_img)
    print("  已放入版本目录图片:", len(os.listdir(dst_img)), "张")

# 安装包：根目录只放 PDF（HTML/图片已在版本目录里，避免重复撑大安装包）
M = os.path.join(ROOT, "build", "make_installer.py")
m = io.open(M, encoding="utf-8").read()
old = '''                 # 图文说明书：托盘「📖 使用说明」直接打开，所以必须随安装包一起装
                 "使用说明-图文版.html", "使用说明-图文版.pdf"):'''
assert old in m
new = '''                 # 图文说明书的 PDF 放安装根目录（HTML 与截图随版本目录一起装，见下）
                 "使用说明-图文版.pdf"):'''
m = m.replace(old, new, 1)
old_img = '''    # 说明书用到的截图（/manual-img/ 提供）
    img_src = os.path.join(ROOT, "使用说明-图片")
    if os.path.isdir(img_src):
        copy_tree(img_src, os.path.join(PAYLOAD, "使用说明-图片"))

'''
assert old_img in m
m = m.replace(old_img, "", 1)
io.open(M, "w", encoding="utf-8", newline="\n").write(m)
print("ok make_installer.py: 只把 PDF 放根目录")

# 版本号
V = os.path.join(ROOT, "version.json")
v = io.open(V, encoding="utf-8").read().replace('"version":  "2.0.30"', '"version":  "2.0.31"')
io.open(V, "w", encoding="utf-8", newline="\n").write(v)
print("ok version.json -> 2.0.31")
