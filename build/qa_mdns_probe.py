# -*- coding: utf-8 -*-
"""mDNS 观测探针（用软件自带的 zeroconf 跑，等价于"手机上看到什么"）。

用法：
    python build\qa_mdns_probe.py collect 4
    python build\qa_mdns_probe.py resolve qa30test 4
输出 JSON 到 stdout（ASCII 安全，便于父进程解析）。
"""
import json
import sys
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

ACTION = sys.argv[1] if len(sys.argv) > 1 else "collect"
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
HOST = sys.argv[3] if len(sys.argv) > 3 else ""

seen = set()


class _C(ServiceListener):
    def add_service(self, zc, type_, name):
        seen.add((type_, name))

    def update_service(self, zc, type_, name):
        seen.add((type_, name))

    def remove_service(self, zc, type_, name):
        seen = None


def main():
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_http._tcp.local.", _C())
        ServiceBrowser(zc, "_https._tcp.local.", _C())
        time.sleep(WAIT)
        names = sorted({n for (_t, n) in seen})
        out = {"names": names, "detail": {}, "addr_of_host": {}}
        for n in names:
            type_ = "_https._tcp.local." if n.endswith("_https._tcp.local.") else "_http._tcp.local."
            info = zc.get_service_info(type_, n, timeout=2500)
            if info is None:
                out["detail"][n] = None
                continue
            out["detail"][n] = {"server": info.server, "port": info.port,
                                "addresses": sorted(info.parsed_addresses())}
            srv = (info.server or "").lower()
            out["addr_of_host"].setdefault(srv, set())
            out["addr_of_host"][srv] |= set(info.parsed_addresses())
        out["addr_of_host"] = {k: sorted(v) for k, v in out["addr_of_host"].items()}
        if HOST:
            key = (HOST + ".local.").lower()
            out["target"] = out["addr_of_host"].get(key, [])
    finally:
        zc.close()
    print(json.dumps(out, ensure_ascii=True))


main()
