# 班级叫号器

局域网班级叫号系统：网页叫号 + TTS 语音播报 + mDNS 局域网访问 + 远程监控对讲 + 远程控制 + 文件传输。

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

本项目以 **GNU 通用公共许可证第 3 版（GPL-3.0-or-later）** 发布。

- 你可以自由使用、修改、再分发，但分发时需保留同样的许可并提供源代码。
- 完整许可证正文见仓库根目录 LICENSE，或 <https://www.gnu.org/licenses/gpl-3.0.html>。
- Copyright (C) 2026 xiaoli
