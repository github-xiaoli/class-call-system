# -*- coding: utf-8 -*-
"""mDNS 环境诊断（只读）：对比 PowerShell 枚举 vs GetAdaptersAddresses，
并查看防火墙 profile / mDNS 规则 / 网络类别。结果写 JSON，避免控制台编码问题。"""
import ctypes
import json
import os
import socket
import subprocess
import sys
from ctypes import wintypes

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "diag_mdns_env.json")
res = {}

# ---------------- 1. 现在的实现：PowerShell Get-NetIPAddress ----------------
ps = ("Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
      "Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } | "
      "Select-Object InterfaceAlias,IPAddress | ConvertTo-Json -Compress")
try:
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", ps], capture_output=True, text=True, timeout=25,
                       creationflags=0x08000000, check=False)
    res["powershell"] = {"rc": p.returncode, "stdout": (p.stdout or "")[:400],
                         "stderr": (p.stderr or "")[:400],
                         "which": subprocess.run(["where", "powershell"], capture_output=True,
                                                 text=True).stdout.strip()[:200]}
except Exception as exc:
    res["powershell"] = {"error": repr(exc)}

# 同样的命令用 pwsh（PowerShell 7）试一次
try:
    p2 = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps],
                        capture_output=True, text=True, timeout=25,
                        creationflags=0x08000000, check=False)
    res["pwsh"] = {"rc": p2.returncode, "stdout": (p2.stdout or "")[:400], "stderr": (p2.stderr or "")[:200]}
except Exception as exc:
    res["pwsh"] = {"error": repr(exc)}

# ---------------- 2. 替代方案：GetAdaptersAddresses ----------------
class SOCKADDR(ctypes.Structure):
    _fields_ = [("sa_family", ctypes.c_ushort), ("sa_data", ctypes.c_char * 14)]


class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_ushort), ("sin_port", ctypes.c_ushort),
                ("sin_addr", ctypes.c_ubyte * 4), ("sin_zero", ctypes.c_char * 8)]


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.POINTER(SOCKADDR)), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG), ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int), ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG), ("PreferredLifetime", wintypes.ULONG),
    ("LeaseLifetime", wintypes.ULONG), ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
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


def enum_native():
    GAA_FLAG_SKIP_ANYCAST = 0x0002
    GAA_FLAG_SKIP_MULTICAST = 0x0004
    GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
    AF_INET = 2
    size = wintypes.ULONG(16 * 1024)
    buf = ctypes.create_string_buffer(size.value)
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_INCLUDE_GATEWAYS
    for _ in range(4):
        rc = ctypes.windll.iphlpapi.GetAdaptersAddresses(
            AF_INET, flags, None, buf, ctypes.byref(size))
        if rc != 111:      # ERROR_BUFFER_OVERFLOW
            break
        buf = ctypes.create_string_buffer(size.value)
    if rc != 0:
        return {"error": "GetAdaptersAddresses rc=%s" % rc}
    out = []
    p = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while p:
        a = p.contents
        ips = []
        u = a.FirstUnicastAddress
        while u:
            sa = u.contents.Address.lpSockaddr
            if sa and sa.contents.sa_family == AF_INET:
                si = ctypes.cast(sa, ctypes.POINTER(SOCKADDR_IN)).contents
                ips.append("%d.%d.%d.%d" % tuple(si.sin_addr[i] for i in range(4)))
            u = u.contents.Next
        mac = "".join("%02X" % a.PhysicalAddress[i] for i in range(min(6, a.PhysicalAddressLength)))
        out.append({
            "alias": a.FriendlyName, "desc": a.Description, "index": a.IfIndex,
            "iftype": a.IfType, "oper": a.OperStatus, "mac": mac,
            "gw": bool(a.FirstGatewayAddress), "ips": ips,
            "mtu": a.Mtu,
        })
        p = a.Next
    return out


try:
    res["native"] = enum_native()
except Exception as exc:
    res["native"] = {"error": repr(exc)}

# ---------------- 3. 本机名字解析情况 ----------------
try:
    res["gethostname"] = socket.gethostname()
    res["getaddrinfo_local"] = sorted({i[4][0] for i in
                                       socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})
except Exception as exc:
    res["gethostname_err"] = repr(exc)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))
    res["default_route_ip"] = s.getsockname()[0]
except Exception as exc:
    res["default_route_ip"] = repr(exc)
finally:
    s.close()

# ---------------- 4. 防火墙 / 网络类别 ----------------
def run(cmd_list):
    try:
        p = subprocess.run(cmd_list, capture_output=True, text=True, timeout=30,
                           creationflags=0x08000000, check=False)
        return {"rc": p.returncode, "out": (p.stdout or "")[:3000], "err": (p.stderr or "")[:500]}
    except Exception as exc:
        return {"error": repr(exc)}


res["fw_profiles"] = run(["netsh", "advfirewall", "show", "allprofiles", "state"])
res["conn_profile"] = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                           "Get-NetConnectionProfile | Select-Object Name,InterfaceAlias,"
                           "NetworkCategory,IPv4Connectivity | ConvertTo-Json -Compress"])
res["fw_mdns_rules"] = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
                            "Where-Object { $_.DisplayName -match 'mDNS|多播|Multicast' } | "
                            "Select-Object DisplayName,Direction,Action,Enabled,Profile | "
                            "ConvertTo-Json -Compress"])
res["fw_python_rules"] = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
                              "Where-Object { $_.Program -match 'python' } | ForEach-Object { "
                              "$r = $_ | Get-NetFirewallRule; [pscustomobject]@{Program=$_.Program;"
                              "Name=$r.DisplayName;Action=$r.Action;Enabled=$r.Enabled;Profile=$r.Profile} } | "
                              "ConvertTo-Json -Compress"])
res["udp5353_listening"] = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "Get-NetUDPEndpoint -LocalPort 5353 -ErrorAction SilentlyContinue | "
                                "Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress"])

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print("written:", OUT)
print(json.dumps(res, ensure_ascii=True, indent=2)[:6000])
