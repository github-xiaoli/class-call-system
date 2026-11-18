# -*- coding: utf-8 -*-
"""把 v2.0.19 发布为正式版：改 release 信息（标题/正文/取消预发布）+ 上传安装包与说明书。

用法：python build\\make_github_release.py
"""
import hashlib
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = "github-xiaoli/class-call-system"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = open(r"C:\Users\李语晨\Desktop\github_api_key.txt", encoding="utf-8").read().strip()
VER = "2.0.19"
TAG = "v" + VER

EXE = os.path.join(ROOT, "build", "out", "班级叫号器_安装包_v%s.exe" % VER)
PDF = os.path.join(ROOT, "使用说明-图文版.pdf")
INFO = os.path.join(ROOT, "release-info.json")

# GitHub 会把资源名里的非 ASCII 字符吃掉，所以资源名用英文
ASSET_EXE = "ClassCaller-Setup-v%s.exe" % VER
ASSET_PDF = "ClassCaller-Manual-v%s.pdf" % VER


def api(method, path, payload=None, base="https://api.github.com"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "cqt-release",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return json.loads(body) if body else {}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_asset(release_id, path, name):
    url = "https://uploads.github.com/repos/%s/releases/%d/assets?name=%s" % (
        REPO, release_id, urllib.parse.quote(name))
    raw = open(path, "rb").read()
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "cqt-release",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(raw)),
    })
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read())


def body_md():
    exe_sha = sha256(EXE)
    pdf_sha = sha256(PDF)
    size = os.path.getsize(EXE) / 1048576.0
    pdfmb = os.path.getsize(PDF) / 1048576.0
    base = "https://github.com/%s/releases/download/%s/" % (REPO, TAG)
    return """## 班级叫号器 2.0.19（正式版）

Windows 10 / 11 课堂随机点名、叫号工具。局域网网页操作、TTS 语音播报、手机扫码配对、远程监控与远控、OTA 更新，
开源免费（GNU GPL-3.0-or-later）。

### 下载

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| [%(exe_name)s](%(exe_url)s) | %(size).1f MB | **推荐**。向导式安装，装到 `C:\\Program Files\\班级叫号器`，桌面/开始菜单有快捷方式，控制面板可卸载 |
| [%(pdf_name)s](%(pdf_url)s) | %(pdfmb).1f MB | 图文说明书（含安装、叫号、导入名单、监控、远控、更新、卸载） |
| [classcaller-%(ver)s.zip](%(zip_url)s) | 约 0.2 MB | OTA 增量代码包（给已装好的软件自动更新用，普通用户不用下） |

### 安装步骤

1. 双击 `班级叫号器_安装包_v%(ver)s.exe`，弹出 UAC 时点「是」。
2. 向导 5 步：**欢迎 → 许可协议 → 安装位置 → 安装 → 完成**。
   许可协议页是 GNU GPL v3 的中文全文，勾选「我已阅读并同意上述许可协议」才能继续。
3. 默认装到 `C:\\Program Files\\班级叫号器`（可改），约 240 MB，装完勾选「立即启动」即可。
4. 第一次启动会自动打开设置向导：同意协议 → 设置主机名 → 设置配对密码 → 扫码配对手机 → 导入名单。
5. 卸载：控制面板 → 卸载程序 → 班级叫号器；卸载前会问是否保留名单与记录（保留则备份到「文档\\班级叫号器数据」）。

安装包/绿色包都自带 Python 3.13 运行环境，目标机不需要装 Python；重复运行安装包即为升级，`data/` 会原样保留。

### 本版更新

- 新增：Windows 安装包改为向导式安装（NSIS）：欢迎页、GPLv3 中文全文许可页、可选安装目录、完成页可直接启动；
  自动创建桌面与开始菜单快捷方式，注册「控制面板 → 卸载程序」入口，卸载时可选保留名单与记录。
- 新增：安装前自动关闭正在运行的旧版本；重复安装走升级流程并保留 `data/`。
- 新增：首次使用设置向导（7 步 · 卡通场景 · 烟花动画 · GPL 协议阅读门禁 · 扫码配对手机）。
- 新增：GPLv3 中文全文译文与中文参考译文。
- 修复：检查更新偶尔显示"已是最新"（CDN 有旧缓存，改为取各线路里版本号最高的一份）。
- 更新：点「检查更新」时按钮锁定并显示「⏳ 检查中…」，避免连点重复检查。
- 修复：托盘图标点不出菜单（pywin32 菜单加分隔线报 TypeError）。
- 修复：第二次弹出的右下角气泡点 ✕ 关不掉（消息分发改为按窗口句柄）。
- 更新：检查更新 4 个地址并发请求；下载 31 条 CDN 并发测速，4 秒内选最快线路。
- 更新：下载/解压改为右下角可关闭气泡提示，整页锁定只显示进度，服务端拒绝重复下载。
- 更新：智能环境更新——代码包只发增量，依赖校验通过就不下载运行环境。
- 监控：摄像头名称改为 DirectShow 真实顺序；视频改为"回执驱动"推流，久看不卡。
- 文件：下载改为表单提交（兼容手机浏览器），上传显示进度条。
- 远控：电脑端只隐藏主控端鼠标，被控端正常显示；手机端支持多指触屏。
- 图标：统一为新的叫号器图标（白色背景），含网页图标。
- 许可证：GNU GPL v3（或更高版本），`LICENSE` 为官方原文，版权与声明见 `NOTICE`。

### 校验值（SHA-256）

```
%(exe_name)s
%(exe_sha)s

%(pdf_name)s
%(pdf_sha)s
```

### 许可证

本项目以 **GNU 通用公共许可证第 3 版，或（由你选择）任何更高版本（GPL-3.0-or-later）** 发布。
许可证全文见仓库根目录 [`LICENSE`](https://github.com/%(repo)s/blob/main/LICENSE)，
版权与「或更高版本」声明见 [`NOTICE`](https://github.com/%(repo)s/blob/main/NOTICE)，
中文译文见 [`LICENSE-中文全文译文.txt`](https://github.com/%(repo)s/blob/main/LICENSE-%%E4%%B8%%AD%%E6%%96%%87%%E5%%85%%A8%%E6%%96%%87%%E8%%AF%%91%%E6%%96%%87.txt)。
Copyright (C) 2026 xiaoli
""" % {
        "ver": VER, "size": size, "pdfmb": pdfmb,
        "exe_url": base + ASSET_EXE,
        "pdf_url": base + ASSET_PDF,
        "zip_url": base + "classcaller-%s.zip" % VER,
        "exe_name": ASSET_EXE, "pdf_name": ASSET_PDF,
        "exe_sha": exe_sha, "pdf_sha": pdf_sha, "repo": REPO,
    }


