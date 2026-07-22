param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

& $PythonExe -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw "项目依赖安装失败" }

& $PythonExe -m pip install "pyinstaller>=6.10,<7"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败" }

& $PythonExe -m PyInstaller --noconfirm --clean AutoQuant.spec
if ($LASTEXITCODE -ne 0) { throw "EXE 构建失败" }

Write-Host "构建完成: $projectRoot\dist\AutoQuant.exe"

