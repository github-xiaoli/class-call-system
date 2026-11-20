# -*- coding: utf-8 -*-
"""验收：软件内置图文说明书 /manual（含兜底页）。

真浏览器验证：
  · /manual 能打开、含新增章节、图片真的显示出来（naturalWidth>0）
  · /manual-img/<图片> 返回图片；/manual.pdf 返回真 PDF；/manual.txt 返回纯文本
  · 安装里没有说明书文件时（旧版升级/OTA）→ 兜底速查页，不报错

用法：python build\qa_manual_page.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

WS = r"C:\Users\李语晨\Desktop\班级叫号器"
SRC = os.path.join(WS, "2.0.0")
PY = os.path.join(WS, "build", "zip", "班级叫号器", "python", "python.exe")
WORK = os.path.join(WS, "_qa30_manual")
PA, PB, PC = 8780, 8781, 8782
BASE_A = "http://127.0.0.1:%d" % PA
BASE_B = "http://127.0.0.1:%d" % PB
BASE_C = "http://127.0.0.1:%d" % PC
results = []
procs = {}


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    return bool(cond)


def get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), dict(r.headers), r.geturl()


def wait_ok(url, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            get(url)
            return True
        except Exception:
            time.sleep(0.4)
    return False


def start(name, port, with_manual=True):
    root = os.path.join(WORK, name)
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(SRC, os.path.join(root, "2.0.0"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(root, "data"))
    with open(os.path.join(root, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "2.0.30", "edition": "beta", "data_dir": "data",
                   "python_dir": "python"}, f)
    with open(os.path.join(root, "data", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"port": port, "mdns_enabled": False, "require_admin": False,
                   "secure_enabled": False, "update_policy": "none"}, f)
    with open(os.path.join(root, "data", "state.json"), "w", encoding="utf-8") as f:
        json.dump({"wizard_done": True, "call_count": 0}, f)
    if with_manual:
        for fn in ("使用说明-图文版.html", "使用说明-图文版.pdf", "使用说明.txt"):
            src = os.path.join(WS, fn)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(root, fn))
        img = os.path.join(WS, "使用说明-图片")
        if os.path.isdir(img):
            shutil.copytree(img, os.path.join(root, "使用说明-图片"))
    logf = open(os.path.join(WORK, name + ".log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([PY, "-u", os.path.join("2.0.0", "app.py")], cwd=root,
                         stdout=logf, stderr=subprocess.STDOUT, creationflags=0x08000000)
    procs[name] = (p, logf, root)


def stop_all():
    for _n, (p, logf, _r) in list(procs.items()):
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            logf.close()
        except Exception:
            pass
    procs.clear()


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    print("== 实例 A：安装目录里有说明书文件 ==")
    start("a", PA, with_manual=True)
    check("实例 A 启动成功", wait_ok(BASE_A + "/api/status"))

    body, hdr, _ = get(BASE_A + "/manual")
    html = body.decode("utf-8", "replace")
    check("/manual 返回 HTML", "text/html" in (hdr.get("Content-Type") or ""), hdr.get("Content-Type"))
    check("/manual 含新增章节「十四、连不上的原因」", "十四、连不上的原因" in html)
    check("/manual 含 14.3 装证书步骤", "证书信任设置" in html)
    check("图片路径已改写成 /manual-img/", '/manual-img/' in html and 'src="使用说明-图片/' not in html)

    # 具体一张图能取到
    import re
    m = re.search(r'src="/manual-img/([^"]+)"', html)
    check("说明书里能找到图片引用", bool(m), m.group(1) if m else "")
    if m:
        img_url = BASE_A + "/manual-img/" + urllib.parse.quote(m.group(1))
        try:
            raw, ihdr, _ = get(img_url)
            check("/manual-img 返回图片",
                  (ihdr.get("Content-Type") or "").startswith("image/") and len(raw) > 1000,
                  "%s %d bytes" % (ihdr.get("Content-Type"), len(raw)))
        except Exception as exc:
            check("/manual-img 返回图片", False, repr(exc))

    pdf, phdr, _ = get(BASE_A + "/manual.pdf")
    check("/manual.pdf 返回真 PDF",
          pdf[:5] == b"%PDF-" and "pdf" in (phdr.get("Content-Type") or ""),
          "%s %d bytes" % (phdr.get("Content-Type"), len(pdf)))

    txt, thdr, _ = get(BASE_A + "/manual.txt")
    check("/manual.txt 可访问", "班级叫号器" in txt.decode("utf-8", "replace"))

    print("== 真浏览器打开 /manual ==")
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(BASE_A + "/manual", wait_until="load")
        pg.wait_for_timeout(1500)
        check("浏览器里标题正确", "图文使用说明" in pg.title(), pg.title())
        loaded = pg.evaluate(
            "() => Array.from(document.images).filter(i => i.complete && i.naturalWidth > 0).length")
        total = pg.evaluate("() => document.images.length")
        check("说明书图片真的显示出来", total > 5 and loaded >= total - 1, "%d/%d" % (loaded, total))
        has14 = pg.evaluate("() => document.body.innerText.includes('十四、连不上的原因')")
        check("页面上能看到第十四章", bool(has14))
        pg.screenshot(path=os.path.join(WORK, "shot-manual.png"), full_page=False)
        br.close()

    print("== 实例 C：说明书只在版本目录（2.0.31 起 OTA 下发的布局）==")
    start("c", PC, with_manual=False)
    check("实例 C 启动成功", wait_ok(BASE_C + "/api/status"))
    body3, hdr3, _ = get(BASE_C + "/manual")
    t3 = body3.decode("utf-8", "replace")
    check("只在版本目录时 /manual 仍是完整图文（不是兜底页）",
          "十四、连不上的原因" in t3 and "没有放图文说明书文件" not in t3)
    check("图片路径可用 /manual-img/", "/manual-img/" in t3)
    body3 = None
    m3 = None
    import re as _re
    m3 = _re.search(r'src="/manual-img/([^"]+)"', t3)
    if m3:
        try:
            raw3, ih3, _ = get(BASE_C + "/manual-img/" + urllib.parse.quote(m3.group(1)))
            check("版本目录里的图片能取到",
                  (ih3.get("Content-Type") or "").startswith("image/") and len(raw3) > 1000,
                  "%s %d bytes" % (ih3.get("Content-Type"), len(raw3)))
        except Exception as exc:
            check("版本目录里的图片能取到", False, repr(exc))

    print("== 实例 B：安装目录里没有说明书文件（旧版升级 / OTA 场景）==")
    start("b", PB, with_manual=False)
    check("实例 B 启动成功", wait_ok(BASE_B + "/api/status"))
    body2, hdr2, _ = get(BASE_B + "/manual")
    t2 = body2.decode("utf-8", "replace")
    check("没有说明书时 /manual 仍返回页面（不报错）", "text/html" in (hdr2.get("Content-Type") or ""))
    check("兜底页给出速查内容", "使用说明" in t2 and ("/plain" in t2) and ("/cert" in t2))
    check("兜底页说明了原因", "没有放图文说明书文件" in t2 or "速查" in t2)
    try:
        pdf2, phdr2, final2 = get(BASE_B + "/manual.pdf")
        check("/manual.pdf 缺文件时回落到 /manual",
              final2.endswith("/manual") or "text/html" in (phdr2.get("Content-Type") or ""),
              "%s -> %s" % (phdr2.get("Content-Type"), final2))
    except Exception as exc:
        check("/manual.pdf 缺文件时回落到 /manual", False, repr(exc))


try:
    main()
except Exception as exc:
    import traceback
    traceback.print_exc()
    check("测试过程无异常", False, repr(exc))
finally:
    stop_all()
    ok = sum(1 for r in results if r[0])
    print("\n===== 结果：%d/%d 通过 =====" % (ok, len(results)))
    for good, name, detail in results:
        if not good:
            print("  FAIL  %s | %s" % (name, detail))
    sys.exit(0 if ok == len(results) else 1)
