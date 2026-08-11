@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run install_company.bat first.
  pause
  exit /b 1
)

set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m rep_patch
if errorlevel 1 pause
