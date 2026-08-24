@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv || exit /b 1
)

.venv\Scripts\python.exe -c "import autoquant_backend, autoquant_shared, websocket" >nul 2>nul
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -e . || exit /b 1
)

.venv\Scripts\python.exe -m autoquant_backend %*
