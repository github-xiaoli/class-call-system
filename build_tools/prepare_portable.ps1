$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path $PSScriptRoot -Parent
$Build = Join-Path $Root 'build'
$Cache = Join-Path $Build 'cache'
$PortableRoot = Join-Path $Build 'portable'
$AppDir = Join-Path $PortableRoot 'ClassCaller'
$PyDir = Join-Path $AppDir 'python'

New-Item -ItemType Directory -Force -Path $Cache, $AppDir | Out-Null

$embedUrl = 'https://registry.npmmirror.com/-/binary/python/3.13.11/python-3.13.11-embed-amd64.zip'
$embedZip = Join-Path $Cache 'python-3.13.11-embed-amd64.zip'
if (!(Test-Path $embedZip)) {
    Write-Output 'Downloading Python embeddable...'
    Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -UseBasicParsing -TimeoutSec 180
}

if (!(Test-Path (Join-Path $PyDir 'python.exe'))) {
    Write-Output 'Extracting Python...'
    New-Item -ItemType Directory -Force -Path $PyDir | Out-Null
    Expand-Archive -Path $embedZip -DestinationPath $PyDir -Force
}

$pthFile = Get-ChildItem $PyDir -Filter '*._pth' | Select-Object -First 1
if (-not $pthFile) { throw '._pth not found' }
$content = Get-Content $pthFile.FullName -Raw
if ($content -match '(?m)^#\s*import site') {
    $content = $content -replace '(?m)^#\s*import site', 'import site'
    Set-Content -Path $pthFile.FullName -Value $content -Encoding ASCII -NoNewline
    Write-Output 'Patched _pth to enable site-packages'
} else {
    Write-Output ('_pth already patched or unexpected: ' + $content)
}

$getpip = Join-Path $Cache 'get-pip.py'
if (!(Test-Path $getpip)) {
    Write-Output 'Downloading get-pip.py...'
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip -UseBasicParsing -TimeoutSec 180
}

Write-Output 'Installing pip into embeddable Python...'
& (Join-Path $PyDir 'python.exe') $getpip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw 'get-pip failed' }

Write-Output 'Installing flask + zeroconf...'
& (Join-Path $PyDir 'python.exe') -m pip install --no-warn-script-location -i https://pypi.tuna.tsinghua.edu.cn/simple flask zeroconf
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

Write-Output 'Verifying...'
& (Join-Path $PyDir 'python.exe') -c "import flask, zeroconf; print('flask', flask.__version__); print('zeroconf OK')"
if ($LASTEXITCODE -ne 0) { throw 'verify failed' }

Write-Output 'PORTABLE PYTHON READY'
