@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv
    if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -c "import zstandard, markdown_it, pygments, pystray, PIL" >nul 2>&1
if errorlevel 1 (
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -c "import json,urllib.request,webbrowser; data=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/summary',timeout=2)); assert 'sessions' in data and 'status' in data; webbrowser.open('http://127.0.0.1:8765')" >nul 2>&1
if not errorlevel 1 exit /b
.venv\Scripts\python.exe -X utf8 app.py --open --tray
if errorlevel 1 goto failed
exit /b
:failed
echo 启动失败，请检查上方错误。需要 Python 3.11 或更新版本。
pause
