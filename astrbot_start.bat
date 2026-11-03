@echo off
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
astrbot run > "%~dp0watchdog\astrbot_boot.log" 2>&1
