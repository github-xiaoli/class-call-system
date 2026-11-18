# Windows 安装包构建说明

本目录是「班级叫号器」Windows 安装包的源代码。安装包是一个 NSIS 生成的单文件 exe（约 60 MB），
装到 `C:\Program Files\班级叫号器`，会创建桌面/开始菜单快捷方式，并在「控制面板 → 卸载程序」里注册卸载入口。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `make_installer.py` | 一键构建脚本：暂存 payload → 7-Zip 压缩 → makensis 编译 → 追加数据 |
| `cqt.nsi` | NSIS 安装脚本（向导页面、快捷方式、注册表卸载项、卸载逻辑） |
| `stopapp.ps1` | 安装/卸载前关掉正在运行的旧版本（只动安装目录里的 python 进程） |
| `payload_start.py` | 装到安装目录的 `start.py`：按 `version.json` 启动对应版本目录 |
| `payload_uninstall.py` | 绿色版用的 python 卸载脚本（安装版用 NSIS 自带的 uninstall.exe） |
| `test_installer_full.py` | 端到端自测：静默安装 → 校验文件/快捷方式/卸载项 → 静默卸载 → 校验清理 |

## 依赖

- [NSIS](https://nsis.sourceforge.io/) 3.x 的 `makensis.exe`（脚本只需 3.0 以上，无第三方插件）
- 7-Zip 的 `7z.exe`（分卷/压缩用）和 `7zr.exe`（会被打进安装包，用于安装时解压）
- Python 3.10+（跑构建脚本）

## 构建

```powershell
# 1) 准备可移植 python 运行环境（一次性，放在 build\zip\班级叫号器\python）
# 2) 暂存程序文件 + 压缩 + 编译 + 追加数据
python build\make_installer.py 2.0.19
# 产物：build\out\班级叫号器_安装包_v2.0.19.exe
```

`make_installer.py` 顶部的 `ROOT / NSIS_DIR / SEVENZ / SEVENZR_SRC` 是绝对路径，换机器时改这几行即可。

## 实现要点（踩过的坑）

1. **大文件不要交给 makensis 压**：32 位 makensis 在映射 >30 MB 的数据时会报
   `Internal compiler error #12345: error creating mmap`，因此程序文件用 7-Zip 压成 `payload.7z`，
   安装包编译完后把该 7z **追加到 exe 末尾**，安装时用内置 `7zr.exe x "$EXEPATH"` 就地解压。
   所以构建分两步：先 `makensis`（只打 0.6 MB 的 7zr + 界面资源），再追加数据。
2. **`RequestExecutionLevel` 不能写在 `!ifdef` 里**：NSIS 对条件块中的该指令也会生效，
   正式包的 `admin` 由 `make_installer.py` 写进生成的 `build_defs.nsh`，测试包则不写。
3. **不要用 `RequestExecutionLevel user`**：某些 NSIS stub 打了 user 会直接弹 `NSIS Error`。
4. **中文源文件**：`makensis` 用 `/INPUTCHARSET UTF8`，源文件存 UTF-8 无 BOM；
   许可证页的文本文件用 UTF-8 BOM + CRLF，中文才不会乱码。
5. **卸载**：先关进程 → 询问是否保留 `data`（选是则 xcopy 到「文档\班级叫号器数据」）→
   清计划任务与 Run 自启项 → 删快捷方式 → 删注册表卸载项 → `RMDir /r $INSTDIR`。
   静默卸载（`/S`）默认保留数据，不会误删名单。
6. **升级**：再次运行安装包即复用注册表里的 `InstallLocation`，`data/` 原样保留；
   安装前会提示"检测到已安装，将升级并保留数据"。

## 自测

```powershell
python installer\test_installer_full.py          # 装到 build\tools\e2e，跑完自动卸载
python installer\test_installer_full.py --keep   # 装完留着看界面
```
