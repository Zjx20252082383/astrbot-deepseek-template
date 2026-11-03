@echo off
chcp 65001 >nul
title DeepSeek QQ 机器人
cd /d "%~dp0"

echo [1/2] 启动 SnowLuma（QQ 客户端）...
start "SnowLuma" cmd /c "snowluma_start.bat"

timeout /t 5 >nul
echo [2/2] 启动 AstrBot...
astrbot run

pause
