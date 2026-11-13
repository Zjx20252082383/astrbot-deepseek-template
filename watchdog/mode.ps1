# Bot mode switch: offline = save tokens (diary only), online = full features
param([string]$Mode)
# 路径按脚本位置推导：$base = 仓库根（= AstrBot 运行根），$wd = watchdog
$wd = $PSScriptRoot
$base = Split-Path -Parent $wd
$pluginsDir = "$base\data\plugins"
$stashDir = "$base\data\plugins_offline"
$log = "$base\data\watchdog.log"

# token-burning proactive plugins: offline mode stashes them OUT of data\plugins
# (AstrBot scans every dir under data\plugins, renaming to *.disabled does NOT work)
# 2026-11: Conversa / autonomous_social 已删除（识海念头引擎替代），现在暂存识海行为出口
$stashable = @("astrbot_plugin_shihai_behavior")

function Log($m) {
    try { "$([DateTime]::Now.ToString('MM-dd HH:mm:ss')) [mode] $m" | Add-Content $log -Encoding UTF8 } catch {}
}

if ($Mode -ne "offline" -and $Mode -ne "online") {
    Write-Host "Usage: mode.ps1 -Mode offline|online"
    exit 1
}

Set-Content -Path "$base\watchdog\mode.txt" -Value $Mode -Encoding ASCII

if ($Mode -eq "offline") {
    if (-not (Test-Path $stashDir)) { New-Item -ItemType Directory -Path $stashDir | Out-Null }
    $stashed = @()
    foreach ($name in $stashable) {
        if (Test-Path "$pluginsDir\$name.disabled") {
            Rename-Item "$pluginsDir\$name.disabled" $name -ErrorAction SilentlyContinue
        }
        if (Test-Path "$pluginsDir\$name") {
            Move-Item "$pluginsDir\$name" "$stashDir\$name" -Force
            $stashed += $name
        }
    }
    if ($stashed.Count -gt 0) {
        Set-Content -Path "$base\watchdog\offline_stashed.txt" -Value $stashed -Encoding ASCII
    }
    Get-Process QQ -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Log "OFFLINE: stashed [$($stashed -join ', ')], QQ closed, AstrBot restarting"
    Write-Host "[OFFLINE] token-burning plugins stashed. Diary tasks still run."
} else {
    $marker = "$base\watchdog\offline_stashed.txt"
    if (Test-Path $marker) {
        Get-Content $marker | ForEach-Object {
            $name = $_.Trim()
            if ($name -and (Test-Path "$stashDir\$name")) {
                Move-Item "$stashDir\$name" "$pluginsDir\$name" -Force
            }
        }
        Remove-Item $marker -ErrorAction SilentlyContinue
    }
    foreach ($name in $stashable) {
        if ((Test-Path "$pluginsDir\$name.disabled") -and (-not (Test-Path "$pluginsDir\$name"))) {
            Rename-Item "$pluginsDir\$name.disabled" $name -ErrorAction SilentlyContinue
        }
    }
    Log "ONLINE: plugins restored, AstrBot restarting. Open QQ manually and log in."
    Write-Host "[ONLINE] plugins restored. Open QQ and log in; SnowLuma hooks it automatically."
}

Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*astrbot*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
schtasks /Run /TN "BotAstrBot" | Out-Null
Write-Host "AstrBot restarting... ready in ~40s"