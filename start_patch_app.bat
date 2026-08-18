@echo off
rem ==============================================================================
rem Myao Patch Bridge Launcher (start_patch_app.bat)
rem 仕様:
rem 1. .venv があれば優先使用、なければシステムの Python を使用
rem 2. ポート 17345 の重複プロセスを停止してポートを解放
rem 3. PYTHONPATH に src を設定して rep_patch を起動
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


