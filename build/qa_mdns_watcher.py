# -*- coding: utf-8 -*-
"""验证 mDNS 监听线程：网卡变化才重新注册（模拟换网，不动真实网卡）。

做法：把 app.py 复制到临时目录，import 之后替换 netif.signature / register_mdns，
用 1 秒的检测间隔观察 6 秒，断言"注册次数正好 2 次（启动 + 换网）"。

用软件自带的 python 跑：build\zip\班级叫号器\python\python.exe build\qa_mdns_watcher.py
"""
import json
import os
import shutil
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WS = r"C:\Users\李语晨\Desktop\班级叫号器"
SRC = os.path.join(WS, "2.0.0")
WORK = os.path.join(WS, "_qa30_watch")

results = []


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, "data"))
shutil.copytree(SRC, os.path.join(WORK, "2.0.0"),
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
with open(os.path.join(WORK, "version.json"), "w", encoding="utf-8") as f:
    json.dump({"version": "2.0.30", "edition": "beta", "data_dir": "data",
               "python_dir": "python"}, f)
with open(os.path.join(WORK, "data", "config.json"), "w", encoding="utf-8") as f:
    json.dump({"port": 8730, "mdns_enabled": True, "mdns_hostname": "watchtest",
               "require_admin": False, "secure_enabled": False}, f)
with open(os.path.join(WORK, "data", "state.json"), "w", encoding="utf-8") as f:
    json.dump({"wizard_done": True}, f)

os.chdir(WORK)
sys.path.insert(0, os.path.join(WORK, "2.0.0"))
import app  # noqa: E402

calls = []
sig_state = {"i": 0}
SIGS = [("WLAN", ("192.168.1.19",)), ("WLAN", ("192.168.1.19",)),
        ("WLAN", ("192.168.1.19",)), ("WLAN", ("192.168.1.77",)),
        ("WLAN", ("192.168.1.77",)), ("WLAN", ("192.168.1.77",))]
sig_calls = []


def fake_sig(*_a, **_k):
    sig_calls.append(1)
    return SIGS[min(sig_state["i"], len(SIGS) - 1)]


def fake_register(cfg, force=False):
    calls.append({"t": time.time(), "force": force})
    app._MDNS_STATE["ok"] = True     # 假装注册成功
    return True


app.netif.signature = fake_sig
app.register_mdns = fake_register
app._MDNS_WATCH_INTERVAL = 0.5

print("== 启动监听线程（模拟 6 个检测周期）==")
app.start_mdns_watcher()
for i in range(6):
    time.sleep(0.5)
    sig_state["i"] = i
time.sleep(1.2)
app._MDNS_STOP.set()
time.sleep(0.3)

check("监听线程有在轮询网卡状态", len(sig_calls) >= 3, len(sig_calls))
check("首次检测即注册一次", len(calls) >= 1, len(calls))
check("网卡没变化时不重复注册（只注册 2 次：启动 + 换网）", len(calls) == 2, len(calls))
check("变化后重新注册用的是 force=True",
      bool(calls) and all(c["force"] for c in calls), [c["force"] for c in calls])
check("注册成功后被记录为 ok", app._MDNS_STATE.get("ok") is True)

# 再验一次：关闭 mDNS 后线程应该不再注册
calls.clear()
app._MDNS_STATE["ok"] = False
cfg = app.get_config()
cfg["mdns_enabled"] = False
app.save_config(cfg)
app._MDNS_STOP.clear()
app._MDNS_WATCHER = None
app.start_mdns_watcher()
time.sleep(2.0)
app._MDNS_STOP.set()
check("关闭 mDNS 后不再尝试注册", len(calls) == 0, len(calls))

ok = sum(1 for r in results if r[0])
print("\n===== 结果：%d/%d 通过 =====" % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
