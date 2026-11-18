# -*- coding: utf-8 -*-
"""用正式安装包做端到端验证：静默安装到指定目录 → 校验 → 静默卸载 → 校验。

用法：python build\\test_installer_full.py            （装到 build\\tools\\e2e）
      python build\\test_installer_full.py --keep     （装完不卸载，留着手动看界面）
"""
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"C:\Users\李语晨\Desktop\班级叫号器"
APP = "班级叫号器"
VER = "2.0.19"
EXE = os.path.join(ROOT, "build", "out", "%s_安装包_v%s.exe" % (APP, VER))
TARGET = os.path.join(ROOT, "build", "tools", "e2e")

ok_all = True


def log(m):
    print(m, flush=True)


def check(cond, msg):
    global ok_all
    log(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        ok_all = False
    return cond


def run_elevated(exe, params="", timeout=1200):
    SEE_MASK_NOCLOSEPROCESS = 0x00000040

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = os.path.dirname(exe)
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError()
    handle = info.hProcess
    if ctypes.windll.kernel32.WaitForSingleObject(handle, timeout * 1000) == 0x102:
        raise RuntimeError("超时")
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value


def main():
    keep = "--keep" in sys.argv
    log("安装包：%s（%.1f MB）" % (EXE, os.path.getsize(EXE) / 1048576.0))
    shutil.rmtree(TARGET, ignore_errors=True)

    log("\n== 静默安装到 %s ==" % TARGET)
    t0 = time.time()
    code = run_elevated(EXE, "/S /D=" + TARGET)
    log("  耗时 %.0fs，退出码 %s" % (time.time() - t0, code))
    time.sleep(1.5)

    log("\n== 校验安装结果 ==")
    check(os.path.isdir(TARGET), "安装目录已创建")
    pyw = os.path.join(TARGET, "python", "pythonw.exe")
    check(os.path.isfile(pyw), "python 运行环境已解压")
    check(os.path.isfile(os.path.join(TARGET, "start.py")), "start.py 已解压")
    check(os.path.isfile(os.path.join(TARGET, VER, "app.py")), "程序代码 %s\\app.py 已解压" % VER)
    check(os.path.isfile(os.path.join(TARGET, "version.json")), "version.json 已解压")
    check(os.path.isfile(os.path.join(TARGET, "使用说明.txt")), "中文文件名文件已解压")
    check(os.path.isfile(os.path.join(TARGET, "uninstall.exe")), "卸载程序已生成")
    n = sum(len(f) for _, _, f in os.walk(TARGET))
    check(n > 3000, "解压文件数 %d（应为 3400+）" % n)

    check(os.path.isfile(os.path.join(os.environ["USERPROFILE"], "Desktop", APP + ".lnk"))
          or os.path.isfile(os.path.join(os.environ["PUBLIC"], "Desktop", APP + ".lnk")),
          "桌面快捷方式已创建")
    sm = os.path.join(os.environ["ProgramData"], "Microsoft", "Windows", "Start Menu", "Programs", APP)
    check(os.path.isdir(sm), "开始菜单文件夹已创建（%s）" % sm)

    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s" % APP
    vals = {}
    for hive, name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        try:
            k = winreg.OpenKey(hive, key_path)
        except FileNotFoundError:
            continue
        i = 0
        while True:
            try:
                nm, v, _ = winreg.EnumValue(k, i)
                vals[nm] = v
                i += 1
            except OSError:
                break
        winreg.CloseKey(k)
        log("  卸载项位置：%s" % name)
        break
    check(bool(vals), "控制面板卸载项已写入")
    check(vals.get("DisplayName") == APP, "DisplayName = %s" % vals.get("DisplayName"))
    check(vals.get("DisplayVersion") == VER, "DisplayVersion = %s" % vals.get("DisplayVersion"))
    check("uninstall.exe" in str(vals.get("UninstallString", "")).lower(),
          "UninstallString = %s" % vals.get("UninstallString"))
    check(int(vals.get("EstimatedSize", 0) or 0) > 100000,
          "EstimatedSize = %s KB" % vals.get("EstimatedSize"))

    # 程序能不能真的跑起来（用 -c 检查 import，避免真的开服务）
    log("\n== 程序自检 ==")
    r = subprocess.run([os.path.join(TARGET, "python", "python.exe"), "-c",
                        "import sys;sys.path.insert(0,r'%s');import flask,zeroconf,win32api,PIL;print('imports ok')"
                        % os.path.join(TARGET, VER)],
                       capture_output=True)
    check(r.returncode == 0, "依赖导入正常：%s" % (r.stdout or r.stderr).decode("utf-8", "replace").strip()[:120])

    if keep:
        log("\n（--keep）保留安装，跳过卸载")
        return

    log("\n== 静默卸载 ==")
    code = run_elevated(os.path.join(TARGET, "uninstall.exe"), "/S")
    log("  卸载退出码 %s" % code)
    time.sleep(4)
    check(not os.path.isdir(TARGET) or not os.listdir(TARGET), "安装目录已删除")
    check(not os.path.isfile(os.path.join(os.environ["PUBLIC"], "Desktop", APP + ".lnk")),
          "桌面快捷方式已删除")
    check(not os.path.isdir(sm), "开始菜单文件夹已删除")
    gone = True
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            winreg.OpenKey(hive, key_path)
            gone = False
        except FileNotFoundError:
            pass
    check(gone, "卸载项已删除")
    bak = os.path.join(os.path.expanduser("~"), "Documents", APP + "数据")
    check(os.path.isdir(bak), "数据已备份到 %s" % bak)
    if os.path.isdir(bak):
        shutil.rmtree(bak, ignore_errors=True)

    # cleanup leftovers
    d = os.path.join(os.environ["PUBLIC"], "Desktop", APP + ".lnk")
    if os.path.isfile(d):
        os.remove(d)

    log("\n==== %s ====" % ("全部通过" if ok_all else "有失败项"))
    sys.exit(0 if ok_all else 1)


main()
