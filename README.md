# 班级叫号器

局域网班级叫号系统：网页叫号 + TTS 语音播报 + mDNS 局域网访问 + 远程监控对讲 + 远程控制 + 文件传输。

## 下载与安装

- **安装版（推荐）**：[Releases](https://github.com/github-xiaoli/class-call-system/releases/latest) 里下载
  `ClassCaller-Setup-v2.0.20.exe`（约 60 MB），双击运行。安装向导 5 步：
  欢迎 → 许可协议（GNU GPL v3 中文全文）→ 安装位置（默认 `C:\Program Files\班级叫号器`）→ 安装 → 完成。
  装完桌面和开始菜单都有快捷方式，之后可在「控制面板 → 卸载程序」里卸载（卸载时可选保留 `data` 数据）。
- **图文说明书**：同一 release 里的 `ClassCaller-Manual-v2.0.20.pdf`，仓库内也有
  [`使用说明-图文版.pdf`](使用说明-图文版.pdf) / [`.html`](使用说明-图文版.html)，纯文本版见 [`使用说明.txt`](使用说明.txt)。
- **绿色版**：把 `classcaller-2.0.20.zip` 解压到已有安装目录即可（它是自动更新用的增量代码包），
  全新的手工部署请参看 `build_tools/`。

安装包自带 Python 3.13 运行环境，目标机不需要另外装 Python。安装包支持往同一目录重复安装
（升级），`data/` 里的名单、叫号记录、配对密码会原样保留。

## 目录结构（OTA 友好）

- `version.json`：当前版本号与启动信息；OTA 更新时替换新版本目录并更新 version。
- `<版本号>/`：该版本的代码（不含程序数据）。当前版本：`2.0.0/`。
- `data/`：程序数据（配置、名条、密码、证书、上传文件等），**OTA 更新时保留不删除**。
- `start.bat`：启动器，读取 `version.json` 选择版本目录启动。
- `启动-隐藏窗口.vbs`：隐藏窗口启动（用于开机自启，不弹黑窗）。
- `build_tools/prepare_portable.ps1`：准备便携版 Python 运行环境。

## 运行

双击 `start.bat`（或 `启动-隐藏窗口.vbs`）。本机访问 `http://127.0.0.1:8000`。

## 许可证

本项目以 **GNU 通用公共许可证第 3 版，或（由你选择）任何更高版本（GPL-3.0-or-later）** 发布。

- 你可以自由使用、修改、再分发，但分发时需保留同样的许可并提供源代码。
- 许可证全文见仓库根目录 [`LICENSE`](LICENSE)（自由软件基金会发布的官方原文，未做任何修改）。
- 版权与「或任何更高版本」声明见 [`NOTICE`](NOTICE)；SPDX 标识：`GPL-3.0-or-later`。
- 许可证官方地址：<https://www.gnu.org/licenses/gpl-3.0.html>
- Copyright (C) 2026 xiaoli

> LICENSE 保持 GPL 官方原文不变，是为了让 GitHub 等平台能自动识别为 `GPL-3.0`；
> 版权人和「或更高版本」的说明放在 NOTICE 里，两者配合才完整。
