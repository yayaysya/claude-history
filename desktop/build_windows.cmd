@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv
    if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -X utf8 build_windows.py
if errorlevel 1 goto failed
echo 构建成功，程序位于 desktop\dist\SessionHistory.exe
exit /b
:failed
echo 构建失败，请检查上方错误。
pause