def main():
    for p in (EXE, PDF, INFO):
        if not os.path.isfile(p):
            raise SystemExit("缺少文件：" + p)

    rel = api("GET", "/repos/%s/releases/tags/%s" % (REPO, TAG))
    rid = rel["id"]
    print("release id", rid, "assets", [a["name"] for a in rel["assets"]])

    rel = api("PATCH", "/repos/%s/releases/%d" % (REPO, rid), {
        "name": "班级叫号器 %s（正式版）" % VER,
        "body": body_md(),
        "prerelease": False,
        "make_latest": True,
    })
    print("已更新为正式版：", rel["name"], "| prerelease =", rel["prerelease"])

    wanted = [
        (EXE, ASSET_EXE),
        (PDF, ASSET_PDF),
        (INFO, "release-info.json"),
    ]
    existing = {a["name"]: a for a in rel["assets"]}
    keep = {ASSET_EXE, ASSET_PDF, "release-info.json", "classcaller-%s.zip" % VER}
    for name, a in list(existing.items()):
        if name not in keep:
            api("DELETE", "/repos/%s/releases/assets/%d" % (REPO, a["id"]))
            print("清理多余资源", name)
            existing.pop(name)
    for path, name in wanted:
        if name in existing:
            api("DELETE", "/repos/%s/releases/assets/%d" % (REPO, existing[name]["id"]))
            print("删除旧资源", name)
        t0 = time.time()
        a = upload_asset(rid, path, name)
        print("上传 %-34s %8.1f MB  %.0fs" % (name, a["size"] / 1048576.0, time.time() - t0))

    rel = api("GET", "/repos/%s/releases/%d" % (REPO, rid))
    print("\n最终资源列表：")
    for a in rel["assets"]:
        print("  %-36s %8.1f MB  %s" % (a["name"], a["size"] / 1048576.0, a["state"]))
    print("release url:", rel["html_url"])


main()
