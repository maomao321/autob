param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$buildEnvironment = Join-Path $projectRoot ".packaging\build-venv-windows"
$buildPython = Join-Path $buildEnvironment "Scripts\python.exe"
$distPath = Join-Path $projectRoot "dist\windows"
$workPath = Join-Path $projectRoot "build\windows"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$CommandArguments = @()
    )

    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit code $LASTEXITCODE): $Executable $($CommandArguments -join ' ')"
    }
}

function New-BuildEnvironment {
    if ($PythonExe) {
        Invoke-Checked -Executable $PythonExe -CommandArguments @("-m", "venv", $buildEnvironment)
        return
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 --version *> $null
        if ($LASTEXITCODE -eq 0) {
            Invoke-Checked -Executable "py" -CommandArguments @("-3", "-m", "venv", $buildEnvironment)
            return
        }
    }

    foreach ($candidate in @("python", "python3")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            Invoke-Checked -Executable $candidate -CommandArguments @("-m", "venv", $buildEnvironment)
            return
        }
    }

    throw "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/windows/ and retry."
}

Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath $buildPython)) {
    Write-Host "Creating an isolated Windows build environment..."
    New-BuildEnvironment
}

Invoke-Checked -Executable $buildPython -CommandArguments @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked -Executable $buildPython -CommandArguments @("-m", "pip", "install", "-e", ".", "pyinstaller>=6.10,<7")
Invoke-Checked -Executable $buildPython -CommandArguments @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $distPath, "--workpath", $workPath, "AutoQuant.spec")

Write-Host "Build complete: $distPath\AutoQuant.exe"
