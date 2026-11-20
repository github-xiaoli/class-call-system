# -*- coding: utf-8 -*-
"""patch 2.0.30 - 5：修正自检误报 + /api/mdns/status 不再吞掉注册结果。"""
import io

P = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
src = io.open(P, encoding="utf-8").read()

# ---- 1. 自检：以"注册成功"为准，本机解析只作参考（Windows 解析器有缓存，刚注册常查不到） ----
start = src.index('        else:\n            # 再用系统解析器确认一遍')
end = src.index('    step("数据目录")')
NEW = '''        else:
            # 本机解析只作参考：刚注册完 Windows 解析器常有缓存/延迟，查不到不代表手机打不开
            first = plan[0] if plan else {}
            ip = None
            for _ in range(3):
                ip = _resolve_mdns(first.get("hostname") or "") if first else None
                if ip:
                    break
                time.sleep(0.8)
            level = "ok"
            detail = "已注册 %s" % host_list
            solution = ""
            if ip and ip in (first.get("ips") or []):
                detail += "（本机解析一致：%s）" % ip
            elif ip:
                level = "warn"
                detail += "（本机解析到 %s，与预期 %s 不一致）" % (
                    ip, ", ".join(first.get("ips") or []) or "无")
                solution = "到设置里点「mDNS 诊断」查看网卡；也可让手机直接用 http://IP:端口。"
            else:
                detail += "（本机暂未解析到 .local 名字，其它设备通常仍可访问）"
                solution = "若手机也打不开，多半是路由器屏蔽了 mDNS 多播，可让手机用 http://IP:端口 直连。"
            checks.append({"level": level, "name": "mDNS", "detail": detail,
                           "solution": solution})

'''
src = src[:start] + NEW + src[end:]

# ---- 2. /api/mdns/status 与 /api/mdns/refresh：注册结果放 registered，ok 只表示接口成功 ----
old = '''@app.route("/api/mdns/status")
def api_mdns_status():
    return jsonify(dict(mdns_status(), ok=True))'''
new = '''@app.route("/api/mdns/status")
def api_mdns_status():
    st = mdns_status()
    st["registered"] = st.pop("ok")
    st["ok"] = True          # 接口本身成功；是否注册成功看 registered
    return jsonify(st)'''
assert old in src
src = src.replace(old, new, 1)

old2 = '''    ok = register_mdns(get_config(), force=True)
    st = mdns_status()
    st["ok"] = bool(ok)
    return jsonify(st)'''
new2 = '''    ok = register_mdns(get_config(), force=True)
    st = mdns_status()
    st["registered"] = bool(ok)
    st["ok"] = True
    return jsonify(st)'''
assert old2 in src
src = src.replace(old2, new2, 1)

# ---- 3. 诊断接口里的 status 也统一（mdns_status 原样返回，使用方按 ok 判断注册结果） ----
io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("ok app.py patched")

# ---- 4. 前端：refesh 后读 registered ----
J = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\static\app.js"
js = io.open(J, encoding="utf-8").read()
oldjs = '''    const d = await authedApi("/api/mdns/refresh", { method: "POST", body: "{}" });
    state.mdns = { enabled: !!d.enabled, ok: !!d.ok, error: d.error || "",
                   urls: d.urls || [], hostname: d.hostname || "" };
    renderMdnsState();
    await loadStatus();
    toast(d.ok ? "mDNS 已重新注册：" + (d.urls || []).join("  ")
               : "重新注册失败：" + (d.error || "原因见日志"));'''
newjs = '''    const d = await authedApi("/api/mdns/refresh", { method: "POST", body: "{}" });
    const registered = (d.registered !== undefined) ? !!d.registered : !!d.ok;
    state.mdns = { enabled: !!d.enabled, ok: registered, error: d.error || "",
                   urls: d.urls || [], hostname: d.hostname || "" };
    renderMdnsState();
    await loadStatus();
    toast(registered ? "mDNS 已重新注册：" + (d.urls || []).join("  ")
                     : "重新注册失败：" + (d.error || "原因见日志"));'''
assert oldjs in js
js = js.replace(oldjs, newjs, 1)
io.open(J, "w", encoding="utf-8", newline="\n").write(js)
print("ok app.js patched")

# ---- 5. 探针：zeroconf 0.154 的 ServiceBrowser 第三个参数直接接收 listener 对象 ----
Q = r"C:\Users\李语晨\Desktop\班级叫号器\build\qa_mdns_probe.py"
qs = io.open(Q, encoding="utf-8").read()
qs = qs.replace('ServiceBrowser(zc, "_http._tcp.local.", [_C()])',
                'ServiceBrowser(zc, "_http._tcp.local.", _C())')
qs = qs.replace('ServiceBrowser(zc, "_https._tcp.local.", [_C()])',
                'ServiceBrowser(zc, "_https._tcp.local.", _C())')
io.open(Q, "w", encoding="utf-8", newline="\n").write(qs)
print("ok probe patched")

# ---- 6. 测试脚本：等注册完成再断言；/api/mdns/status 用 registered ----
T = r"C:\Users\李语晨\Desktop\班级叫号器\build\qa_mdns_2030.py"
ts = io.open(T, encoding="utf-8").read()
ts = ts.replace(
    '    st = wait_json(BASE_A + "/api/status", timeout=120, pred=lambda d: d.get("ok"))',
    '''    wait_json(BASE_A + "/api/status", timeout=120, pred=lambda d: d.get("ok"))
    # mDNS 注册是在 HTTP 服务起来之后才做的，等它落定再断言
    wait_json(BASE_A + "/api/mdns/status", timeout=90,
              pred=lambda d: d.get("registered") is True or bool(d.get("error")))
    st = get_json(BASE_A + "/api/status")''')
ts = ts.replace('    check("mdns status ok", ms.get("ok") is True, ms.get("error"))',
                '    check("mdns 注册成功", ms.get("registered") is True, ms.get("error"))')
ts = ts.replace('''    msb = get_json(BASE_B + "/api/mdns/status")
    check("第二台也注册成功了（没被同名卡死）", msb.get("ok") is True, msb.get("error"))''',
                '''    wait_json(BASE_B + "/api/mdns/status", timeout=90,
              pred=lambda d: d.get("registered") is True or bool(d.get("error")))
    msb = get_json(BASE_B + "/api/mdns/status")
    check("第二台也注册成功了（没被同名卡死）", msb.get("registered") is True,
          msb.get("error"))''')
io.open(T, "w", encoding="utf-8", newline="\n").write(ts)
print("ok test patched")
