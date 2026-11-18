# -*- coding: utf-8 -*-
"""打包 Windows 安装程序（NSIS 3 编译成单个 exe）。

做法：程序文件先用 7-Zip 分卷压缩成 payload.7z.001…（每卷 8MB），
NSIS 只负责界面、快捷方式、卸载项，安装时用内置 7zr.exe 解压。

用法：
    python build\\make_installer.py 2.0.19          # 正式安装包（装到 Program Files，需要管理员）
    python build\\make_installer.py 2.0.19 --test    # 测试包（装到当前用户目录，不提权）

产物：build\\out\\班级叫号器_安装包_v2.0.19.exe
"""
import datetime
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"C:\Users\李语晨\Desktop\班级叫号器"
SRC_CODE = os.path.join(ROOT, "2.0.0")
SRC_PY = os.path.join(ROOT, "build", "zip", "班级叫号器", "python")
INST = os.path.join(ROOT, "build", "installer")
PAYLOAD = os.path.join(INST, "payload")
OUT = os.path.join(ROOT, "build", "out")
OUT_TEST = os.path.join(ROOT, "build", "out_test")
NSIS_DIR = os.path.join(ROOT, "build", "tools", "nsis")
MAKENSIS = os.path.join(NSIS_DIR, "Bin", "makensis.exe")
SEVENZ = r"C:\Program Files\7-Zip\7z.exe"
SEVENZR_SRC = os.path.join(ROOT, "build", "tools", "7zr.exe")
VOL_SUFFIX = "8m"            # 每卷 8MB（32 位 makensis 对单个嵌入文件的大小很敏感）

TEST = "--test" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
VER = ARGS[0] if ARGS else json.load(
    io.open(os.path.join(ROOT, "version.json"), encoding="utf-8").read())["version"]

EXE = os.path.join(OUT_TEST if TEST else OUT,
                   "班级叫号器_安装包_v%s%s.exe" % (VER, "_测试" if TEST else ""))


def log(m):
    print(m, flush=True)


def copy_tree(src, dst, ignore_pyc=True):
    def ig(d, names):
        return [n for n in names if ignore_pyc and (n == "__pycache__" or n.endswith(".pyc"))]
    shutil.copytree(src, dst, ignore=ig)


def write_text(path, text, encoding="utf-8", newline="\r\n"):
    with io.open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(text)


def stage_payload():
    if os.path.isdir(PAYLOAD):
        shutil.rmtree(PAYLOAD, ignore_errors=True)
    os.makedirs(PAYLOAD)

    log("1/6 复制程序代码 -> payload\\%s" % VER)
    copy_tree(SRC_CODE, os.path.join(PAYLOAD, VER))

    log("2/6 复制 Python 运行环境（约 260MB，稍等）…")
    copy_tree(SRC_PY, os.path.join(PAYLOAD, "python"))

    log("3/6 写启动/卸载脚本与许可证")
    shutil.copy2(os.path.join(INST, "payload_start.py"), os.path.join(PAYLOAD, "start.py"))
    shutil.copy2(os.path.join(INST, "payload_uninstall.py"), os.path.join(PAYLOAD, "uninstall.py"))

    vj = json.loads(io.open(os.path.join(ROOT, "version.json"), encoding="utf-8").read())
    vj["version"] = VER
    write_text(os.path.join(PAYLOAD, "version.json"),
               json.dumps(vj, ensure_ascii=False, indent=2), newline="\n")

    for name in ("start.bat", "启动-隐藏窗口.vbs", "示例名条.csv", "使用说明.txt", "README.md",
                 "LICENSE", "NOTICE", "LICENSE-中文全文译文.txt", "LICENSE-中文参考译文.txt"):
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(PAYLOAD, name))

    icon = os.path.join(ROOT, "data", "icon.ico")
    os.makedirs(os.path.join(PAYLOAD, "data"), exist_ok=True)
    if os.path.isfile(icon):
        shutil.copy2(icon, os.path.join(PAYLOAD, "data", "icon.ico"))
    else:
        log("  WARN 缺少 data\\icon.ico，快捷方式将使用默认图标")

    # 安装向导里显示的许可证：GPL 中文全文（UTF-8 BOM + CRLF，NSIS 才能正确显示中文）
    lic_zh = os.path.join(INST, "license_zh.txt")
    body = io.open(os.path.join(ROOT, "LICENSE-中文全文译文.txt"),
                   encoding="utf-8").read().replace("\r\n", "\n").replace("\n", "\r\n")
    write_text(lic_zh, body, encoding="utf-8-sig")
    return lic_zh


