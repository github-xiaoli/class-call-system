# -*- coding: utf-8 -*-
"""2.0.30 mDNS + 日志功能验收（真机真跑：真 zeroconf 观测 + Playwright/Edge 真点击）。

覆盖：
  1. /api/health 新增字段（scheme / host / server_ip / client_ip / ua）
  2. mDNS 注册结果：nine6test.local 的 A 记录**恰好 1 条**且是真网卡 IP
     （老版本会同时广播 Hyper-V 的 172.25.96.1，手机就时好时坏）
  3. /api/mdns/diag：虚拟网卡被跳过并给出原因
  4. 两台电脑同名时自动改名（allow_name_change），不再整机注册失败
  5. 日志：/api/logs 实时读取、/api/logs/export 导出、/logs 页面真点击 + 实时刷新
  6. mDNS 失败提示：状态接口 + 设置页红字提示
  7. edition=stable 时主页隐藏「日志」入口，但直接输 /logs 仍能进
  8. /plain、/ping 极简页

用法：python build/qa_mdns_2030.py
"""
import json
import os
import shutil
import socket
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
PROBE = os.path.join(WS, "build", "qa_mdns_probe.py")
WORK = os.path.join(WS, "_qa30")
HOST = "qa30test"
PORT_A = 8720
PORT_B = 8721
BASE_A = "http://127.0.0.1:%d" % PORT_A
BASE_B = "http://127.0.0.1:%d" % PORT_B

results = []
procs = {}


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    return bool(cond)


