@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RegisterHub Runner

if not "%1"=="" (
    python main.py %*
    goto END
)

:MENU
cls
echo ======================================================
echo           RegisterHub CLI Runner
echo ======================================================
echo.
echo   [1] 运行开箱 Demo 演示 (本地离线靶场)
echo   [2] 运行 QuanticData 滑块验证测试 (OpenCV 视觉与贝塞尔拟人拖拽)
echo   [3] 运行全球通用注册靶场测试 (AutomationExercise 实战)
echo   [4] 运行系统环境诊断 (Doctor)
echo   [5] 输入目标网址直接探测 (--url)
echo   [6] 一键生成新站适配器模板 (--scaffold)
echo   [7] 导出账号资产 (CSV / JSON / Bitwarden 密码本)
echo   [8] 运行自动化测试套件 (Pytest)
echo   [0] 退出
echo.
echo ======================================================
set /p choice="请输入选项 [0-8]: "

if "%choice%"=="1" (
    echo.
    echo [*] 正在启动 Demo 演示...
    python main.py --run demo
    pause
    goto MENU
)
if "%choice%"=="2" (
    echo.
    echo [*] 正在启动 QuanticData 滑块视觉求解测试...
    python main.py --run quanticdata
    pause
    goto MENU
)
if "%choice%"=="3" (
    echo.
    echo [*] 正在启动 AutomationExercise 实战靶场注册...
    python main.py --run automation_exercise
    pause
    goto MENU
)
if "%choice%"=="4" (
    echo.
    python main.py doctor
    pause
    goto MENU
)
if "%choice%"=="5" (
    echo.
    set /p target_url="请输入目标注册网址: "
    python main.py --url %target_url%
    pause
    goto MENU
)
if "%choice%"=="6" (
    echo.
    set /p sname="请输入新站点名称 (例如 github / twitter): "
    set /p surl="请输入目标注册网址 (可选，直接回车跳过): "
    if "%surl%"=="" (
        python main.py --scaffold %sname%
    ) else (
        python main.py --scaffold %sname% --url %surl%
    )
    pause
    goto MENU
)
if "%choice%"=="7" (
    echo.
    echo   [A] 导出为标准 CSV
    echo   [B] 导出为结构化 JSON
    echo   [C] 导出为 Bitwarden / 1Password 密码管理器 CSV
    set /p exp_fmt="请选择格式 [A/B/C]: "
    if /i "%exp_fmt%"=="A" python main.py export --export csv
    if /i "%exp_fmt%"=="B" python main.py export --export json
    if /i "%exp_fmt%"=="C" python main.py export --export bitwarden
    pause
    goto MENU
)
if "%choice%"=="8" (
    echo.
    echo [*] 正在运行自动化单元测试套件...
    pytest tests/ -v
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
