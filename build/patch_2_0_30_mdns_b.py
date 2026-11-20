# -*- coding: utf-8 -*-
"""patch 2.0.30 - 2：重写 register_mdns（一域名一地址）、加 mDNS 状态/诊断/监听线程。"""
import io

P = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
src = io.open(P, encoding="utf-8").read()

start = src.index("def register_mdns(cfg):")
end = src.index("def _excluded_tcp_ranges():")
assert start < end

NEW = '''_MDNS_STATE = {
    "ok": False,        # 最近一次注册是否成功
    "error": "",        # 失败原因（网页直接显示给老师看）
    "solution": "",     # 对应的解决办法
    "at": 0.0,          # 最近一次尝试的时间
    "plan": [],         # [{hostname, ips, iface, primary}]
    "names": [],        # 实际注册上去的服务名（可能因撞名被自动加了后缀）
}
_MDNS_STOP = threading.Event()
_MDNS_WATCHER = None
_MDNS_WATCH_INTERVAL = 8.0     # 每 8 秒看一眼网卡有没有变化（换网/插拔/Wi-Fi 连上）


def mdns_status():
    """mDNS 状态汇总（设置界面、启动自检、诊断接口共用，不做耗时解析）。"""
    cfg = get_config()
    port = int(_running_port or cfg.get("port", 8000))
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    plan = list(_MDNS_STATE.get("plan") or [])
    st = {
        "ok": bool(_MDNS_STATE.get("ok")),
        "enabled": bool(cfg.get("mdns_enabled")),
        "error": _MDNS_STATE.get("error") or "",
        "solution": _MDNS_STATE.get("solution") or "",
        "at": _MDNS_STATE.get("at") or 0,
        "at_text": (time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(_MDNS_STATE["at"]))
                    if _MDNS_STATE.get("at") else ""),
        "zeroconf_ok": HAS_ZEROCONF,
        "hostname": hostname,
        "port": port,
        "plan": plan,
        "names": list(_MDNS_STATE.get("names") or []),
        "urls": ["http://%s:%d" % (p.get("hostname") or hostname, port) for p in plan],
        "lan_ips": get_lan_ips(),
    }
    return st


def mdns_diagnose():
    """完整诊断：网卡判定（含每块网卡被跳过的原因）+ 注册计划 + 本机解析结果。"""
    cfg = get_config()
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    adapters = []
    try:
        adapters = netif.enum_adapters(force=True)
    except Exception as exc:
        print("[mDNS] 诊断时枚举网卡失败:", exc)
    plan = get_mdns_plan(cfg)
    resolved = []
    for p in plan:
        ip = _resolve_mdns(p["hostname"])
        resolved.append({"host": "%s.local" % p["hostname"], "ip": ip or "",
                         "ok": bool(ip and ip in p["ips"])})
    hints = []
    if not HAS_ZEROCONF:
        hints.append("缺少 zeroconf 组件，mDNS 不可用")
    if not plan:
        hints.append("没有可广播的网卡：请确认网线/Wi-Fi 已连接并且拿到了 IP")
    for a in adapters:
        if a.get("usable") and not a.get("gateway"):
            hints.append("网卡「%s」没有默认网关，通常说明这块网卡没接到路由器" % a.get("alias"))
    if hostname.lower() == socket.gethostname().lower():
        hints.append("主机名与 Windows 计算机名相同，可能与系统自带的网络发现服务抢名字，"
                     "建议改成别的名字（例如 %s-cls）" % hostname)
    if plan and not any(r["ok"] for r in resolved):
        hints.append("本机暂时解析不到自己的 .local 名字：其它设备一般仍可访问，"
                     "若手机也打不开，多半是路由器屏蔽了 mDNS 多播（可用 IP 直连）")
    return {
        "ok": True,
        "status": mdns_status(),
        "adapters": [{
            "alias": a.get("alias"), "desc": a.get("desc"), "ips": a.get("ips"),
            "all_ips": a.get("all_ips"), "gateway": a.get("gateway"),
            "usable": a.get("usable"), "virtual": a.get("virtual"),
            "reason": a.get("reason"), "iftype": a.get("iftype"),
        } for a in adapters],
        "adapters_text": netif.describe(adapters),
        "plan": plan,
        "resolved": resolved,
        "hints": hints,
        "computer_name": socket.gethostname(),
        "default_route_ip": netif.default_route_ip(),
        "log_file": runlog.log_file_path(),
    }


def register_mdns(cfg, force=False):
    """注册（或更新）mDNS 服务：**一个域名只带一块网卡的地址**。

    重复调用时状态没变就直接返回；变了就 update_service / 重新注册，避免
    "注销后立刻注册同名服务"被自身缓存挡住（NonUniqueNameException）。
    """
    global _mdns_zc, _mdns_infos
    if not HAS_ZEROCONF:
        _MDNS_STATE.update({"ok": False, "at": time.time(), "plan": [], "names": [],
                            "error": "缺少 zeroconf 组件，mDNS 无法使用",
                            "solution": "请使用完整版软件（内含 mDNS 组件）"})
        return False
    with MDNS_LOCK:
        try:
            if not cfg.get("mdns_enabled"):
                for info in _mdns_infos:
                    try:
                        _mdns_zc.unregister_service(info)
                    except Exception:
                        pass
                _mdns_infos = []
                register_mdns._key = None
                _MDNS_STATE.update({"ok": False, "error": "", "solution": "",
                                    "at": time.time(), "plan": [], "names": []})
                return False

            port = int(_running_port or cfg.get("port", 8000))
            secure_port = int(_running_secure_port or cfg.get("secure_port", 9443))
            secure_on = bool(cfg.get("secure_enabled", True))
            plan = get_mdns_plan(cfg)
            if not plan:
                msg = "没有可广播的局域网地址（网卡未连接或没拿到 IPv4）"
                print("[mDNS] " + msg)
                _MDNS_STATE.update({
                    "ok": False, "error": msg, "at": time.time(),
                    "solution": "确认网线/Wi-Fi 已连接、能上网；或在设置里换一块网卡后重启软件",
                    "plan": [], "names": []})
                return False

            # ---- 期望的服务表：name -> (type, addrs, port, hostname, iface, ips)
            wanted = {}
            for p in plan:
                try:
                    addrs = [socket.inet_aton(ip) for ip in p["ips"]]
                except Exception:
                    addrs = []
                if not addrs:
                    continue
                tag = " (%s)" % p["hostname"]     # 实例名带域名：同一局域网多台电脑不会撞名
                wanted["班级叫号器%s._http._tcp.local." % tag] = (
                    "_http._tcp.local.", addrs, port, p["hostname"], p["iface"], p["ips"])
                if secure_on:
                    wanted["班级叫号器%s-安全._https._tcp.local." % tag] = (
                        "_https._tcp.local.", addrs, secure_port, p["hostname"],
                        p["iface"], p["ips"])
            if not wanted:
                _MDNS_STATE.update({"ok": False, "at": time.time(), "plan": plan, "names": [],
                                    "error": "网卡地址无效，无法广播",
                                    "solution": "在设置里更换网卡或重启软件"})
                return False

            # ---- 状态完全没变就不动（最常见：向导里连续保存两次）
            state_key = (secure_on, secure_port, tuple(sorted(
                (n, t, tuple(a), sp) for n, (t, a, sp, _h, _i, _ips) in wanted.items())))
            if (not force and _mdns_infos
                    and getattr(register_mdns, "_key", None) == state_key):
                return True

            # ---- 响应器：只绑定真实网卡（虚拟网卡不参与收发）
            ifaces = []
            for p in plan:
                for ip in p["ips"]:
                    if ip not in ifaces:
                        ifaces.append(ip)
            if _mdns_zc is None:
                try:
                    _mdns_zc = Zeroconf(interfaces=ifaces,
                                        ip_version=(IPVersion.V4Only if IPVersion else None))
                except Exception as exc:
                    print("[mDNS] 按网卡创建响应器失败（%r），改用系统默认" % (exc,))
                    _mdns_zc = Zeroconf()
                register_mdns._ifaces = ifaces
            elif getattr(register_mdns, "_ifaces", None) != ifaces:
                try:
                    _mdns_zc.update_interfaces(
                        interfaces=ifaces,
                        ip_version=(IPVersion.V4Only if IPVersion else None))
                    register_mdns._ifaces = ifaces
                    print("[mDNS] 网卡地址变化，已重新绑定: %s" % (", ".join(ifaces) or "-"))
                except Exception as exc:
                    print("[mDNS] 重新绑定网卡失败（%r），重建响应器" % (exc,))
                    try:
                        _mdns_zc.close()
                    except Exception:
                        pass
                    _mdns_zc = Zeroconf()
                    register_mdns._ifaces = ifaces
            zc = _mdns_zc

            # ---- 注销不再需要的
            keep = []
            for info in list(_mdns_infos):
                if info.name not in wanted:
                    try:
                        zc.unregister_service(info)
                    except Exception:
                        pass
                else:
                    keep.append(info)

            # ---- 更新 / 注册
            new_infos = []
            errors = []
            for name, (stype, addrs, sport, host_name, iface_name, ips) in wanted.items():
                server = "%s.local." % host_name
                existing = next((x for x in keep if x.name == name), None)
                if existing is not None:
                    try:
                        zc.update_service(ServiceInfo(
                            stype, name, addresses=addrs, port=sport,
                            properties={b"path": b"/"}, server=server))
                        new_infos.append(existing)
                        print("[mDNS] 已更新 %s  网卡=%s  IP=%s" % (name, iface_name,
                                                                 ", ".join(ips)))
                        continue
                    except Exception as exc:
                        print("[mDNS] 更新 %s 失败，改为重注册: %r" % (name, exc))
                info = ServiceInfo(stype, name, addresses=addrs, port=sport,
                                   properties={b"path": b"/", b"host": host_name.encode(),
                                               b"ver": b"2.0.30"},
                                   server=server)
                ok = False
                for attempt in (1, 2):
                    try:
                        # allow_name_change：万一和别的电脑撞名，自动改成 名字-2，
                        # 而不是整台电脑的 mDNS 直接注册失败
                        zc.register_service(info, allow_name_change=True)
                        ok = True
                        break
                    except Exception as exc:
                        kind = type(exc).__name__
                        if attempt == 1 and "NonUniqueName" in kind:
                            try:
                                zc.unregister_service(info)
                            except Exception:
                                pass
                            time.sleep(0.3)
                            continue
                        errors.append("%s：%s" % (name, exc))
                        print("[mDNS] 注册 %s 失败（%s）: %r" % (name, iface_name, exc))
                        break
                if ok:
                    new_infos.append(info)
                    print("[mDNS] 已注册 %s  网卡=%s  IP=%s" % (info.name, iface_name,
                                                             ", ".join(ips)))

            _mdns_infos = new_infos
            register_mdns._key = state_key
            ok = bool(new_infos)
            for p in plan:
                print("[mDNS] 手机访问地址：http://%s.local:%d（网卡 %s，IP %s）"
                      % (p["hostname"], port, p["iface"], ", ".join(p["ips"])))
            _MDNS_STATE.update({
                "ok": ok, "at": time.time(), "plan": plan,
                "names": [i.name for i in new_infos],
                "error": "" if ok else (errors[0] if errors else "mDNS 注册失败"),
                "solution": "" if ok else "到设置里点「mDNS 诊断」查看原因；"
                                          "手机也可直接用 http://IP:端口 访问",
            })
            return ok
        except Exception as exc:
            print("[mDNS] 注册失败（不影响叫号功能）:", exc)
            _MDNS_STATE.update({"ok": False, "at": time.time(),
                                "error": str(exc),
                                "solution": "到设置里点「mDNS 诊断」查看网卡情况；"
                                            "手机也可直接用 http://IP:端口 访问"})
            return False


def _mdns_watcher_loop():
    """盯着网卡变化：换网、插网线、Wi-Fi 连上、IP 变了 → 自动重新注册。

    以前只在启动时注册一次，所以"开机时 Wi-Fi 还没连上"或"中途换了网络"
    之后 mDNS 就一直不生效 —— 这也是"有时能解析有时不能"的原因之一。
    """
    last_sig = None
    while not _MDNS_STOP.wait(_MDNS_WATCH_INTERVAL):
        try:
            cfg = get_config()
            if not cfg.get("mdns_enabled"):
                last_sig = None
                continue
            sig = netif.signature()
            if sig == last_sig and _MDNS_STATE.get("ok"):
                continue
            if last_sig is not None and sig != last_sig:
                print("[mDNS] 检测到网络变化，重新注册…")
            ok = register_mdns(cfg, force=True)
            last_sig = sig if ok else None
        except Exception as exc:
            print("[mDNS] 监听线程异常:", exc)


def start_mdns_watcher():
    """启动 mDNS 监听线程（幂等）。"""
    global _MDNS_WATCHER
    if not HAS_ZEROCONF:
        return
    if _MDNS_WATCHER is not None and _MDNS_WATCHER.is_alive():
        return
    _MDNS_STOP.clear()
    _MDNS_WATCHER = threading.Thread(target=_mdns_watcher_loop, daemon=True,
                                     name="mdns-watcher")
    _MDNS_WATCHER.start()


'''

src = src[:start] + NEW + src[end:]
io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("ok, register_mdns rewritten")
