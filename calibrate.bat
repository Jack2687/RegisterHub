@echo off
chcp 65001 >nul
title 视窗布局与防切割标定工具
cd /d "%~dp0"

if "%1"=="auto" goto do_auto
if "%1"=="demo" goto do_demo
if "%1"=="1" goto mode_single
if "%1"=="2" goto mode_dual
if "%1"=="4" goto mode_quad
if "%1"=="6" goto mode_hex

:MENU
cls
echo ====================================================================
echo   视窗几何与自适应防切割工具箱 (跨分辨率开箱适配 + 巡演验收)
echo ====================================================================
echo   [1] 一键自动适配本机屏幕 (生成 1/2/4双态/6双态 6套对称布局)
echo   [2] 一键全景巡演预览全部 6 套布局 (--demo-layouts)
echo   [3] 切换 4 并发形态 (贴满四边 edge_2x2 / 居中紧凑 centered_compact)
echo   [4] 切换 6 并发形态 (贴满六宫格 edge_3x2 / 八字展翼 wing_cascade)
echo   [5] 交互式手工微调标定 (可选 1 / 2 / 4 / 6 并发)
echo   [0] 退出
echo ====================================================================
echo.
set "choice="
set /p choice="请输入选项 [0-5] (直接回车执行 [1] 一键自动适配): "
if "%choice%"=="" set "choice=1"

if "%choice%"=="0" exit /b 0
if "%choice%"=="1" goto do_auto
if "%choice%"=="2" goto do_demo
if "%choice%"=="3" goto switch_quad
if "%choice%"=="4" goto switch_hex
if "%choice%"=="5" goto manual_cal
echo 无效选项，请重试。
timeout /t 2 >nul
goto MENU

:do_auto
echo.
python main.py --auto-layout
pause
goto MENU

:do_demo
echo.
set "sec="
set /p "sec=每套布局停留几秒 [默认 8]: "
if "%sec%"=="" set "sec=8"
python main.py --demo-layouts --demo-sec "%sec%"
pause
goto MENU

:switch_quad
echo.
echo   [A] edge_2x2         - 严丝合缝贴满四边 (0重叠)
echo   [B] centered_compact - 对称留白居中紧凑 (鼠标行程短，推荐)
set "qm="
set /p qm="请选择 4 并发形态 [A/B]: "
if /i "%qm%"=="A" python main.py --auto-layout --quad-mode edge_2x2
if /i "%qm%"=="B" python main.py --auto-layout --quad-mode centered_compact
pause
goto MENU

:switch_hex
echo.
echo   [A] edge_3x2     - 严丝合缝贴满六宫格 (0重叠)
echo   [B] wing_cascade - 左右对称八字阶梯展翼 (大视野，推荐)
set "hm="
set /p hm="请选择 6 并发形态 [A/B]: "
if /i "%hm%"=="A" python main.py --auto-layout --hex-mode edge_3x2
if /i "%hm%"=="B" python main.py --auto-layout --hex-mode wing_cascade
pause
goto MENU

:manual_cal
echo.
set "cc="
set /p "cc=请输入要微调的并发窗口数 (1 / 2 / 4 / 6) [默认 2]: "
if "%cc%"=="" set "cc=2"
python main.py --calibrate -c "%cc%"
pause
goto MENU

:mode_single
python main.py --calibrate -c 1 "%~2" "%~3"
pause
exit /b 0

:mode_dual
python main.py --calibrate -c 2 "%~2" "%~3"
pause
exit /b 0

:mode_quad
python main.py --calibrate -c 4 "%~2" "%~3"
pause
exit /b 0

:mode_hex
python main.py --calibrate -c 6 "%~2" "%~3"
pause
exit /b 0
