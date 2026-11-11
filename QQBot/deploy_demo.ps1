[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Join-Path $PSScriptRoot '..\..\qqbot_runtime')
)

$ErrorActionPreference = 'Stop'
$pluginName = 'astrbot_plugin_shihai_demo'
$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $pluginName))
$runtimePath = [System.IO.Path]::GetFullPath($RuntimeRoot)

if (-not (Test-Path -LiteralPath (Join-Path $runtimePath '.astrbot'))) {
    throw "Not an initialized AstrBot runtime: $runtimePath"
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'main.py'))) {
    throw "Plugin source not found: $sourceRoot"
}

$pluginsRoot = Join-Path $runtimePath 'data\plugins'
$targetRoot = [System.IO.Path]::GetFullPath((Join-Path $pluginsRoot $pluginName))
$expectedPrefix = [System.IO.Path]::GetFullPath($pluginsRoot).TrimEnd('\') + '\'
if (-not $targetRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Plugin destination is outside the selected runtime.'
}
if ((Test-Path -LiteralPath $targetRoot) -and
    ((Get-Item -LiteralPath $targetRoot).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    throw 'Plugin destination must not be a junction or symbolic link.'
}

New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
$count = 0
foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File) {
    if ($file.FullName -match '[\\/](__pycache__|\.git)[\\/]' -or
        $file.Extension -in @('.pyc', '.pyo')) {
        continue
    }
    $relativePath = $file.FullName.Substring($sourceRoot.Length + 1)
    $destination = Join-Path $targetRoot $relativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
    if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash) {
        throw "Copy verification failed: $relativePath"
    }
    $count++
}

Write-Output "Deployed and verified $count source files to $targetRoot"
Write-Output 'Reload this plugin in AstrBot, or restart AstrBot if this is the first deployment.'
