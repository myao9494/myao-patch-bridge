@echo off
rem ==============================================================================
rem Myao Patch Bridge Launcher (start_patch_app.bat)
rem Keep this file ASCII-only: cmd.exe may misparse multi-byte text under the
rem Japanese code page and skip or break the following lines.
rem 1. Use .venv\Scripts\python.exe when it exists, otherwise the system python.
rem 2. Stop any process already listening on port 17345.
rem 3. Set PYTHONPATH to src and start rep_patch, passing all arguments through.
rem ==============================================================================
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
)
if "%PYTHON_EXE%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=python"
  )
)

if "%PYTHON_EXE%"=="" (
  echo [Error] Python was not found. Please install Python 3.10+.
  pause
  exit /b 1
)

set "PORT=17345"
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  taskkill /PID %%P /T /F >nul 2>&1
)

set "PYTHONPATH=%~dp0src"
"%PYTHON_EXE%" -m rep_patch %*
if errorlevel 1 (
  echo.
  echo [Error] Application failed to start.
  pause
)


