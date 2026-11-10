@echo off
set "SNOWLUMA_ACCEPT_EULA=1"
set "SNOWLUMA_ACCEPT_PRIVACY=1"
cd /d "e:\Users\zjx\Desktop\deepseek接QQ小号\snowluma"
node ./index.mjs > "e:\Users\zjx\Desktop\deepseek接QQ小号\watchdog\snowluma_boot.log" 2>&1
