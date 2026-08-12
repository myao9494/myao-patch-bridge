@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run install_company.bat first.
  pause
  exit /b 1
)

set "PORT=17345"
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  echo Stopping the existing process on port %PORT% (PID %%P)...
  taskkill /PID %%P /T /F >nul 2>&1
)

set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m rep_patch
if errorlevel 1 pause
