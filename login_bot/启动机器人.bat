@echo off
chcp 65001 >nul
title 抖音登录机器人
cd /d "%~dp0"

set PY=python
if not exist "%PY%" set PY=python

echo ================================================
echo   抖音登录机器人（看门狗 + 机器人 自动互保）
echo ================================================
echo.

rem 检查是否已有看门狗在运行
"%PY%" -c "import psutil,sys; alive=any('dy_bot_watchdog' in ' '.join(p.info['cmdline'] or []) for p in psutil.process_iter(['cmdline'])); sys.exit(0 if alive else 1)" 2>nul
if not errorlevel 1 (
    echo [!] 机器人已在运行，无需重复启动。
    echo.
    pause
    exit /b 0
)

echo 正在启动看门狗（它会自动拉起机器人并持续守护）...
start "抖音机器人守护" /min "%PY%" -u dy_bot_watchdog.py
echo.
echo 已启动！任务栏可见一个最小化的守护窗口（勿关闭它）。
echo 想停止机器人：关闭那个最小化窗口即可。
timeout /t 5 >nul
exit /b 0
