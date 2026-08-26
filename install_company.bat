@echo off
rem ==============================================================================
rem Myao Patch Bridge Installer (install_company.bat)
rem 仕様:
rem 1. Python 3.10+ の存在を確認
rem 2. .venv が作成できれば使用、できなければシステムの Python 環境に直接インストール
rem 3. requirements.lock のパッケージを検証付きでインストール
rem ==============================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [Error] Python 3.10+ was not found.
  echo Please install Python and add it to PATH.
  pause
  exit /b 1
)

set "PYTHON_EXE=python"
if not exist ".venv\Scripts\python.exe" (
  echo Trying to create virtualenv .venv...
  python -m venv .venv 2>nul
  if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
  ) else (
    echo [.venv skipped] Installing directly into system Python environment...
  )
) else (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
)

echo Installing dependencies...
"%PYTHON_EXE%" -m pip install --require-hashes -r requirements.lock
if errorlevel 1 goto :error

echo.
echo [OK] Installation completed.
echo You can now run start_patch_app.bat
pause
exit /b 0

:error
echo.
echo [Error] Installation failed. Check the messages above.
pause
exit /b 1


