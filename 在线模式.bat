@echo off
chcp 65001 >nul
title 机器人 - 在线完整模式
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog\mode.ps1" -Mode online
pause
