@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ══════════════════════════════════════════════════════════════
rem  注册「抖音粉丝数据每日抓取」Windows 任务计划
rem  默认每天 11:00 自动执行 run_all_accounts.py（数据每日10点更新后）
rem  修改时间：改下面这一行再重新运行本脚本即可
rem ══════════════════════════════════════════════════════════════
set "TASK_TIME=11:00"
set "TASK_NAME=DouyinFansSnapshot_Daily"

echo ================================================
echo  安装每日自动抓取任务（每天 %TASK_TIME%）
echo ================================================
echo.

rem ── 1) 定位 Python（多级探测，优先「装了依赖」的解释器）────────
set "PY="
rem ① 手动指定（特殊环境部署时，在这里填 python.exe 完整路径）
set "PY_OVERRIDE="
if defined PY_OVERRIDE if exist "%PY_OVERRIDE%" set "PY=%PY_OVERRIDE%"

rem ② 项目内 .venv
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

rem ③ PATH 中「装了依赖」的 python
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY (
            "%%i" -c "import requests, dotenv" >nul 2>nul
            if not errorlevel 1 set "PY=%%i"
        )
    )
)

rem ④ 常见安装目录（用户级/系统级，未加入 PATH 的情况）
if not defined PY (
    for /d %%d in ("%LocalAppData%\Programs\Python\Python3*") do (
        if not defined PY if exist "%%d\python.exe" (
            "%%d\python.exe" -c "import requests, dotenv" >nul 2>nul
            if not errorlevel 1 set "PY=%%d\python.exe"
        )
    )
)
if not defined PY (
    for /d %%d in ("%ProgramFiles%\Python3*") do (
        if not defined PY if exist "%%d\python.exe" (
            "%%d\python.exe" -c "import requests, dotenv" >nul 2>nul
            if not errorlevel 1 set "PY=%%d\python.exe"
        )
    )
)

rem ⑤ 兜底：PATH 里第一个 python（未验证依赖）
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY set "PY=%%i"
    )
)

if not defined PY (
    echo [错误] 未找到 python。请：
    echo   1) 安装 Python 3.12+（勾选 Add to PATH）
    echo   2) 执行 pip install -r requirements.txt
    echo   3) 重跑本脚本；或在本脚本顶部 PY_OVERRIDE= 填 python.exe 完整路径
    pause
    exit /b 1
)
echo   Python : %PY%
echo   脚本   : %~dp0run_all_accounts.py
echo.

rem ── 2) 注册任务计划（/F 覆盖旧任务，可反复运行）─────────────
schtasks /Create /TN "%TASK_NAME%" /TR "\"%PY%\" \"%~dp0run_all_accounts.py\"" /SC DAILY /ST %TASK_TIME% /F

if errorlevel 1 (
    echo.
    echo [失败] 注册失败。请右键本文件 →「以管理员身份运行」后重试。
    pause
    exit /b 1
)

echo.
echo [成功] 已注册每日任务：%TASK_NAME%（每天 %TASK_TIME% 自动抓取全部账号）
echo.
echo   查看任务：schtasks /Query /TN "%TASK_NAME%"
echo   立即测试：schtasks /Run    /TN "%TASK_NAME%"
echo   卸载任务：schtasks /Delete /TN "%TASK_NAME%" /F
echo.
echo   注意：任务在「用户已登录」状态下运行（保持本机登录即可）。
pause
