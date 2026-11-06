$ErrorActionPreference = "Stop"
$runtimeDir = Join-Path $PSScriptRoot ".runtime"
$pidFile = Join-Path $runtimeDir "terminal.pid"
$stopFile = Join-Path $runtimeDir "terminal.stop"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "No background terminal PID file."
    return
}
$processInfo = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$process = Get-Process -Id $processInfo.pid -ErrorAction SilentlyContinue
if (-not $process -or $process.StartTime.ToUniversalTime().ToString("o") -ne $processInfo.started) {
    Remove-Item -LiteralPath $pidFile
    Write-Output "The recorded terminal process is no longer running."
    return
}
New-Item -ItemType File -Path $stopFile -Force | Out-Null
if (-not $process.WaitForExit(15000)) {
    throw "Terminal has not exited yet. Check $runtimeDir\terminal.log. The controller watchdog releases each action after at most 5 seconds."
}
Remove-Item -LiteralPath $pidFile
Write-Output "Terminal stopped gracefully; controller inputs released."
