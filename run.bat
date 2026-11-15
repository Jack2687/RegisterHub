@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AutoRegister Community Runner
echo [*] Launching AutoRegister Engine...
python main.py %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [-] Execution paused due to an error.
    pause
)
