; ============================================================================
;  班级叫号器 —— Windows 安装程序（NSIS 3 / Modern UI 2）
;  - 装到 C:\Program Files\班级叫号器（需要管理员权限，UAC 提权一次）
;  - 桌面 + 开始菜单快捷方式，「控制面板 → 卸载程序」可卸载
;  - 卸载时询问是否保留 data（名单 / 记录 / 配对密码），保留则备份到「文档」
;  - 编译：makensis /INPUTCHARSET UTF8 cqt.nsi   （由 build\make_installer.py 调用）
; ============================================================================

Unicode true
!ifdef TESTBUILD
  SetCompressor zlib              ; 测试包用快压缩，缩短编译时间
!else
  SetCompressor lzma              ; 只压 7zr.exe 等零碎文件（大文件在追加的 7z 里）
  SetCompressorDictSize 32
!endif
; datablock 优化在打包几百 MB 时会让 makensis 报
; 「Internal compiler error #12345: error creating mmap」，所以默认关掉
!ifdef DATABLOCK_OPT
  SetDatablockOptimize on
!endif
CRCCheck on

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!include "build_defs.nsh"   ; 由 make_installer.py 生成：APP_VER PAYLOAD OUTPATH LIC_ZH LIC_EN ICON VI_VER INSTALL_DATE

; ---------------------------------------------------------------- 基本信息
!define APP_NAME      "班级叫号器"
!define APP_PUBLISHER "xiaoli"
!define APP_URL       "https://github.com/github-xiaoli/class-call-system"
!define APP_TASK      "班级叫号器"
!define UNINST_KEY    "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
!define RUN_KEY       "Software\Microsoft\Windows\CurrentVersion\Run"

Name "${APP_NAME} ${APP_VER}"
OutFile "${OUTPATH}"
BrandingText "${APP_NAME} ${APP_VER} · 开源免费 · GPL-3.0-or-later"
Icon "${ICON}"
UninstallIcon "${ICON}"
ShowInstDetails nevershow
ShowUninstDetails nevershow
XPStyle on

!ifdef TESTBUILD
  ; 仅自动化测试用：不提权、装到当前用户目录、写 HKCU
  ; 注意：RequestExecutionLevel 写在 build_defs.nsh 里（NSIS 对 !ifdef 里的
  ; RequestExecutionLevel 也会生效，所以正式包的 admin 不能写在脚本的条件分支里）
  InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
  !define REG_HIVE HKCU
  !define SHELLVAR current
  !define BASE_DIR "$LOCALAPPDATA\Programs"
!else
  InstallDir "$PROGRAMFILES\${APP_NAME}"
  !define REG_HIVE HKLM
  !define SHELLVAR all
  !define BASE_DIR "$PROGRAMFILES"
!endif

VIProductVersion "${VI_VER}"
VIAddVersionKey /LANG=2052 "ProductName"      "${APP_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription"  "${APP_NAME} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion"      "${APP_VER}"
VIAddVersionKey /LANG=2052 "ProductVersion"   "${APP_VER}"
VIAddVersionKey /LANG=2052 "CompanyName"      "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "LegalCopyright"   "GNU GPL-3.0-or-later"
VIAddVersionKey /LANG=2052 "Comments"         "开源免费软件，源码与许可证见安装目录"

; ---------------------------------------------------------------- 安装向导
!define MUI_ABORTWARNING
!define MUI_ICON   "${ICON}"
!define MUI_UNICON "${ICON}"

!define MUI_WELCOMEPAGE_TITLE "安装 ${APP_NAME} ${APP_VER}"
!define MUI_WELCOMEPAGE_TEXT  "${APP_NAME} 是一个开源免费的课堂随机点名 / 叫号工具，可以离线运行。$\r$\n$\r$\n· 装到 ${BASE_DIR}\${APP_NAME}（会弹出一次 UAC 提示，点「是」）$\r$\n· 装完桌面和开始菜单都有快捷方式 $\r$\n· 以后在「控制面板 → 卸载程序」里卸载 $\r$\n· data 文件夹（名单、记录、配对密码）升级时保留 $\r$\n$\r$\n点「下一步」继续。"
!insertmacro MUI_PAGE_WELCOME

