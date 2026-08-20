@echo off
setlocal
cd /d "%~dp0"

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

echo [AutoQuant] Python 3 was not found.
echo Install Python 3.10 or newer and run: py -m pip install -e .
pause
exit /b 1
