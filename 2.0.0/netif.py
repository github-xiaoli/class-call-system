# -*- coding: utf-8 -*-
# 班级叫号器 (Class Caller)  Copyright (C) 2026 xiaoli
# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows 网卡枚举（GetAdaptersAddresses，纯标准库 ctypes）。

为什么不用 PowerShell / WMI：
    以前的实现是 `powershell Get-NetIPAddress`，教室电脑常被组策略或 AppLocker
    限制 PowerShell，一旦失败就退化成"把所有 IP 合并成一块网卡"，于是
    Hyper-V / WSL / Wi-Fi Direct 这些连不上的虚拟网卡地址也被广播出去，
    手机随机拿到它们 —— 表现就是"有时候能打开、有时候打不开"。
    GetAdaptersAddresses 是系统 API，毫秒级返回，且能直接读到
    「有没有默认网关」「运行状态」「网卡类型」「友好名称/描述」，
    足以准确区分真实网卡与虚拟网卡。

对外接口：
    enum_adapters(force=False) -> [adapter dict]
    pick_primary(adapters=None) -> adapter dict | None
    mdns_plan(adapters=None)    -> [(hostname_suffix, [ip...], adapter)]  # 由 app 决定域名
    signature()                 -> 用于检测网卡变化（换网、插拔、Wi-Fi 连上）
    default_route_ip()          -> UDP 探测出的默认路由 IP（最后兜底）
    describe(adapters)          -> 诊断用的可读文本
