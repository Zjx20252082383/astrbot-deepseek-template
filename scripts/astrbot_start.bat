@echo off
rem ASCII only
cd /d "%~dp0.."
if not exist "watchdog" mkdir "watchdog"
set "PYTHONNOUSERSITE=1"
rem 需要 astrbot 在 PATH 中（pip install astrbot 后生成 Sripts\astrbot.exe）
astrbot run > "watchdog\astrbot_boot.log" 2>&1
