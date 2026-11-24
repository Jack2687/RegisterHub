@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RegisterHub Web Workbench (:18886)
echo [*] Starting RegisterHub Web Workbench at http://127.0.0.1:18886 ...
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul && start http://127.0.0.1:18886"
python main.py --web --port 18886
pause