"""

import ctypes
import json
import os
import socket
import threading
import time
from ctypes import wintypes

LOCK = threading.Lock()
_CACHE = {"ts": 0.0, "list": []}
CACHE_TTL = 5.0          # 枚举缓存秒数（避免每次请求都进系统 API，也保证换网后能及时发现）

# IF_TYPE（ipifcons.h）
IF_TYPE_ETHERNET = 6
IF_TYPE_PPP = 23
IF_TYPE_LOOPBACK = 24
IF_TYPE_WIRELESS = 71
IF_TYPE_TUNNEL = 131
IF_TYPE_WWANPP = 243

# IF_OPER_STATUS
OPER_UP = 1

# 描述/别名里出现这些词，说明是虚拟/隧道网卡：它们的地址手机永远连不上
_VIRTUAL_HINTS = (
    "vethernet", "hyper-v", "hyperv", "virtual", "wi-fi direct", "wifi direct",
    "vmware", "virtualbox", "vbox", "docker", "wsl", "npcap", "loopback",
    "teredo", "6to4", "isatap", "tap-windows", "tap adapter", "tun", "vpn",
    "zerotier", "tailscale", "wireguard", "openvpn", "sangfor", "easyconnect",
    "clash", "vmnet", "wan miniport", "bluetooth", "蓝牙", "虚拟", "隧道",
    "环回", "微型端口",
)


# ---------------------------------------------------------------------------
# ctypes 结构体定义（与 iphlpapi.h 对应，x64/x86 均可）
# ---------------------------------------------------------------------------
class _SOCKADDR(ctypes.Structure):
    _fields_ = [("sa_family", ctypes.c_ushort), ("sa_data", ctypes.c_char * 14)]


class _SOCKADDR_IN(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_ushort), ("sin_port", ctypes.c_ushort),
                ("sin_addr", ctypes.c_ubyte * 4), ("sin_zero", ctypes.c_char * 8)]


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.POINTER(_SOCKADDR)), ("iSockaddrLength", ctypes.c_int)]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG), ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int), ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG), ("PreferredLifetime", wintypes.ULONG),
    ("LeaseLifetime", wintypes.ULONG), ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG), ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.ULONG), ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", wintypes.DWORD), ("ZoneIndices", wintypes.ULONG * 16),
    ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_ulonglong), ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ("FirstWinsServerAddress", ctypes.c_void_p), ("FirstGatewayAddress", ctypes.c_void_p),
]

_GAA_FLAG_SKIP_ANYCAST = 0x0002
_GAA_FLAG_SKIP_MULTICAST = 0x0004
_GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
_AF_INET = 2
_ERROR_BUFFER_OVERFLOW = 111


# ---------------------------------------------------------------------------
# 基础判断
# ---------------------------------------------------------------------------
def ip_ok(ip):
    """这个 IPv4 地址有没有可能被手机访问到。"""
    ip = str(ip or "").strip()
    if not ip:
        return False
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except Exception:
        return False
    if any(n < 0 or n > 255 for n in nums):
        return False
    if nums[0] in (0, 127):                       # 0.x / 回环
        return False
    if nums[0] == 169 and nums[1] == 254:         # 链路本地，说明没拿到 DHCP
        return False
    if nums[0] >= 224:                            # 组播 / 保留
        return False
    return True


def looks_virtual(adapter):
    """虚拟/隧道网卡判定：名称或描述里带关键词，或类型是回环/隧道/PPP。"""
    text = ("%s %s" % (adapter.get("alias") or "", adapter.get("desc") or "")).lower()
    if any(h in text for h in _VIRTUAL_HINTS):
        return True
    return adapter.get("iftype") in (IF_TYPE_LOOPBACK, IF_TYPE_TUNNEL, IF_TYPE_PPP)


def _score(adapter):
    """给网卡打分：分高的更适合作为"手机该访问的那块网卡"。"""
    s = 0
    if looks_virtual(adapter):
        s -= 200
    if adapter.get("iftype") in (IF_TYPE_ETHERNET, IF_TYPE_WIRELESS):
        s += 50
    if adapter.get("gateway"):
        s += 100
    if adapter.get("oper") == OPER_UP:
        s += 20
    if adapter.get("ips"):
        s += 10
    if adapter.get("iftype") == IF_TYPE_WWANPP:   # 蜂窝网卡，一般不是我们要的
        s -= 80
    return s


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
def _enum_raw():
    """调用 GetAdaptersAddresses 拿到原始网卡列表（不缓存）。"""
    size = wintypes.ULONG(16 * 1024)
    buf = ctypes.create_string_buffer(size.value)
    flags = (_GAA_FLAG_SKIP_ANYCAST | _GAA_FLAG_SKIP_MULTICAST
             | _GAA_FLAG_INCLUDE_GATEWAYS)
    rc = 0
    for _ in range(4):
        rc = ctypes.windll.iphlpapi.GetAdaptersAddresses(
            _AF_INET, flags, None, buf, ctypes.byref(size))
        if rc != _ERROR_BUFFER_OVERFLOW:
            break
        buf = ctypes.create_string_buffer(size.value)
    if rc != 0:
        raise OSError("GetAdaptersAddresses 失败, 返回码 %s" % rc)

    out = []
    p = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    while p:
        a = p.contents
        ips = []
        u = a.FirstUnicastAddress
        while u:
            sa = u.contents.Address.lpSockaddr
            if sa and sa.contents.sa_family == _AF_INET:
                si = ctypes.cast(sa, ctypes.POINTER(_SOCKADDR_IN)).contents
                ip = "%d.%d.%d.%d" % tuple(si.sin_addr[i] for i in range(4))
                if ip not in ips:
                    ips.append(ip)
            u = u.contents.Next
        mac = ""
        try:
            n = min(6, max(0, int(a.PhysicalAddressLength)))
            mac = "".join("%02X" % a.PhysicalAddress[i] for i in range(n))
        except Exception:
            pass
        out.append({
            "index": int(a.IfIndex),
            "alias": a.FriendlyName or "",       # 以太网 / WLAN
            "desc": a.Description or "",         # 网卡型号描述
            "iftype": int(a.IfType),
            "oper": int(a.OperStatus),
            "mac": mac,
            "gateway": bool(a.FirstGatewayAddress),
            "ips": ips,
            "mtu": int(a.Mtu or 0),
        })
        p = a.Next
    return out


def enum_adapters(force=False):
    """枚举带 IPv4 的网卡，附带 usable/virtual/reason 结论。

    usable=True 才是"手机真的能连上"的网卡，会被用于 mDNS 广播。
    """
    now = time.time()
    with LOCK:
        if not force and _CACHE["list"] and (now - _CACHE["ts"]) < CACHE_TTL:
            return [dict(it) for it in _CACHE["list"]]
    try:
        raw = _enum_raw()
    except Exception as exc:
        print("[网卡] 枚举失败: %r" % (exc,))
        raw = []

    items = []
    for a in raw:
        good_ips = [ip for ip in a["ips"] if ip_ok(ip)]
        virtual = looks_virtual(a)
        reason = ""
        if virtual:
            reason = "虚拟/隧道网卡"
        elif not a["ips"]:
            reason = "没有 IPv4 地址"
        elif not good_ips:
            reason = "只有链路本地地址（169.254，未拿到 DHCP）"
        elif a["oper"] != OPER_UP:
            reason = "网卡未连接"
        elif a["iftype"] not in (IF_TYPE_ETHERNET, IF_TYPE_WIRELESS, IF_TYPE_WWANPP):
            reason = "不是以太网/无线网卡（类型 %s）" % a["iftype"]
        it = dict(a)
        it["ips"] = good_ips
        it["all_ips"] = a["ips"]
        it["virtual"] = virtual
        it["usable"] = not reason
        it["reason"] = reason
        it["score"] = _score(a)
        items.append(it)

    items.sort(key=lambda x: (-x["score"], x["index"]))
    with LOCK:
        _CACHE["ts"] = time.time()
        _CACHE["list"] = [dict(it) for it in items]
    return items


def usable_adapters(adapters=None):
    items = adapters if adapters is not None else enum_adapters()
    return [it for it in items if it.get("usable")]


def pick_primary(adapters=None):
    """挑出"手机最应该访问的那块网卡"：分数最高（有默认网关的真实网卡优先）。"""
    usable = usable_adapters(adapters)
    return usable[0] if usable else None


def default_route_ip():
    """最后兜底：UDP 探测默认路由 IP（不需要真的联网，只是让系统选路）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return ip if ip_ok(ip) else ""
        finally:
            s.close()
    except Exception:
        return ""


def signature(adapters=None):
    """网卡状态指纹：变化了就说明换网/插拔/Wi-Fi 连上了，需要重新注册 mDNS。"""
    items = adapters if adapters is not None else enum_adapters(force=True)
    return tuple(sorted((it["index"], it["alias"], tuple(it["ips"]), it["oper"],
                         it["gateway"]) for it in items if it.get("usable")))


def describe(adapters=None):
    """诊断用的可读文本（网页「mDNS 诊断」与日志里用）。"""
    items = adapters if adapters is not None else enum_adapters()
    if not items:
        return "没有枚举到任何网卡"
    lines = []
    for it in items:
        flag = "可用" if it["usable"] else "跳过"
        extra = "" if it["usable"] else "（%s）" % it["reason"]
        gw = "有默认网关" if it["gateway"] else "无默认网关"
        lines.append("[%s] %s %s｜%s｜%s｜%s%s" % (
            flag, it["alias"], it["ips"] or "-", gw, it["desc"], it["mac"] or "-", extra))
    return "\n".join(lines)


def dump(path):
    """把枚举结果写到 JSON（排障用）。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(enum_adapters(force=True), f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    # 直接运行：打印本机网卡判定结果，方便排障
    print(describe())
    p = pick_primary()
    print("主网卡:", (p["alias"] if p else "无"), (p["ips"] if p else ""))
