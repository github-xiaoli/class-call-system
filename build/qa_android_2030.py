# -*- coding: utf-8 -*-
"""安卓/鸿蒙可用性验收：二维码与接入地址必须用 IP（真浏览器点击验证）。

背景：安卓 10 / 鸿蒙 3.0 的手机浏览器走系统解析器，安卓系统不支持 mDNS，
http://xxx.local 必然解析失败（IP 可访问）。所以二维码必须放 IP。

用法：python build\qa_android_2030.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

WS = r"C:\Users\李语晨\Desktop\班级叫号器"
SRC = os.path.join(WS, "2.0.0")
PY = os.path.join(WS, "build", "zip", "班级叫号器", "python", "python.exe")
WORK = os.path.join(WS, "_qa30_android")
results = []
procs = {}


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    return bool(cond)


def get_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_json(url, timeout=90, pred=None):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            d = get_json(url)
            last = d
            if pred is None or pred(d):
                return d
        except Exception as exc:
            last = exc
        time.sleep(0.4)
    raise RuntimeError("等待超时 %s: %r" % (url, last))


def start(name, port, wizard_done, step=0):
    root = os.path.join(WORK, name)
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(SRC, os.path.join(root, "2.0.0"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(root, "data"))
    with open(os.path.join(root, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "2.0.30", "edition": "beta", "data_dir": "data",
                   "python_dir": "python"}, f)
    with open(os.path.join(root, "data", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"port": port, "mdns_enabled": True, "mdns_hostname": "andtest",
                   "require_admin": False, "secure_enabled": True, "secure_port": 9750 + port % 100,
                   "update_policy": "none"}, f)
    with open(os.path.join(root, "data", "state.json"), "w", encoding="utf-8") as f:
        json.dump({"wizard_done": wizard_done, "wizard_step": step,
                   "license_agreed": True, "call_count": 0}, f)
    logf = open(os.path.join(WORK, name + ".log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([PY, "-u", os.path.join("2.0.0", "app.py")], cwd=root,
                         stdout=logf, stderr=subprocess.STDOUT, creationflags=0x08000000)
    procs[name] = (p, logf)
    return root


def stop_all():
    for name, (p, logf) in list(procs.items()):
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

    print("== 实例 A：已完成向导（验证密码页二维码）==")
    start("a", 8740, True)
    wait_json("http://127.0.0.1:8740/api/status", pred=lambda d: d.get("ok"))
    st = get_json("http://127.0.0.1:8740/api/status")
    lan = next((ip for ip in st.get("local_ips") or []
                if not ip.startswith("127.") and not ip.startswith("169.254.")), "")
    check("本机有局域网 IP", bool(lan), lan)

    print("== 实例 B：向导停在第 5 步（验证向导二维码）==")
    start("b", 8741, False, step=5)

    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)

        # ---------- 密码页二维码 ----------
        pg = br.new_page(viewport={"width": 1200, "height": 900})
        pg.goto("http://127.0.0.1:8740/password", wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)
        pair_url_text = pg.evaluate("() => (document.getElementById('pair-url')||{}).textContent || ''")
        check("密码页提示的地址用 IP", lan in pair_url_text, pair_url_text[:110])
        check("密码页同时给出 .local 备用地址", ".local" in pair_url_text, pair_url_text[:110])

        pg.click("#btn-qr-create")
        pg.wait_for_timeout(2500)
        content = pg.evaluate("() => { const c = document.getElementById('qr-canvas');"
                              " return (c && c.dataset.content) || ''; }")
        check("二维码内容指向 IP（不是 .local）",
              ("//" + lan) in content and ".local" not in content, content[:110])
        check("二维码走 https 安全端口", content.startswith("https://"), content[:40])
        check("二维码带配对令牌", "?t=" in content)
        hint_text = pg.evaluate("() => document.body.innerText")
        check("页面上说明了安卓不支持 .local",
              "安卓" in hint_text and ".local" in hint_text and "IP" in hint_text)
        pg.screenshot(path=os.path.join(WORK, "shot-password-ip.png"))

        # ---------- /pair 页（手机扫码后落地的页面）----------
        url = content
        ctx2 = br.new_context(viewport={"width": 390, "height": 780},
                              ignore_https_errors=True)   # 自签名证书：真机上点「继续访问」
        pg2 = ctx2.new_page()
        try:
            pg2.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            check("/pair 页能打开", False, repr(exc)[:120])
        else:
            pg2.wait_for_timeout(1200)
            body = pg2.evaluate("() => document.body.innerText")
            check("/pair 页能打开并显示配对信息",
                  ("配对" in body or "激活" in body), body[:80].replace("\n", " "))

        # ---------- 向导第 5 步二维码 ----------
        pg3 = br.new_page(viewport={"width": 1280, "height": 800})
        pg3.goto("http://127.0.0.1:8741/wizard", wait_until="domcontentloaded")
        pg3.wait_for_timeout(3500)
        qr_url = pg3.evaluate("() => (document.getElementById('qr-url')||{}).textContent || ''")
        qr_content = pg3.evaluate("() => { const c = document.getElementById('qr');"
                                 " return (c && c.dataset.content) || ''; }")
        step_now = pg3.evaluate("() => document.querySelector('.step.active') ?"
                               " document.querySelector('.step.active').dataset.step : ''")
        check("向导恢复到第 5 步", str(step_now) == "5", step_now)
        check("向导二维码内容用 IP（不是 .local）",
              ("//" + lan) in qr_content and ".local" not in qr_content, qr_content[:120])
        check("向导文字里也显示 IP 地址", ("//" + lan) in qr_url, qr_url[:120])
        check("向导里也给出 .local 备用", "也可用" in qr_url and ".local" in qr_url, qr_url[:120])
        pg3.screenshot(path=os.path.join(WORK, "shot-wizard-ip.png"))

        # ---------- 设置页说明 ----------
        pg4 = br.new_page(viewport={"width": 1280, "height": 900})
        pg4.goto("http://127.0.0.1:8740/", wait_until="domcontentloaded")
        pg4.wait_for_timeout(1200)
        pg4.click("#btn-settings")
        pg4.wait_for_timeout(800)
        set_text = pg4.evaluate("() => (document.getElementById('modal-settings')||{}).innerText || ''")
        check("设置里说明「安卓/鸿蒙不解析 .local」",
              "安卓" in set_text and ".local" in set_text)
        br.close()


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
