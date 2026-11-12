@echo off
rem ASCII only
cd /d "%~dp0.."
if not exist "watchdog" mkdir "watchdog"
set "PYTHONNOUSERSITE=1"
"C:\Users\zjx\AppData\Local\Programs\Python\Python311\Scripts\astrbot.exe" run > "watchdog\astrbot_boot.log" 2>&1