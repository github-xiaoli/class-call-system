# -*- coding: utf-8 -*-
"""patch 2.0.30 - 4：启动自检的 mDNS 项改为「注册结果 + 双向解析比对」，并启动监听线程。"""
import io

P = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
src = io.open(P, encoding="utf-8").read()

start = src.index('    if cfg.get("mdns_enabled"):\n        step("mDNS 域名解析")')
end = src.index('    step("数据目录")')
assert start < end

NEW = '''    if cfg.get("mdns_enabled"):
        step("mDNS 域名解析")
        mdns = mdns_status()
        plan = mdns.get("plan") or []
        host_list = "、".join("%s.local → %s" % (p.get("hostname"),
                                              ", ".join(p.get("ips") or [])) for p in plan)
        if not HAS_ZEROCONF:
            checks.append({"level": "warn", "name": "mDNS",
                           "detail": "未安装 zeroconf 组件",
                           "solution": "请使用完整版软件（含 mDNS 组件）。"})
        elif not mdns.get("ok"):
            checks.append({"level": "warn", "name": "mDNS",
                           "detail": "mDNS 未注册成功：%s" % (mdns.get("error") or "未知原因"),
                           "solution": mdns.get("solution") or
                                         "到设置里点「mDNS 诊断」查看网卡情况；"
                                         "手机也可用 http://IP:端口 直连。"})
        else:
            # 再用系统解析器确认一遍（本机解析不到不代表手机解析不到）
            bad = []
            for p in plan:
                h = p.get("hostname")
                ip = _resolve_mdns(h) if h else None
                if not ip or ip not in (p.get("ips") or []):
                    bad.append(h)
            if bad:
                checks.append({
                    "level": "warn", "name": "mDNS",
                    "detail": "已注册 %s，但本机暂时解析不到 %s.local" % (host_list, bad[0]),
                    "solution": "其它设备通常仍可访问；若手机也打不开，"
                                "多半是路由器屏蔽了 mDNS 多播，可让手机直接用 http://IP:端口。",
                })
            else:
                checks.append({"level": "ok", "name": "mDNS",
                               "detail": "已注册 %s（本机解析一致）" % host_list,
                               "solution": ""})

'''

src = src[:start] + NEW + src[end:]

# ---- 启动时把监听线程拉起来 ----
old_reg = '''    if cfg.get("mdns_enabled"):
        status("正在注册 mDNS…")
        register_mdns(cfg)'''
assert old_reg in src
new_reg = '''    if cfg.get("mdns_enabled"):
        status("正在注册 mDNS…")
        register_mdns(cfg)
        start_mdns_watcher()   # 之后网卡一变（换网/插网线/Wi-Fi 连上）就自动重新注册'''
src = src.replace(old_reg, new_reg, 1)

# ---- 退出时停掉监听线程 ----
old_fin = '''    finally:
        remote_shutdown()
        monitor_shutdown()'''
assert old_fin in src
src = src.replace(old_fin, '''    finally:
        _MDNS_STOP.set()
        remote_shutdown()
        monitor_shutdown()''', 1)

io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("ok, startup checks + watcher wired")
