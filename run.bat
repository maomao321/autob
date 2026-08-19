@echo off
setlocal
cd /d "%~dp0"

if exist "dist\windows\AutoQuant.exe" (
    start "" "dist\windows\AutoQuant.exe"
    exit /b 0
)

if exist "dist\AutoQuant.exe" (
    start "" "dist\AutoQuant.exe"
    exit /b 0
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m autoquant
    exit /b %errorlevel%
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m autoquant
    if not errorlevel 1 exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    python -m autoquant
    if not errorlevel 1 exit /b 0
)

echo [AutoQuant] AutoQuant.exe and Python 3 were not found.
echo Run packaging\build_exe.ps1 first, or install Python 3.10 or newer.
pause
exit /b 1
