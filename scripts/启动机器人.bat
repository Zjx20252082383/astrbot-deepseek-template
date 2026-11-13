@echo off
title DeepSeek QQ 机器人
cd /d "%~dp0.."

echo [1/3] 启动 RVC 唱歌服务...
if defined RVC_HOME (
    start "RVC" wscript.exe "%RVC_HOME%\rvc_service_hidden.vbs"
) else (
    echo     未设置 RVC_HOME，跳过 RVC（不需要唱歌功能可忽略）
)

timeout /t 3 >nul
echo [2/3] 启动 SnowLuma（QQ 客户端）...
start "SnowLuma" "%~dp0snowluma_start.bat"

timeout /t 5 >nul
echo [3/3] 启动 AstrBot...
rem 需要 astrbot 在 PATH 中
astrbot run

pause
