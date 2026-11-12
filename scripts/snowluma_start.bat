@echo off
rem ASCII only
set "SNOWLUMA_ACCEPT_EULA=1"
set "SNOWLUMA_ACCEPT_PRIVACY=1"
cd /d "%~dp0.."
if not exist "watchdog" mkdir "watchdog"
node .\snowluma\index.mjs > "watchdog\snowluma_boot.log" 2>&1