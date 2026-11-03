﻿﻿﻿# Bot watchdog: checks RVC / AstrBot / NapCat every run, restarts if down.
$base = "E:\Users\zjx\Desktop\deepseek接QQ小号"
$log = "$base\data\watchdog.log"
$wd = "$base\watchdog"

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

# 1) RVC service on 18888
if (-not (Test-Port 18888)) {
    if (CooldownOk 'rvc' 180) {
        Log "RVC down -> schtasks Run RVCService"
        schtasks /Run /TN "RVCService" | Out-Null
    }
}

# 2) AstrBot: 6199 must be listening
if (-not (Test-Port 6199)) {
    if (CooldownOk 'astrbot' 240) {
        Log "AstrBot down -> schtasks Run BotAstrBot"
        schtasks /Run /TN "BotAstrBot" | Out-Null
    }
}

# 3) SnowLuma: webui 5099 responding. QQ login itself is manual (scan QR);
# offline mode: user intentionally took the bot offline - do NOT restart
$botMode = "online"
$modeFile = "$wd\mode.txt"
if (Test-Path $modeFile) { $botMode = (Get-Content $modeFile -Raw).Trim() }
if ($botMode -eq "offline") { return }

$snowOk = Test-Port 5099
if (-not $snowOk) {
    if (CooldownOk 'snowluma' 300) {
        Log "SnowLuma down -> restart snowluma_service_hidden.vbs (QQ must be running & logged in)"
        Start-Process wscript.exe -ArgumentList ('"{0}\snowluma_service_hidden.vbs"' -f $base)
    }
}
