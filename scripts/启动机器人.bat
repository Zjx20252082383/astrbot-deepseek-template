@echo off
title DeepSeek QQ 机器人
cd /d "%~dp0.."

echo [1/3] 启动 RVC 唱歌服务...
start "RVC" wscript.exe "E:\AI\rvc-ai-cover\rvc_service_hidden.vbs"

timeout /t 3 >nul
echo [2/3] 启动 SnowLuma（QQ 客户端）...
start "SnowLuma" "%~dp0snowluma_start.bat"

timeout /t 5 >nul
echo [3/3] 启动 AstrBot...
"C:\Users\zjx\AppData\Local\Programs\Python\Python311\Scripts\astrbot.exe" run

pause