!define MUI_LICENSEPAGE_CHECKBOX
!define MUI_LICENSEPAGE_CHECKBOX_TEXT "我已阅读并同意上述许可协议（GNU GPL-3.0-or-later）"
!define MUI_LICENSEPAGE_TEXT_TOP "许可协议：GNU General Public License v3.0 中文全文（这是一份法律性资料）"
!define MUI_LICENSEPAGE_TEXT_BOTTOM "英文原文与中文译文随程序一起安装：LICENSE、LICENSE-中文全文译文.txt、LICENSE-中文参考译文.txt"
!insertmacro MUI_PAGE_LICENSE "${LIC_ZH}"

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "安装完成"
!define MUI_FINISHPAGE_TEXT "${APP_NAME} ${APP_VER} 已经装好了。$\r$\n$\r$\n点「完成」会启动程序，第一次运行会自动打开设置向导（激活、绑定手机、导入名单）。$\r$\n$\r$\n也可以用桌面或开始菜单里的快捷方式启动。"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APP_NAME}（打开设置向导）"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\使用说明.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "打开使用说明"
!define MUI_FINISHPAGE_LINK "查看项目主页（源码 / 更新日志）"
!define MUI_FINISHPAGE_LINK_LOCATION "${APP_URL}"
!insertmacro MUI_PAGE_FINISH
!define MUI_FINISHPAGE_TITLE "卸载完成"
!define MUI_FINISHPAGE_TEXT "$(UN_FINISH)"
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------- 界面文字
LangString UPGRADE_TIP ${LANG_SIMPCHINESE} "检测到已经安装过「${APP_NAME}」。$\r$\n$\r$\n继续安装会完成升级：程序文件更新为新版本，data 文件夹里的名单、叫号记录和配对密码原样保留。$\r$\n$\r$\n继续吗？"
LangString UPGRADE_TIP ${LANG_ENGLISH}     "An existing installation was found. Setup will upgrade the program files and keep the data folder untouched. Continue?"
LangString UN_KEEP_DATA ${LANG_SIMPCHINESE} "卸载前是否保留数据？$\r$\n$\r$\n选「是」：把 data 文件夹备份到「文档\${APP_NAME}数据」，然后删除程序。$\r$\n选「否」：程序和学生名单、叫号记录、配对密码一起删除。"
LangString UN_KEEP_DATA ${LANG_ENGLISH}     "Keep your data before uninstalling?$\r$\n$\r$\nYes: back up the data folder to Documents, then remove the program.$\r$\nNo: delete the program together with rosters, records and pairing passwords."
LangString UN_FINISH    ${LANG_SIMPCHINESE} "「${APP_NAME}」已经卸载完成。$\r$\n$\r$\n程序文件、快捷方式和「控制面板」里的卸载项都已清理。"
LangString UN_FINISH    ${LANG_ENGLISH}     "${APP_NAME} has been removed from your computer."

; ---------------------------------------------------------------- 完成页启动
Function LaunchApp
  Exec '"$INSTDIR\python\pythonw.exe" "$INSTDIR\start.py"'
FunctionEnd

