# -*- coding: utf-8 -*-
# 结束正在运行的「班级叫号器」：只动安装目录里的 python 进程，
# 外加「路径里带班级叫号器」的 python 进程（比如之前装在别处的旧版本），不动其它 Python 程序。
param([string]$AppDir = "")

$ErrorActionPreference = "SilentlyContinue"

function Test-UnderDir([string]$path, [string]$dir) {
    if (-not $path -or -not $dir) { return $false }
    try {
        $p = [System.IO.Path]::GetFullPath($path)
        $d = [System.IO.Path]::GetFullPath($dir).TrimEnd('\') + '\'
        return $p.StartsWith($d, [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

$killed = 0
foreach ($name in @("python", "pythonw", "班级叫号器")) {
    foreach ($p in (Get-Process -Name $name -ErrorAction SilentlyContinue)) {
        $hit = $false
        try {
            $exe = $p.Path
            if ($exe -and (Test-UnderDir $exe $AppDir)) { $hit = $true }
            if (-not $hit -and $exe -and $exe.IndexOf("班级叫号器", [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hit = $true }
        } catch { }
        if (-not $hit) {
            # 取不到 Path（权限不足）时，用命令行判断
            try {
                $cl = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $p.Id) -ErrorAction SilentlyContinue).CommandLine
                if ($cl -and $AppDir -and $cl.IndexOf($AppDir, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hit = $true }
                if (-not $hit -and $cl -and $cl.IndexOf("班级叫号器", [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hit = $true }
            } catch { }
        }
        if ($hit) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; $killed++ } catch { }
        }
    }
}

Write-Output ("stopped=" + $killed)
if ($killed -gt 0) { Start-Sleep -Milliseconds 800 }
exit 0
