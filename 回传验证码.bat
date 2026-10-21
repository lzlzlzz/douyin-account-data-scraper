@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo  Douyin SMS verify code relay
echo ==========================================
set /p code=请输入短信验证码后回车: 
python submit_code.py %code%
pause
