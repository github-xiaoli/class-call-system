# -*- coding: utf-8 -*-
"""patch 2.0.30 - 1：把网卡枚举 / mDNS 分组整段换成 netif 实现。"""
import io
import re

P = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0\app.py"
src = io.open(P, encoding="utf-8").read()

start_marker = "def get_network_interfaces():"
end_marker = "def normalize_hostname(name):"
i = src.index(start_marker)
j = src.index(end_marker)
assert i < j

NEW = '''def get_network_interfaces():
    """枚举网卡（用于设置界面选择 mDNS 绑定网卡，以及诊断显示）。

    返回 [{id, name, ips, virtual, usable, gateway, desc, reason}]，
    id/name 用 Windows 网卡别名（如 以太网 / WLAN）。虚拟网卡会标注出来，
    以免老师误选一块手机根本连不上的网卡。
    """
    interfaces = []
    try:
        for it in netif.enum_adapters():
            ips = it.get("ips") or it.get("all_ips") or []
            if not ips:
                continue
            alias = it.get("alias") or ("网卡%s" % it.get("index"))
            if it.get("virtual"):
                tag = "（虚拟网卡）"
            elif not it.get("usable"):
                tag = "（%s）" % (it.get("reason") or "不可用")
            elif not it.get("gateway"):
                tag = "（无默认网关）"
            else:
                tag = ""
            interfaces.append({
                "id": alias,
                "name": alias + tag,
                "ips": ips,
                "virtual": bool(it.get("virtual")),
                "usable": bool(it.get("usable")),
                "gateway": bool(it.get("gateway")),
                "desc": it.get("desc") or "",
                "reason": it.get("reason") or "",
            })
    except Exception as exc:
        print("[网卡] 枚举网卡失败:", exc)
    return interfaces


def _mdns_ip_ok(ip):
    """mDNS 广播时该 IP 有没有意义（内部走 netif 的判定规则）。"""
    return netif.ip_ok(ip)


def _mdns_domain(hostname, n):
    """第 n 块网卡用哪个域名：主机名、主机名-2、主机名-3……

    绝不让两块网卡共用同一个域名 —— zeroconf 会把同域名（server）的 A 记录
    合并成一条记录集，那样手机上就会拿到"一半能连、一半连不上"的地址列表。
    """
    hostname = normalize_hostname(hostname)
    return hostname if n <= 1 else "%s-%d" % (hostname, n)


def get_mdns_plan(cfg):
    """生成 mDNS 广播计划：[{hostname, ips, iface, primary, virtual}]。

    每块**真实**网卡一个独立域名，并且每个域名只带 1 个 IP。
    虚拟网卡（Hyper-V / WSL / Wi-Fi Direct / VPN …）一律不广播。
    """
    hostname = normalize_hostname(cfg.get("mdns_hostname", ""))
    wanted = str(cfg.get("mdns_interface", "") or "").strip()
    try:
        adapters = netif.enum_adapters()
    except Exception as exc:
        print("[mDNS] 枚举网卡失败:", exc)
        adapters = []

    usable = [a for a in adapters if a.get("usable") and a.get("ips")]
    if not usable:
        ip = netif.default_route_ip()
        if netif.ip_ok(ip):
            print("[mDNS] 没有识别到可用真实网卡，退回默认路由地址 %s" % ip)
            return [{"hostname": hostname, "ips": [ip], "iface": "默认路由",
                     "primary": True, "virtual": False}]
        return []

    if wanted and wanted.lower() not in ("auto", "自动", "全部"):
        picked = [a for a in usable
                  if wanted in (a.get("alias") or "", a.get("desc") or "")]
        if picked:
            usable = picked
        else:
            print("[mDNS] 设置里指定的网卡「%s」不存在，改用自动选择" % wanted)

    plan = []
    n = 0
    for a in usable:
        ips = [ip for ip in a.get("ips", []) if netif.ip_ok(ip)]
        if not ips:
            continue
        n += 1
        plan.append({"hostname": _mdns_domain(hostname, n), "ips": ips[:1],
                     "iface": a.get("alias") or "-", "primary": n == 1,
                     "virtual": bool(a.get("virtual"))})
    return plan


def get_mdns_targets(cfg):
    """按网卡返回分组信息（设置界面、自检、诊断用）。"""
    out = []
    for p in get_mdns_plan(cfg):
        out.append({"id": p["iface"], "name": p["iface"], "ips": p["ips"],
                    "virtual": p["virtual"], "hostname": p["hostname"],
                    "primary": p["primary"]})
    return out


def get_mdns_ips(cfg):
    """mDNS 实际广播出去的 IP（所有域名合并、去重，主网卡在最前）。"""
    out = []
    for p in get_mdns_plan(cfg):
        for ip in p["ips"]:
            if ip not in out:
                out.append(ip)
    return out


'''

src = src[:i] + NEW + src[j:]
io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("ok, replaced netif block")
