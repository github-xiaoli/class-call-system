# -*- coding: utf-8 -*-
"""班级叫号器 卸载程序（控制面板「卸载程序」调用）。

用法：
  pythonw uninstall.py          # 带确认弹窗
  pythonw uninstall.py /quiet   # 静默卸载（保留数据到「文档\\班级叫号器数据」）

要点：
- 装在 Program Files 里，删除需要管理员权限 —— 会自动请求提权后重跑
- 自己所在的文件被占用，删除目录交给一个延时批处理完成
- 全过程写日志 %TEMP%\\cqt_uninstall.log
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = "班级叫号器"
QUIET = "/quiet" in [a.lower() for a in sys.argv[1:]]
LOG = os.path.join(tempfile.gettempdir(), "cqt_uninstall.log")
MB_YESNO = 0x04
MB_ICONQUESTION = 0x20
MB_ICONINFORMATION = 0x40
MB_ICONERROR = 0x10


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def msg(text, flags=MB_ICONINFORMATION):
    if QUIET:
        log("QUIET-MSG: " + text.replace("\n", " / "))
        return 6
    return ctypes.windll.user32.MessageBoxW(0, text, APP + " 卸载", flags)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate():
    """请求管理员权限重新运行自己（Program Files 里的文件要管理员才能删）。"""
    exe = sys.executable
    params = '"%s"' % os.path.abspath(__file__) + (" /quiet" if QUIET else "")
    log("请求提权: %s %s" % (exe, params))
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, ROOT, 1)
        return r > 32
    except Exception as exc:
        log("提权失败: %r" % (exc,))
        return False


def kill_app():
    """结束所有从本安装目录启动的进程（最多试 3 轮）。"""
    killed = 0
    me = os.getpid()
    root_l = ROOT.lower()
    for round_no in range(3):
        try:
            import psutil
            for proc in psutil.process_iter():
                try:
                    if proc.pid == me:
                        continue
                    hit = False
                    try:
                        exe = (proc.exe() or "")
                        if exe.lower().startswith(root_l):
                            hit = True
                    except Exception:
                        pass
                    if not hit:
                        try:
                            cl = " ".join(proc.cmdline() or [])
                            if root_l in cl.lower():
                                hit = True
                        except Exception:
                            pass
                    if hit:
                        proc.kill()
                        killed += 1
                except Exception:
                    continue
        except Exception as exc:
            log("psutil 不可用: %r" % (exc,))
            break
        time.sleep(0.6)
        if killed == 0 and round_no >= 1:
            break
    # 兜底：按可执行文件路径再扫一次（防止 psutil 取不到信息）
    try:
        out = subprocess.run(["wmic", "process", "where", "name='pythonw.exe'",
                              "get", "ProcessId,ExecutablePath", "/format:csv"],
                             capture_output=True, text=True, encoding="mbcs",
                             errors="replace").stdout
        for line in out.splitlines():
            if root_l in line.lower():
                parts = [p for p in line.split(",") if p.strip()]
                for p in parts:
                    if p.strip().isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", p.strip()],
                                       capture_output=True)
                        killed += 1
    except Exception:
        pass
    log("结束进程数: %d" % killed)
    time.sleep(0.8)


def remove_shortcuts():
    targets = []
    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    if os.path.isdir(desktop):
        targets.append(os.path.join(desktop, APP + ".lnk"))
    sm = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                      "Start Menu", "Programs")
    targets.append(os.path.join(sm, APP + ".lnk"))
    targets.append(os.path.join(sm, APP, APP + ".lnk"))
    for p in targets:
        if os.path.isfile(p):
            for _ in range(3):
                try:
                    os.remove(p)
                    log("删除快捷方式: " + p)
                    break
                except Exception as exc:
                    log("删除快捷方式失败(重试): %s %r" % (p, exc))
                    time.sleep(0.3)
    folder = os.path.join(sm, APP)
    try:
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
            log("删除开始菜单文件夹: " + folder)
    except Exception:
        pass


def _reg_del_tree(root, path):
    """递归删除注册表键（用 winreg，避免 reg.exe 的转义问题）。"""
    import winreg
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log("打开注册表键失败 %s: %r" % (path, exc))
        return False
    try:
        # 先删子键
        while True:
            try:
                sub = winreg.EnumKey(key, 0)
            except OSError:
                break
            try:
                _reg_del_tree(root, path + "\\" + sub)
            except Exception:
                pass
        # 再删值
        while True:
            try:
                name = winreg.EnumValue(key, 0)[0]
            except OSError:
                break
            try:
                winreg.DeleteValue(key, name)
            except Exception:
                pass
    finally:
        winreg.CloseKey(key)
    try:
        winreg.DeleteKey(root, path)
        return True
    except OSError as exc:
        log("删除注册表键失败 %s: %r" % (path, exc))
        return False


def remove_registry():
    import winreg
    sub = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + APP
    for root, name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                       (winreg.HKEY_CURRENT_USER, "HKCU")):
        ok = _reg_del_tree(root, sub)
        log("删除卸载项 %s\\%s -> %s" % (name, sub, "成功" if ok else "未找到/失败"))


def remove_autostart():
    subprocess.run(["schtasks", "/Delete", "/TN", APP, "/F"], capture_output=True)
    for hive in ("HKCU", "HKLM"):
        subprocess.run(["reg", "delete",
                        hive + r"\SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
                        "/v", APP, "/f"], capture_output=True)
    log("已清理开机自启（计划任务 + Run 项）")


def schedule_delete():
    """先自己删一遍，删不掉的交给延时批处理（本进程占着 pythonw.exe / dll，自己删不干净）。"""
    left = 0
    try:
        shutil.rmtree(ROOT, ignore_errors=True)
        left = len(os.listdir(ROOT)) if os.path.isdir(ROOT) else 0
        log("直接删除后剩余项: %d" % left)
    except Exception as exc:
        log("直接删除失败: %r" % (exc,))

    bat = os.path.join(tempfile.gettempdir(), "cqt_uninstall_%d.bat" % int(time.time()))
    try:
        with open(bat, "w", encoding="mbcs", errors="replace") as f:
            f.write("@echo off\r\n")
            f.write("ping -n 3 127.0.0.1 >nul\r\n")
            for _ in range(12):                      # 最多重试 12 次，等进程彻底退出
                f.write('rmdir /s /q "%s" 2>nul\r\n' % ROOT)
                f.write("if not exist \"%s\" goto done\r\n" % ROOT)
                f.write("ping -n 2 127.0.0.1 >nul\r\n")
            f.write(":done\r\n")
            f.write('del /f /q "%s"\r\n' % bat)
        subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("已安排延时删除目录: " + ROOT)
    except Exception as exc:
        log("安排删除失败: %r" % (exc,))


def main():
    log("==== uninstall 启动 admin=%s quiet=%s root=%s ====" % (is_admin(), QUIET, ROOT))

    if not is_admin():
        if os.environ.get("CQT_NOELEVATE") == "1":
            log("CQT_NOELEVATE=1，跳过提权直接继续")
        elif elevate():
            log("已请求提权，当前实例退出")
            return
        else:
            log("未能提权，继续尝试（可能因权限删不干净）")

    if not QUIET:
        if msg("确定要卸载「%s」吗？\n\n卸载会结束正在运行的软件，并删除程序文件。\n"
               "名单、叫号记录和配对密码保存在 data 文件夹里，稍后会问你是否保留。"
               % APP, MB_YESNO | MB_ICONQUESTION) != 6:
            return

    keep_data = False
    if not QUIET:
        keep_data = msg("是否保留学生名单、叫号记录和配对密码？\n\n"
                        "选「是」= 备份到「文档\\%s数据」（重装后可拷回来）\n"
                        "选「否」= 连同数据一起删除" % APP, MB_YESNO | MB_ICONQUESTION) == 6
    else:
        keep_data = True     # 静默卸载默认保留数据，避免误删

    kill_app()
    time.sleep(0.5)
    remove_autostart()
    remove_shortcuts()
    remove_registry()

    data_dir = os.path.join(ROOT, "data")
    backup = os.path.join(os.path.expanduser("~"), "Documents", APP + "数据")
    if keep_data and os.path.isdir(data_dir):
        try:
            if os.path.isdir(backup):
                shutil.rmtree(backup, ignore_errors=True)
            shutil.copytree(data_dir, backup)
            log("数据已备份到 " + backup)
        except Exception as exc:
            log("数据备份失败: %r" % (exc,))
            backup = ""

    schedule_delete()
    log("uninstall 结束")

    if not QUIET:
        extra = ("\n\n数据已备份到：\n" + backup) if (keep_data and backup) else ""
        msg("「%s」已卸载。%s\n\n（个别文件若提示被占用，稍后会自动删除；日志：%s）"
            % (APP, extra, LOG), MB_ICONINFORMATION)


if __name__ == "__main__":
    main()
