@echo off
REM 센터 PC(Windows)에서 두 번 눌러 실행. Python 3.10 이상이 깔려 있어야 한다.
cd /d %~dp0
start "" http://127.0.0.1:8765
python app.py
pause
