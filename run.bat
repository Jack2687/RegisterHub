@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AutoRegister Community Runner

if not "%1"=="" (
    python main.py %*
    goto END
)

:MENU
cls
echo ======================================================
echo           AutoRegister Community CLI Runner
echo ======================================================
echo.
echo   [1] 运行开箱 Demo 演示 (本地离线靶场)
echo   [2] 运行 Hugging Face 注册测试 (桌面有头窗口)
echo   [3] 运行系统环境诊断 (Doctor)
echo   [4] 输入目标网址直接探测 (--url)
echo   [0] 退出
echo.
echo ======================================================
set /p choice="请输入选项 [0-4]: "

if "%choice%"=="1" (
    echo.
    echo [*] 正在启动 Demo 演示...
    python main.py --run demo
    pause
    goto MENU
)
if "%choice%"=="2" (
    echo.
    echo [*] 正在启动 Hugging Face 注册适配器...
    python main.py --run huggingface
    pause
    goto MENU
)
if "%choice%"=="3" (
    echo.
    python main.py doctor
    pause
    goto MENU
)
if "%choice%"=="4" (
    echo.
    set /p target_url="请输入目标注册网址: "
    python main.py --url %target_url%
    pause
    goto MENU
)
if "%choice%"=="0" (
    exit /b 0
)

echo 无效选项，请重试。
timeout /t 2 >nul
goto MENU

:END
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [-] Execution paused due to an error.
    pause
)
