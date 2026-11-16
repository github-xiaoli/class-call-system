@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem 读取 version.json 中的版本号（缺失时回退到 2.0.0）
set "VER=2.0.0"
if exist "%~dp0version.json" (
  for /f "usebackq delims=" %%V in (`powershell -NoProfile -NonInteractive -Command "(Get-Content -LiteralPath '%~dp0version.json' -Raw | ConvertFrom-Json).version"`) do set "VER=%%V"
)
if not defined VER set "VER=2.0.0"
for /f "delims=" %%V in ("%VER%") do set "VER=%%V"

set "APP=%~dp0%VER%\app.py"
if not exist "%APP%" set "APP=%~dp0app.py"

if exist "%~dp0python\pythonw.exe" (
  set "PY=%~dp0python\pythonw.exe"
) else if exist "%~dp0python\python.exe" (
  set "PY=%~dp0python\python.exe"
) else (
  where pythonw >nul 2>nul && (set "PY=pythonw") || (set "PY=python")
)

start "" "%PY%" "%APP%"
exit /b 0
