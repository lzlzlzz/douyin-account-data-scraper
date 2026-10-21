@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo  Douyin Fans Snapshot - ALL Accounts
echo ==========================================
python run_all_accounts.py
echo.
echo ExitCode=%ERRORLEVEL%
pause
