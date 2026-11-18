# -*- coding: utf-8 -*-
"""便携版/安装版的启动入口：读 version.json，运行对应版本目录里的 app.py。

这样就固定了一个"不管版本怎么升级都不变"的启动文件，
快捷方式和安装程序都指向它（OTA 更新后仍能正确启动新版本）。
"""
import json
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    version = ""
    try:
        with open(os.path.join(ROOT, "version.json"), "r", encoding="utf-8") as f:
            version = str(json.load(f).get("version", "") or "")
    except Exception:
        version = ""
    if not version:
        version = "2.0.0"
    code_dir = os.path.join(ROOT, version)
    if not os.path.isdir(code_dir):
        # 版本目录不存在时，退回到任意一个带 app.py 的目录
        for name in sorted(os.listdir(ROOT), reverse=True):
            p = os.path.join(ROOT, name)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, "app.py")):
                code_dir = p
                break
    app = os.path.join(code_dir, "app.py")
    if not os.path.isfile(app):
        raise SystemExit("找不到程序文件：%s" % app)

    os.chdir(ROOT)
    sys.path.insert(0, code_dir)
    sys.dont_write_bytecode = True
    runpy.run_path(app, run_name="__main__")


if __name__ == "__main__":
    main()
