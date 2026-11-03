@echo off
set "SNOWLUMA_ACCEPT_EULA=1"
set "SNOWLUMA_ACCEPT_PRIVACY=1"
cd /d "%~dp0snowluma"
node ./index.mjs > "%~dp0watchdog\snowluma_boot.log" 2>&1
