@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or later was not found.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install --require-hashes -r requirements.lock
if errorlevel 1 goto :error

echo.
echo Installation completed.
pause
exit /b 0

:error
echo.
echo Installation failed. Review the messages above.
pause
exit /b 1
