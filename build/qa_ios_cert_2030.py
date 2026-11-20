# -*- coding: utf-8 -*-
"""iOS 白屏根治验收：根证书 + 服务器证书链 + 描述文件 + 白屏兜底。

真实验证：
  · /cert.crt 确实是「根证书」（CA=True），且 /cert.mobileconfig 里嵌的就是它
  · 用它当唯一信任锚，用 **IP** 和 **名字** 做 hostname 校验去连 HTTPS 都能通过
    （这正是 iPhone 装了证书后 Safari 的行为）
  · 服务器证书有效期 ≤825 天、带 serverAuth、SAN 里同时有 DNS 名字和局域网 IP
  · 浏览器：iPhone UA 下 /cert 引导页内容正确；关掉 JS 时主页出现白屏兜底；
    正常时兜底不出现（window.__appReady 置位）

用法：python build\qa_ios_cert_2030.py
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
PROBE = os.path.join(WS, "build", "qa_cert_probe.py")
WORK = os.path.join(WS, "_qa30_ios")
PORT = 8760
SPORT = 9760
BASE = "http://127.0.0.1:%d" % PORT
results = []
procs = {}


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    return bool(cond)


def get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def get_json(url, timeout=20):
    return json.loads(get(url, timeout)[0].decode("utf-8"))


def wait_json(url, timeout=120, pred=None):
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


def start():
    root = os.path.join(WORK, "a")
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(SRC, os.path.join(root, "2.0.0"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(root, "data"))
    with open(os.path.join(root, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "2.0.30", "edition": "beta", "data_dir": "data",
                   "python_dir": "python"}, f)
    with open(os.path.join(root, "data", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"port": PORT, "mdns_enabled": True, "mdns_hostname": "iostest",
                   "require_admin": False, "secure_enabled": True, "secure_port": SPORT,
                   "update_policy": "none"}, f)
    with open(os.path.join(root, "data", "state.json"), "w", encoding="utf-8") as f:
        json.dump({"wizard_done": True, "call_count": 0}, f)
    logf = open(os.path.join(WORK, "a.log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([PY, "-u", os.path.join("2.0.0", "app.py")], cwd=root,
                         stdout=logf, stderr=subprocess.STDOUT, creationflags=0x08000000)
    procs["a"] = (p, logf, root)
    return root


def stop():
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
    root = start()
    st = wait_json(BASE + "/api/status", pred=lambda d: d.get("ok"))
    lan = next((ip for ip in st.get("local_ips") or []
                if not ip.startswith("127.") and not ip.startswith("169.254.")), "")
    check("有局域网 IP", bool(lan), lan)

    # ---------- 1~3. 证书与描述文件（探针：用自带 python 跑 cryptography） ----------
    print("== 1~3. 根证书 / 描述文件 / 证书链 ==")
    pr = subprocess.run([PY, PROBE, BASE, str(SPORT), lan, "iostest", WORK],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=180, creationflags=0x08000000)
    lines = (pr.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError("证书探针无输出: %s" % ((pr.stderr or "")[:400],))
    d = json.loads(lines[-1])
    check("探针执行成功", d.get("ok") is True, d.get("error"))
    check("/cert.crt 是证书类型", "x-x509-ca-cert" in (d.get("cert_ct") or ""), d.get("cert_ct"))
    check("/cert.crt 是下载附件", "attachment" in (d.get("cert_cd") or ""), d.get("cert_cd"))
    check("根证书 CA=True（iPhone 才能开「完全信任」）", d.get("ca_is_ca") is True,
          d.get("ca_err") or d.get("ca_is_ca"))
    check("根证书名字可辨认", "班级叫号器" in (d.get("ca_cn") or ""), d.get("ca_cn"))
    check("描述文件类型是描述文件", "aspen-config" in (d.get("mc_ct") or ""), d.get("mc_ct"))
    check("描述文件里是根证书负载",
          d.get("mc_type") == "com.apple.security.root", d.get("mc_type"))
    check("描述文件里的证书 = /cert.crt", d.get("mc_fp") == d.get("ca_fp"),
          "%s vs %s" % (d.get("mc_fp", "")[:16], d.get("ca_fp", "")[:16]))
    check("描述文件说明里有「完全信任」", "完全信任" in (d.get("mc_desc") or ""))
    check("只信任这张根证书，用 IP 连 HTTPS 通过（=iPhone 装好后的效果）",
          d.get("tls_ip") is True, d.get("tls_ip_err") or "")
    check("只信任这张根证书，用设备名连 HTTPS 通过",
          d.get("tls_name") is True, d.get("tls_name_err") or "")
    leaf = d.get("leaf") or {}
    check("服务器证书有效期 ≤825 天", 0 < int(leaf.get("span") or 9999) <= 825, leaf.get("span"))
    check("服务器证书含 serverAuth", leaf.get("server_auth") is True)
    check("服务器证书由本机根证书签发", leaf.get("issuer_eq_ca") is True)
    check("SAN 含局域网 IP（二维码用 IP 必需）", lan in (leaf.get("ips") or []),
          leaf.get("ips"))
    check("SAN 含设备名 .local", "iostest.local" in [x.lower() for x in (leaf.get("dns") or [])],
          leaf.get("dns"))

    # ---------- 4. 浏览器 ----------
    print("== 4. 浏览器：装证书引导页 + 白屏兜底 ==")
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)

        # 4.1 iPhone UA 打开 /cert
        ctx_i = br.new_context(viewport={"width": 390, "height": 844},
                               user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
                                           "Mobile/15E148 Safari/604.1"))
        pg = ctx_i.new_page()
        pg.goto(BASE + "/cert", wait_until="domcontentloaded")
        pg.wait_for_timeout(900)
        body = pg.evaluate("() => document.body.innerText")
        check("/cert 页给出「证书信任设置」步骤", "证书信任设置" in body)
        check("/cert 页说明第三方浏览器为什么白屏",
              "白屏" in body and ("第三方" in body or "没有" in body))
        check("/cert 页有描述文件下载按钮",
              pg.evaluate("() => !!document.querySelector('a[href=\"/cert.mobileconfig\"]')"))
        check("/cert 页有 .crt 下载按钮",
              pg.evaluate("() => !!document.querySelector('a[href=\"/cert.crt\"]')"))
        check("/cert 页列出本机 IP 的 https 地址", (lan + ":" + str(SPORT)) in body)
        pg.screenshot(path=os.path.join(WORK, "shot-cert-iphone.png"))

        # 4.2 关掉 JS：主页必须出现兜底提示
        ctx_nojs = br.new_context(viewport={"width": 390, "height": 844}, java_script_enabled=False)
        pg2 = ctx_nojs.new_page()
        pg2.goto(BASE + "/", wait_until="domcontentloaded")
        pg2.wait_for_timeout(700)
        txt = pg2.evaluate("() => document.body.innerText") if False else pg2.inner_text("body")
        check("禁用 JS 时主页有兜底说明（不是纯白屏）",
              "JavaScript" in txt and "/plain" in txt, txt[:60].replace("\n", " "))
        pg2.screenshot(path=os.path.join(WORK, "shot-nojs.png"))

        # 4.3 正常 JS：兜底不出现，且 __appReady 已置位
        pg3 = br.new_page(viewport={"width": 1280, "height": 900})
        pg3.goto(BASE + "/", wait_until="domcontentloaded")
        pg3.wait_for_timeout(5000)
        ready = pg3.evaluate("() => !!window.__appReady")
        visible = pg3.is_visible("#boot-fallback")
        check("正常加载时 __appReady 置位", ready is True, ready)
        check("正常加载时不显示白屏兜底", visible is False, visible)

        # 4.4 /plain 里给到 /cert 入口
        pg4 = br.new_page()
        pg4.goto(BASE + "/plain", wait_until="domcontentloaded")
        ptxt = pg4.inner_text("body")
        check("/plain 里有装证书入口", "/cert" in ptxt and "证书" in ptxt, ptxt[:60])
        br.close()


try:
    main()
except Exception as exc:
    import traceback
    traceback.print_exc()
    check("测试过程无异常", False, repr(exc))
finally:
    stop()
    ok = sum(1 for r in results if r[0])
    print("\n===== 结果：%d/%d 通过 =====" % (ok, len(results)))
    for good, name, detail in results:
        if not good:
            print("  FAIL  %s | %s" % (name, detail))
    sys.exit(0 if ok == len(results) else 1)