def get_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_text(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace"), dict(r.headers)


def post_json(url, payload=None, timeout=60):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_json(url, timeout=90, pred=None):
    """轮询直到接口可用（启动自检里有 mDNS 解析，可能要几秒）。"""
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
        time.sleep(0.5)
    raise RuntimeError("等待超时 %s：%r" % (url, last))


def start(name, port, edition="beta", hostname=HOST):
    root = os.path.join(WORK, name)
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(SRC, os.path.join(root, "2.0.0"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(root, "data"))
    with open(os.path.join(root, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"app_name": "班级叫号器", "version": "2.0.30", "edition": edition,
                   "entry": "app.py", "data_dir": "data", "python_dir": "python"}, f)
    with open(os.path.join(root, "data", "config.json"), "w", encoding="utf-8") as f:
        json.dump({"port": port, "mdns_enabled": True, "mdns_hostname": hostname,
                   "require_admin": False, "secure_enabled": False,
                   "update_policy": "none", "monitor_quality": "standard"}, f)
    # 向导闸门：测试实例直接标记为「已完成首次设置」，否则接口一律 428
    with open(os.path.join(root, "data", "state.json"), "w", encoding="utf-8") as f:
        json.dump({"wizard_done": True, "call_count": 0}, f)
    logf = open(os.path.join(WORK, name + ".log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([PY, "-u", os.path.join("2.0.0", "app.py")], cwd=root,
                         stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=0x08000000)
    procs[name] = (p, root, logf)
    return root


def stop_all():
    for name, (p, _root, logf) in list(procs.items()):
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


def read_log(name):
    p = os.path.join(WORK, name + ".log")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def probe(action="collect", wait=4.0, host=""):
    """用软件自带的 zeroconf（3.13）当"手机"观测局域网 mDNS。"""
    out = subprocess.run([PY, PROBE, action, str(wait), host], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120,
                         creationflags=0x08000000)
    txt = (out.stdout or "").strip().splitlines()
    if not txt:
        raise RuntimeError("probe 无输出: %s" % (out.stderr or "")[:300])
    return json.loads(txt[-1])


def collector_browse(seconds=4.0):
    """局域网里现在的服务实例名集合。"""
    return set(probe("collect", seconds).get("names") or [])


def resolve_name(host, seconds=4.0):
    """通过真 mDNS 解析主机名的 A 记录（用 zeroconf 读 SRV/A，避开系统缓存）。"""
    d = probe("resolve", seconds, host)
    return list(d.get("target") or [])


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    net_ip = None
    print("== 启动实例 A（%d，主机名 %s）==" % (PORT_A, HOST))
    start("a", PORT_A, edition="beta")
    wait_json(BASE_A + "/api/status", timeout=120, pred=lambda d: d.get("ok"))
    # mDNS 注册是在 HTTP 服务起来之后才做的，等它落定再断言
    wait_json(BASE_A + "/api/mdns/status", timeout=90,
              pred=lambda d: d.get("registered") is True or bool(d.get("error")))
    st = get_json(BASE_A + "/api/status")

    # ---------- 1. /api/health ----------
    print("== 1. /api/health 排障字段 ==")
    h = get_json(BASE_A + "/api/health")
    check("health: ok=True", h.get("ok") is True)
    check("health: 有 scheme 字段", h.get("scheme") in ("http", "https"), h.get("scheme"))
    check("health: 有 server_ip", bool(h.get("server_ip")), h.get("server_ip"))
    check("health: 有 client_ip", bool(h.get("client_ip")), h.get("client_ip"))
    check("health: 有 ua", "python" in (h.get("ua") or "").lower() or bool(h.get("ua")),
          (h.get("ua") or "")[:40])

    # ---------- 2. mDNS 注册结果 ----------
    print("== 2. mDNS 注册结果（关键：A 记录只能 1 条）==")
    ms = get_json(BASE_A + "/api/mdns/status")
    check("mdns 注册成功", ms.get("registered") is True, ms.get("error"))
    plan = ms.get("plan") or []
    check("广播计划恰好 1 块网卡", len(plan) == 1, plan)
    if plan:
        net_ip = (plan[0].get("ips") or [None])[0]
        check("域名 = %s" % HOST, plan[0].get("hostname") == HOST, plan[0].get("hostname"))
        check("广播 IP 不是虚拟网卡", net_ip and not net_ip.startswith("172.25."), net_ip)
    check("status 里 mdns_ok=True", st.get("mdns_ok") is True, st.get("mdns_error"))
    check("status 里 mdns_urls 正确", (st.get("mdns_urls") or [""])[0] ==
          "http://%s.local:%d" % (HOST, PORT_A), st.get("mdns_urls"))

    addrs = resolve_name(HOST)
    check("mDNS 解析到的 A 记录恰好 1 条", len(addrs) == 1, addrs)
    if addrs and net_ip:
        check("A 记录就是真网卡 IP（无 Hyper-V 地址）", addrs == [net_ip], addrs)

    # ---------- 3. 诊断接口 ----------
    print("== 3. /api/mdns/diag 网卡判定 ==")
    dg = get_json(BASE_A + "/api/mdns/diag")
    ads = dg.get("adapters") or []
    usable = [a for a in ads if a.get("usable")]
    virt = [a for a in ads if a.get("virtual")]
    check("诊断：至少 1 块可用网卡", len(usable) >= 1, [a.get("alias") for a in usable])
    check("诊断：虚拟网卡被识别", len(virt) >= 1, [a.get("alias") for a in virt])
    check("诊断：虚拟网卡有跳过原因", all(a.get("reason") for a in virt),
          [a.get("reason") for a in virt][:2])
    check("诊断：可用网卡都不是虚拟的", all(not a.get("virtual") for a in usable),
          [a.get("alias") for a in usable])
    check("诊断：hints 是列表", isinstance(dg.get("hints"), list))

    # ---------- 4. 两台同名电脑 ----------
    print("== 4. 第二台同名电脑：自动改名而不是注册失败 ==")
    start("b", PORT_B, edition="stable")
    wait_json(BASE_B + "/api/status", timeout=120, pred=lambda d: d.get("ok"))
    wait_json(BASE_B + "/api/mdns/status", timeout=90,
              pred=lambda d: d.get("registered") is True or bool(d.get("error")))
    msb = get_json(BASE_B + "/api/mdns/status")
    check("第二台也注册成功了（没被同名卡死）", msb.get("registered") is True,
          msb.get("error"))
    names = collector_browse(4.5)
    same = sorted(n for n in names if "班级叫号器" in n)
    check("局域网里出现 2 个服务实例", len(same) >= 2, same)
    check("第二台被自动改名（带 -2）", any("-2" in n for n in same), same)

    # ---------- 5. 日志 ----------
    print("== 5. 日志：接口 + 导出 ==")
    lg = get_json(BASE_A + "/api/logs?after=-1&limit=200")
    lines = lg.get("lines") or []
    check("日志有内容", len(lines) > 0, len(lines))
    check("日志带序号/时间/级别", all({"seq", "t", "level", "text"} <= set(x) for x in lines[:5]))
    check("日志里有 mDNS 注册记录",
          any("mDNS" in x["text"] for x in lines), [x["text"][:40] for x in lines[:3]])
    check("日志包含手机访问地址行",
          any("手机访问地址" in x["text"] for x in lines))
    nxt = lg.get("next")
    lg2 = get_json(BASE_A + "/api/logs?after=%d&limit=50" % nxt)
    check("增量拉取不重复（after 生效）", all(x["seq"] > nxt for x in lg2.get("lines") or []))
    txt, hdr = get_text(BASE_A + "/api/logs/export")
    check("导出：附件下载头", "attachment" in (hdr.get("Content-Disposition") or ""),
          hdr.get("Content-Disposition"))
    check("导出：内容含标题", "班级叫号器 运行日志" in txt, txt[:40])
    check("导出：含本次运行日志", "手机访问地址" in txt)
    check("日志文件已落盘", os.path.isfile(os.path.join(WORK, "a", "data", "logs",
                                                        lg.get("file", "").split("\\")[-1])),
          lg.get("file"))

    # ---------- 6. 极简页 ----------
    print("== 6. /plain 与 /ping ==")
    t1, _ = get_text(BASE_A + "/plain")
    check("/plain 能打开并说明已连上", "已经连上这台电脑" in t1)
    t2, _ = get_text(BASE_A + "/ping")
    check("/ping 返回 OK", t2.strip().startswith("OK"), t2.strip()[:30])

    # ---------- 7. 浏览器验收（真点击） ----------
    print("== 7. 浏览器：设置里的 mDNS 状态/诊断 + 日志页 ==")
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport={"width": 1280, "height": 900})

        # 7.1 beta 版：主页有「日志」按钮（不隐藏）
        pg.goto(BASE_A + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)
        btn_visible = pg.evaluate(
            "() => { const b = document.getElementById('btn-logs');"
            " return !!b && !b.classList.contains('hidden'); }")
        check("beta 版主页显示「日志」按钮", btn_visible is True)

        # 7.2 设置里显示 mDNS 已生效
        pg.click("#btn-settings")
        pg.wait_for_timeout(900)
        mstate = pg.evaluate("() => document.getElementById('mdns-state').textContent")
        mclass = pg.evaluate("() => document.getElementById('mdns-state').className")
        check("设置里显示 mDNS 已生效", "已生效" in mstate and HOST in mstate, mstate[:70])
        check("状态行用绿色（ok）类", "ok" in mclass, mclass)

        # 7.3 点「mDNS 诊断」真出结果
        pg.click("#btn-mdns-diag")
        pg.wait_for_timeout(2500)
        diag_txt = pg.evaluate("() => document.getElementById('mdns-diag-out').textContent")
        check("诊断面板可见", pg.is_visible("#mdns-diag-out"))
        check("诊断内容含网卡判定", "网卡判定" in diag_txt, diag_txt[:60])
        check("诊断内容列出本机 IP", (net_ip or "xxx") in diag_txt)
        check("诊断内容含广播计划", "广播计划" in diag_txt)
        pg.screenshot(path=os.path.join(WORK, "shot-settings-mdns.png"))

        # 7.4 失败提示（把 state 改成失败，验证红字渲染）
        pg.evaluate("() => { state.mdns = { enabled: true, ok: false,"
                    " error: '测试：没有可用网卡' , urls: [], hostname: 'x' };"
                    " renderMdnsState(); }")
        pg.wait_for_timeout(300)
        bad_txt = pg.evaluate("() => document.getElementById('mdns-state').textContent")
        bad_cls = pg.evaluate("() => document.getElementById('mdns-state').className")
        check("失败时显示红字警告", "注册失败" in bad_txt and "测试：没有可用网卡" in bad_txt,
              bad_txt[:70])
        check("失败时状态行用 bad 类", "bad" in bad_cls, bad_cls)
        pg.click("#modal-settings .modal-head .close")
        pg.wait_for_timeout(300)

        # 7.5 日志页：真点击进入 + 实时刷新
        pg.click("#btn-logs")
        pg.wait_for_timeout(2500)
        check("点「日志」进入了 /logs", pg.url.endswith("/logs"), pg.url)
        check("日志页标题正确", "运行日志" in pg.title(), pg.title())
        n1 = pg.evaluate("() => document.querySelectorAll('#view .ln').length")
        check("日志页显示出日志行", n1 > 0, n1)
        # 触发一条新日志（重新注册 mDNS），看页面 2 秒内是否自动出现
        post_json(BASE_A + "/api/mdns/refresh")
        pg.wait_for_timeout(2200)
        n2 = pg.evaluate("() => document.querySelectorAll('#view .ln').length")
        check("新日志自动刷出来（实时）", n2 > n1, "%s -> %s" % (n1, n2))
        txt_page = pg.evaluate("() => document.getElementById('view').textContent")
        check("页面里有 mDNS 注册日志", "mDNS" in txt_page)
        pg.screenshot(path=os.path.join(WORK, "shot-logs.png"))

        # 7.6 导出按钮真点击 → 触发下载
        with pg.expect_download(timeout=15000) as dl:
            pg.click("#btn-export")
        d = dl.value
        check("导出按钮真的下载了文件", "日志" in d.suggested_filename, d.suggested_filename)

        # 7.7 过滤：输入关键字后只剩匹配行
        pg.fill("#search", "mDNS")
        pg.wait_for_timeout(1500)
        txt_filtered = pg.evaluate("() => document.getElementById('view').textContent")
        check("关键字过滤生效（只剩 mDNS 行）",
              "mDNS" in txt_filtered and "端口" not in txt_filtered.split("\n")[0][:6],
              txt_filtered[:60])

        # 7.8 stable 版：主页隐藏入口，但 /logs 直连仍可进
        pg.goto(BASE_B + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)
        hidden = pg.evaluate(
            "() => { const b = document.getElementById('btn-logs');"
            " return !b || b.classList.contains('hidden'); }")
        check("stable 版主页隐藏「日志」按钮", hidden is True)
        pg.goto(BASE_B + "/logs", wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)
        ok_logs = pg.evaluate("() => document.querySelectorAll('#view .ln').length")
        check("stable 版直接输 /logs 仍能进且出日志", ok_logs > 0, ok_logs)
        pg.screenshot(path=os.path.join(WORK, "shot-logs-stable.png"))
        br.close()

    # ---------- 8. 日志里没有异常 ----------
    print("== 8. 启动日志检查 ==")
    alog = read_log("a")
    check("日志里没有 Traceback", "Traceback" not in alog)
    check("日志里有网卡枚举结论", "[mDNS]" in alog)
    bad_words = [w for w in ("注册失败", "非唯一", "NonUniqueName") if w in alog]
    check("日志里没有注册失败记录", not bad_words, bad_words)


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
