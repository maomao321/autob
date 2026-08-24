@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto ensure_dependencies

where py >nul 2>nul
if not errorlevel 1 (
    echo [AutoQuant] Creating project virtual environment...
    py -3 -m venv ".venv"
    if errorlevel 1 goto venv_error
    goto ensure_dependencies
)

where python >nul 2>nul
if not errorlevel 1 (
    echo [AutoQuant] Creating project virtual environment...
    python -m venv ".venv"
    if errorlevel 1 goto venv_error
    goto ensure_dependencies
)

echo [AutoQuant] Python 3 was not found.
echo Install Python 3.10 or newer, then run this script again.
pause
exit /b 1

:ensure_dependencies
".venv\Scripts\python.exe" -c "import autoquant_frontend, autoquant_shared, PySide6, openpyxl" >nul 2>nul
if not errorlevel 1 goto launch

echo [AutoQuant] Installing source dependencies into .venv...
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto dependency_error

:launch
".venv\Scripts\python.exe" -m autoquant_frontend
set "AUTOQUANT_EXIT_CODE=%errorlevel%"
if not "%AUTOQUANT_EXIT_CODE%"=="0" pause
exit /b %AUTOQUANT_EXIT_CODE%

:venv_error
echo [AutoQuant] Failed to create .venv. Verify that Python 3.10 or newer includes the venv module.
pause
exit /b 1

:dependency_error
echo [AutoQuant] Failed to install dependencies. Check the network and the pip error above.
pause
exit /b 1
