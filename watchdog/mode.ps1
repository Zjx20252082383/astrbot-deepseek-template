# 模式切换：offline = 省 token（QQ 离线，只跑定时任务），online = 完整功能
# 用法：powershell -File mode.ps1 -Mode online|offline （或直接双击根目录的 在线模式.bat / 离线模式.bat）
param([string]$Mode)
$base = Split-Path -Parent $PSScriptRoot
$log = "$base\data\watchdog.log"

function Log($m) {
    try { "$([DateTime]::Now.ToString('MM-dd HH:mm:ss')) [mode] $m" | Add-Content $log -Encoding UTF8 } catch {}
}

if ($Mode -ne "offline" -and $Mode -ne "online") {
    Write-Host "Usage: mode.ps1 -Mode offline|online"
    exit 1
}

Set-Content -Path "$base\watchdog\mode.txt" -Value $Mode -Encoding ASCII

if ($Mode -eq "offline") {
    # 停用费 token 的主动社交插件（只停这个开关停过的，不动手动停用的）
    $plugins = @(
        "$base\data\plugins\astrbot_plugin_autonomous_social"
    )
    $marker = "$base\watchdog\disabled_by_offline.txt"
    $disabled = @()
    foreach ($p in $plugins) {
        if (Test-Path $p) {
            Rename-Item $p "$p.disabled" -ErrorAction SilentlyContinue
            $disabled += "$p.disabled"
        }
    }
    if ($disabled.Count -gt 0) {
        Set-Content -Path $marker -Value $disabled -Encoding ASCII
    }
    # 停掉 SnowLuma（QQ 下线）
    Get-CimInstance Win32_Process -Filter "Name like 'node%'" |
        Where-Object { $_.CommandLine -like '*snowluma*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Log "OFFLINE mode set: SnowLuma stopped, AstrBot restarting"
    Write-Host "[OFFLINE] QQ 已下线，费 token 插件已停用。定时任务仍在运行。"
} else {
    # 恢复本开关停用的插件
    $marker = "$base\watchdog\disabled_by_offline.txt"
    if (Test-Path $marker) {
        Get-Content $marker | ForEach-Object {
            $line = $_.Trim()
            if ($line -and (Test-Path $line)) {
                $newName = $line -replace '\.disabled$', ''
                Rename-Item $line $newName -ErrorAction SilentlyContinue
            }
        }
        Remove-Item $marker -ErrorAction SilentlyContinue
    }
    Start-Process wscript.exe -ArgumentList "`"$base\snowluma_service_hidden.vbs`"" -WindowStyle Hidden
    Log "ONLINE mode set: SnowLuma starting, AstrBot restarting"
    Write-Host "[ONLINE] 所有功能已恢复。"
}

# 重启 AstrBot 使插件状态生效
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*astrbot*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Start-Process wscript.exe -ArgumentList "`"$base\astrbot_service_hidden.vbs`"" -WindowStyle Hidden
Write-Host "AstrBot 重启中... 约 40 秒后就绪"