; ---------------------------------------------------------------- 初始化
Function .onInit
  LogSet on

  ${IfNot} ${AtLeastWin7}
    MessageBox MB_ICONSTOP "这个安装包需要 Windows 7 或更新的系统。"
    Abort
  ${EndIf}

  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}

  ; 只有用户没指定 /D= 安装目录时，才用「默认目录」逻辑（免得覆盖 /D 的值）
  StrCpy $R2 "${BASE_DIR}\${APP_NAME}"
  ${If} $INSTDIR == $R2
    ${If} ${RunningX64}
      StrCpy $INSTDIR "$PROGRAMFILES64\${APP_NAME}"
    ${EndIf}
    ; 已经装过：沿用原来的安装目录，用户升级时不用重新选路径
    ReadRegStr $0 ${REG_HIVE} "${UNINST_KEY}" "InstallLocation"
    ${If} $0 != ""
      StrCpy $INSTDIR $0
    ${EndIf}
  ${EndIf}

  ; 升级提示
  ${If} ${FileExists} "$INSTDIR\version.json"
    MessageBox MB_OKCANCEL|MB_ICONINFORMATION "$(UPGRADE_TIP)" /SD IDOK IDCANCEL upg_cancel
    Goto upg_ok
    upg_cancel:
      Abort
    upg_ok:
  ${EndIf}

FunctionEnd

Function .onInstSuccess
  FileOpen $0 "$TEMP\cqt_install.log" a
  FileWrite $0 "install ok ver=${APP_VER} dir=$INSTDIR$\r$\n"
  FileClose $0
FunctionEnd

; ---------------------------------------------------------------- 结束旧进程
Function StopApp
  InitPluginsDir
  File /oname=$PLUGINSDIR\stopapp.ps1 "stopapp.ps1"
  DetailPrint "关闭正在运行的旧版本…"
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\stopapp.ps1" "$INSTDIR"'
  Pop $0
  Sleep 500
FunctionEnd

Function un.StopApp
  InitPluginsDir
  File /oname=$PLUGINSDIR\stopapp.ps1 "stopapp.ps1"
  DetailPrint "关闭正在运行的程序…"
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\stopapp.ps1" "$INSTDIR"'
  Pop $0
  Sleep 500
FunctionEnd

; ---------------------------------------------------------------- 安装
Section "安装 ${APP_NAME}" SecMain
  SectionIn RO

  SetShellVarContext ${SHELLVAR}
  Call StopApp

  SetOutPath "$INSTDIR"
  SetOverwrite on

  ; 程序文件（约 60MB 的 7z 数据）是直接追加在安装包 exe 末尾的，
  ; 安装时用内置的 7zr.exe 就地解压自己（7zr x 安装包.exe）。
  ; 这样 makensis 只需要打包 0.6MB 的 7zr，编译又快又不会因大文件映射失败。
  SetCompress off
  File /oname=$PLUGINSDIR\7zr.exe "${SEVENZR}"
  SetCompress auto

  DetailPrint "正在解压程序文件（约 230 MB，通常 20~60 秒）…"
  nsExec::ExecToLog '"$PLUGINSDIR\7zr.exe" x "$EXEPATH" -o"$INSTDIR" -y -bso0 -bsp0'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "解压程序文件失败（代码 $0）。$\r$\n$\r$\n可能原因：磁盘空间不足、杀毒软件拦截，或者安装包被下载工具截断。$\r$\n请重新下载安装包后重试。"
    Abort
  ${EndIf}

  ; 装完校验关键文件，缺了就报错（不留半截安装）
  StrCpy $1 ""
  ${IfNot} ${FileExists} "$INSTDIR\python\pythonw.exe"
    StrCpy $1 "python\pythonw.exe"
  ${ElseIfNot} ${FileExists} "$INSTDIR\start.py"
    StrCpy $1 "start.py"
  ${ElseIfNot} ${FileExists} "$INSTDIR\version.json"
    StrCpy $1 "version.json"
  ${ElseIfNot} ${FileExists} "$INSTDIR\${APP_VER}\app.py"
    StrCpy $1 "${APP_VER}\app.py"
  ${EndIf}
  ${If} $1 != ""
    MessageBox MB_ICONSTOP "安装失败：缺少文件 $1$\r$\n$\r$\n请重新运行安装包；如果一直失败，把安装包换个文件夹（例如 D:\）再试。"
    Abort
  ${EndIf}

  ; 数据目录（已有内容保留，只补默认结构）
  CreateDirectory "$INSTDIR\data"
  CreateDirectory "$INSTDIR\data\update"

  ; 给普通用户写权限：OTA 更新、保存名单、写日志都要写这个目录
  DetailPrint "设置数据目录权限…"
  nsExec::ExecToLog 'icacls "$INSTDIR" /grant *S-1-5-32-545:(OI)(CI)M /T /C /Q'
  Pop $0