def make_archive():
    """把 payload 压成一个 7z（后面直接追加到安装包 exe 末尾）。"""
    log("4/6 7-Zip 压缩程序文件（多线程，比 makensis 自带的快很多）…")
    for old in glob.glob(os.path.join(INST, "payload.7z*")):
        os.remove(old)
    arc = os.path.join(INST, "payload.7z")
    t0 = time.time()
    # 在 payload 目录里打包，压缩包里就是 start.py / python\… / <版本号>\…（不带 payload\ 这层）
    r = subprocess.run([SEVENZ, "a", "-t7z", "-m0=lzma2", "-mx=9", "-ms=on", "-mmt=on",
                        arc, "*", "-r", "-y"],
                       cwd=PAYLOAD, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    log("  压缩耗时 %.0fs，退出码 %s" % (time.time() - t0, r.returncode))
    if r.returncode != 0:
        log((r.stdout or "")[-1500:])
        raise SystemExit("7-Zip 压缩失败")
    log("  压缩后 %.1f MB" % (os.path.getsize(arc) / 1048576.0))
    shutil.copy2(SEVENZR_SRC, os.path.join(INST, "7zr.exe"))
    return arc


def main():
    if not os.path.isfile(MAKENSIS):
        raise SystemExit("找不到 makensis：%s" % MAKENSIS)
    if not os.path.isfile(SEVENZR_SRC):
        raise SystemExit("找不到 7zr.exe：%s" % SEVENZR_SRC)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(OUT_TEST, exist_ok=True)

    reuse = "--reuse" in sys.argv and os.path.isdir(os.path.join(PAYLOAD, VER))
    if reuse:
        log("1~3/6 复用已暂存的 payload\\%s" % VER)
        lic_zh = os.path.join(INST, "license_zh.txt")
        if not os.path.isfile(lic_zh):
            body = io.open(os.path.join(ROOT, "LICENSE-中文全文译文.txt"),
                           encoding="utf-8").read().replace("\r\n", "\n").replace("\n", "\r\n")
            write_text(lic_zh, body, encoding="utf-8-sig")
    else:
        lic_zh = stage_payload()
    arc = make_archive()

    log("5/6 编译安装程序（NSIS）…")
    vi = [int(x) for x in VER.split(".")][:3]
    while len(vi) < 3:
        vi.append(0)
    defines = """\
; 由 build\\make_installer.py 自动生成，不要手改
%s!define APP_VER      "%s"
!define VI_VER       "%d.%d.%d.0"
!define INSTALL_DATE "%s"
!define PAYLOAD      "%s"
!define OUTPATH      "%s"
!define LIC_ZH       "%s"
!define LIC_EN       "%s"
!define ICON         "%s"
!define SEVENZR      "%s"
""" % ("" if TEST else "RequestExecutionLevel admin\n",
       VER, vi[0], vi[1], vi[2], datetime.date.today().strftime("%Y%m%d"),
       PAYLOAD, EXE, lic_zh, os.path.join(PAYLOAD, "LICENSE"),
       os.path.join(PAYLOAD, "data", "icon.ico"), os.path.join(INST, "7zr.exe"))
    write_text(os.path.join(INST, "build_defs.nsh"), defines, newline="\n")

    cmd = [MAKENSIS, "/V2", "/INPUTCHARSET", "UTF8"]
    if TEST:
        cmd.append("/DTESTBUILD")
    cmd.append(os.path.join(INST, "cqt.nsi"))
    env = dict(os.environ)
    env["NSISDIR"] = NSIS_DIR
    if os.path.isfile(EXE):
        os.remove(EXE)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=INST, capture_output=True, env=env)
    out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    tail = "\n".join(out.strip().splitlines()[-25:])
    log("  makensis 耗时 %.0fs，退出码 %s" % (time.time() - t0, r.returncode))
    if tail:
        log(tail)
    if r.returncode != 0 or not os.path.isfile(EXE):
        raise SystemExit("编译失败")

    # 把 7z 数据直接追加到 exe 末尾（安装时 7zr x 安装包.exe 就地解压）
    log("6/6 追加程序数据到安装包…")
    with open(EXE, "ab") as out, open(arc, "rb") as src:
        shutil.copyfileobj(src, out, 1024 * 1024)

    log("完成：%s（%.1f MB）" % (EXE, os.path.getsize(EXE) / 1048576.0))


main()
