@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo  Douyin Fans Snapshot (7d + 30d)
echo ==========================================
python douyin_fans_snapshot.py --cookie-file douyin_cookies.txt
echo.
echo ExitCode=%ERRORLEVEL%
pause
