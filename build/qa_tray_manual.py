# -*- coding: utf-8 -*-
"""验收：托盘菜单里的「📖 使用说明」（真实构建菜单 + 模拟点击项号）。

做法：真的调 CreatePopupMenu/AppendMenu 建出菜单（这样能验证分隔符等不会被 pywin32 拒绝），
只把 TrackPopupMenu 换成"返回 4"来模拟点了「使用说明」，再检查它打开的 URL。

用软件自带 python 跑：build\zip\班级叫号器\python\python.exe build\qa_tray_manual.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CODE = r"C:\Users\李语晨\Desktop\班级叫号器\2.0.0"
sys.path.insert(0, CODE)

import win32con  # noqa: E402
import win32gui  # noqa: E402
import desktop  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    return bool(cond)


items = []
orig_append = win32gui.AppendMenu


def fake_append(menu, flags, item_id, text):
    items.append({"flags": flags, "id": item_id, "text": text})
    return orig_append(menu, flags, item_id, text)


win32gui.AppendMenu = fake_append

clicked = {"id": 4}
win32gui.TrackPopupMenu = lambda *a, **k: clicked["id"]

opened = []
_real_startfile = os.startfile
os.startfile = lambda p: opened.append(p)


def new_ui(port=8123):
    ui = object.__new__(desktop.DesktopUI)
    ui.port = port
    ui._updating = False
    ui._hwnd = 0
    return ui


# ---------- 1. 菜单项 ----------
print("== 1. 菜单结构 ==")
ui = new_ui()
ui._show_tray_menu()
texts = [it["text"] for it in items]
ids = [it["id"] for it in items]
print("   菜单项:", texts)
check("菜单里有「📖 使用说明」", any("使用说明" in t for t in texts), texts)
check("使用说明的项号是 4", any(it["id"] == 4 and "使用说明" in it["text"] for it in items), ids)
check("「打开网页」排第一", bool(texts) and texts[0] == "打开网页", texts[:2])
check("「使用说明」排第二", len(texts) > 1 and "使用说明" in texts[1], texts[:2])
check("菜单共 5 项（含分隔符）", len(items) == 5, len(items))

# ---------- 2. 点了使用说明 ----------
print("== 2. 模拟点击「📖 使用说明」 ==")
opened.clear()
ui = new_ui(port=8123)
ui._show_tray_menu()
check("打开了 /manual 页面", opened == ["http://127.0.0.1:8123/manual"], opened)

# ---------- 3. 其它项没被弄坏 ----------
print("== 3. 其它菜单项仍然正常 ==")
opened.clear()
clicked["id"] = 1
new_ui(port=8124)._show_tray_menu()
check("「打开网页」仍打开首页", opened == ["http://127.0.0.1:8124"], opened)

quit_called = {"n": 0}
clicked["id"] = 2
ui = new_ui()
ui._quit_app = lambda: quit_called.__setitem__("n", quit_called["n"] + 1)
ui._show_tray_menu()
check("「退出」仍然走退出流程", quit_called["n"] == 1, quit_called)

upd_called = {"n": 0}
clicked["id"] = 3
ui = new_ui()
ui._open_update_page = lambda: upd_called.__setitem__("n", upd_called["n"] + 1)
ui._show_tray_menu()
check("「软件更新」仍然走更新页", upd_called["n"] == 1, upd_called)

# ---------- 4. 真菜单能被系统接受 ----------
print("== 4. 真菜单句柄检查 ==")
menu = win32gui.CreatePopupMenu()
win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "打开网页")
win32gui.AppendMenu(menu, win32con.MF_STRING, 4, "📖 使用说明")
win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
win32gui.AppendMenu(menu, win32con.MF_STRING, 2, "退出")
cnt = win32gui.GetMenuItemCount(menu)
win32gui.DestroyMenu(menu)
check("系统实际接受 4 个菜单项", cnt == 4, cnt)

os.startfile = _real_startfile
ok = sum(1 for r in results if r[0])
print("\n===== 结果：%d/%d 通过 =====" % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
