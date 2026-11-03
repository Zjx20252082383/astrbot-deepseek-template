# 看门狗（可选功能）：每轮检查 AstrBot / SnowLuma 是否存活，掉线自动拉起
# 用法：用任务计划程序每 1~5 分钟运行一次 watchdog_hidden.vbs，或不定时手动运行
$base = Split-Path -Parent $PSScriptRoot
$log = "$base\data\watchdog.log"
$wd = $PSScriptRoot

function Log($msg) {
    try {
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
            Get-Content $log -Tail 200 | Set-Content $log -Encoding UTF8
        }
        "$([DateTime]::Now.ToString('MM-dd HH:mm:ss')) $msg" | Add-Content $log -Encoding UTF8
    } catch {}
}

function Test-Port($port) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(1500) -and $c.Connected) { $c.Close(); return $true }
        $c.Close(); return $false
    } catch { return $false }
}

function CooldownOk($name, $seconds) {
    $f = "$wd\cool_$name.txt"
    if (Test-Path $f) {
        $last = (Get-Item $f).LastWriteTime
        if (((Get-Date) - $last).TotalSeconds -lt $seconds) { return $false }
    }
    Set-Content -Path $f -Value "1" -Encoding ASCII
    return $true
}

function Start-Hidden($vbs) {
    Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WindowStyle Hidden
}

# 离线模式：主人主动下线，不自动拉起
$botMode = "online"
$modeFile = "$wd\mode.txt"
if (Test-Path $modeFile) { $botMode = (Get-Content $modeFile -Raw).Trim() }

# 1) AstrBot: 消息端口 6199 必须在监听
if (-not (Test-Port 6199)) {
    if (CooldownOk 'astrbot' 240) {
        Log "AstrBot down -> start astrbot_service_hidden.vbs"
        Start-Hidden "$base\astrbot_service_hidden.vbs"
    }
}

# 2) SnowLuma: WebUI 端口 5099 必须在监听
if ($botMode -eq "offline") { return }
if (-not (Test-Port 5099)) {
    if (CooldownOk 'snowluma' 300) {
        Log "SnowLuma down -> start snowluma_service_hidden.vbs"
        Start-Hidden "$base\snowluma_service_hidden.vbs"
    }
}
