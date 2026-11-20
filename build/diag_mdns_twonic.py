# -*- coding: utf-8 -*-
"""复现：多网卡共用同一个 server 名（<hostname>.local.）时，mDNS 里到底广播了什么。

等价于 app.py 里 register_mdns() 在多网卡分组下的行为：
  两个不同实例名，但 server 都写 "mtest.local."，addresses 各自只有自己那张网卡的 IP。
注册进程在子进程里跑 18 秒，父进程用 ServiceBrowser 观察手机端会看到什么。
"""
import json
import os
import socket
import subprocess
import sys
import time

from zeroconf import Zeroconf, ServiceBrowser, ServiceInfo, ServiceListener

HOST = "mtest"
GROUPS = [("有线", "192.168.1.19"), ("无线", "10.9.9.9")]   # 两小组，故意用两个不同网段
MODE = sys.argv[1] if len(sys.argv) > 1 else "browse"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "diag_mdns_twonic.json")


def register():
    zc = Zeroconf()
    infos = []
    for tag, ip in GROUPS:
        name = "班级叫号器 (%s)._http._tcp.local." % tag
        info = ServiceInfo("_http._tcp.local.", name,
                           addresses=[socket.inet_aton(ip)], port=8000,
                           properties={b"path": b"/"},
                           server="%s.local." % HOST)
        try:
            zc.register_service(info)
            infos.append(info)
            print("[reg] ok", name, ip, flush=True)
        except Exception as exc:
            print("[reg] FAIL", name, repr(exc), flush=True)
    time.sleep(18)
    zc.close()


class L(ServiceListener):
    def add_service(self, zc, type_, name):
        print("ADD", name, flush=True)

    def update_service(self, zc, type_, name):
        print("UPD", name, flush=True)

    def remove_service(self, zc, type_, name):
        print("DEL", name, flush=True)


def browse():
    zc = Zeroconf()
    l = L()
    ServiceBrowser(zc, "_http._tcp.local.", [l])
    time.sleep(3.0)
    names = [n for n in ("班级叫号器 (有线)._http._tcp.local.", "班级叫号器 (无线)._http._tcp.local.")]
    detail = {}
    for n in names:
        info = zc.get_service_info("_http._tcp.local.", n, timeout=3000)
        detail[n] = None if info is None else {
            "server": info.server, "port": info.port,
            "addr": info.parsed_addresses()}
    # 再单独解析 server 名本身的 A 记录（手机就是靠这个拿 IP）
    a = {}
    for nm in (HOST + ".local.",):
        try:
            detail_a = zc.get_service_info("_http._tcp.local.", names[0], timeout=2000)
            a[nm] = detail_a.parsed_addresses() if detail_a else None
        except Exception as exc:
            a[nm] = repr(exc)
    # 用系统解析器再看一眼
    try:
        sysres = sorted({i[4][0] for i in socket.getaddrinfo(HOST + ".local", None, socket.AF_INET)})
    except Exception as exc:
        sysres = repr(exc)
    zc.close()
    out = {"instances": detail, "server_a_records_via_service": a, "system_resolver": sysres}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=True, indent=2))


if MODE == "register":
    register()
else:
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "register"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             creationflags=0x08000000)
    time.sleep(5.0)
    try:
        browse()
    finally:
        child.terminate()
        try:
            print(child.communicate(timeout=5)[0][:800])
        except Exception:
            pass
