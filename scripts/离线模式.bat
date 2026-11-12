@echo off
chcp 65001 >nul
title 机器人 - 离线省电模式
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\watchdog\mode.ps1" -Mode offline
pause