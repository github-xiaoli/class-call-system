@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Read version from version.json (UTF-8). Fallback to 2.0.0 on failure.
set "VER=2.0.0"
if not exist "%~dp0version.json" goto version_done

for /f "usebackq delims=" %%V in (`powershell -NoProfile -NonInteractive -Command "try { $v=(Get-Content -LiteralPath '%~dp0version.json' -Raw -Encoding UTF8 | ConvertFrom-Json).version; if($v){Write-Output $v}else{Write-Output '2.0.0'} } catch { Write-Output '2.0.0' }"`) do set "VER=%%V"

:version_done
if not defined VER set "VER=2.0.0"
for /f "delims=" %%V in ("%VER%") do set "VER=%%V"

set "APP=%~dp0%VER%\app.py"
if not exist "%APP%" set "APP=%~dp0app.py"
if not exist "%APP%" goto noapp

if exist "%~dp0python\pythonw.exe" (
  set "PY=%~dp0python\pythonw.exe"
) else if exist "%~dp0python\python.exe" (
  set "PY=%~dp0python\python.exe"
) else (
  where pythonw >nul 2>nul
  if not errorlevel 1 (set "PY=pythonw") else (set "PY=python")
)

start "" "%PY%" "%APP%"
exit /b 0

:noapp
echo [ERROR] app.py not found: %APP%
echo Please check version.json or extract all files first.
pause
exit /b 1