SectionEnd

Section "桌面快捷方式" SecDesktop
  SetShellVarContext ${SHELLVAR}
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\python\pythonw.exe" \
                 '"$INSTDIR\start.py"' "$INSTDIR\data\icon.ico" 0 SW_SHOWNORMAL "" "${APP_NAME}"
SectionEnd

Section "开始菜单快捷方式" SecStartMenu
  SetShellVarContext ${SHELLVAR}
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\python\pythonw.exe" \
                 '"$INSTDIR\start.py"' "$INSTDIR\data\icon.ico" 0 SW_SHOWNORMAL "" "${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\使用说明.lnk" "$INSTDIR\使用说明.txt"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Section -Registry
  ; 卸载程序本体
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 控制面板「卸载程序」条目
  ${GetSize} "$INSTDIR" "/S=0K" $1 $2 $3
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "DisplayName"          "${APP_NAME}"
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "DisplayVersion"       "${APP_VER}"
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "Publisher"            "${APP_PUBLISHER}"
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "InstallLocation"      "$INSTDIR"
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "DisplayIcon"          "$INSTDIR\data\icon.ico,0"
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "UninstallString"      '"$INSTDIR\uninstall.exe"'
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "InstallDate"          "${INSTALL_DATE}"
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "URLInfoAbout"         "${APP_URL}"
  WriteRegStr   ${REG_HIVE} "${UNINST_KEY}" "Comments"             "开源免费 · GNU GPL-3.0-or-later"
  WriteRegDWORD ${REG_HIVE} "${UNINST_KEY}" "EstimatedSize"        $1
  WriteRegDWORD ${REG_HIVE} "${UNINST_KEY}" "NoModify"             1
  WriteRegDWORD ${REG_HIVE} "${UNINST_KEY}" "NoRepair"             1
SectionEnd

; ---------------------------------------------------------------- 卸载
Function un.onInit
  ${If} ${RunningX64}
    SetRegView 64
  ${EndIf}
  SetShellVarContext ${SHELLVAR}
FunctionEnd

Section "Uninstall"
  SetShellVarContext ${SHELLVAR}
  Call un.StopApp

  ; 问一句是否保留数据（静默卸载默认「是」，不会误删）
  MessageBox MB_YESNO|MB_ICONQUESTION "$(UN_KEEP_DATA)" /SD IDYES IDNO del_data
  ${If} ${FileExists} "$INSTDIR\data\*.*"
    SetShellVarContext current
    CreateDirectory "$DOCUMENTS\${APP_NAME}数据"
    nsExec::ExecToLog 'cmd /c xcopy /e /i /y /q "$INSTDIR\data" "$DOCUMENTS\${APP_NAME}数据"'
    Pop $0
    DetailPrint "数据已备份到 $DOCUMENTS\${APP_NAME}数据"
  ${EndIf}
  Goto after_data
  del_data:
    DetailPrint "按选择删除数据…"
  after_data:
  SetShellVarContext ${SHELLVAR}

  ; 开机自启（计划任务 + 注册表 Run）
  nsExec::ExecToLog 'schtasks /Delete /TN "${APP_TASK}" /F'
  Pop $0
  DeleteRegValue HKCU "${RUN_KEY}" "${APP_TASK}"

  ; 快捷方式（所有用户 + 当前用户都清一遍）
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\使用说明.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  SetShellVarContext current
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  ; 卸载项
  DeleteRegKey ${REG_HIVE} "${UNINST_KEY}"
  DeleteRegKey HKCU "${UNINST_KEY}"

  ; 程序文件（含 python 运行环境、各版本目录、data）
  Delete "$INSTDIR\uninstall.exe"
  RMDir /r /REBOOTOK "$INSTDIR"
SectionEnd
