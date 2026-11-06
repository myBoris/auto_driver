param(
    [string]$Python = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$TerminalArgs = @()
)

$ErrorActionPreference = "Stop"
$runtimeDir = Join-Path $PSScriptRoot ".runtime"
$pidFile = Join-Path $runtimeDir "terminal.pid"
$stopFile = Join-Path $runtimeDir "terminal.stop"

if (Test-Path -LiteralPath $pidFile) {
    $processInfo = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $existing = Get-Process -Id $processInfo.pid -ErrorAction SilentlyContinue
    if ($existing -and $existing.StartTime.ToUniversalTime().ToString("o") -eq $processInfo.started) {
        Write-Output "Terminal is already running (PID $($processInfo.pid))."
        return
    }
}
if (-not $env:CENTER_TOKEN -and -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".env"))) {
    throw 'Create terminal/.env with CENTER_TOKEN, or set $env:CENTER_TOKEN before starting.'
}
if (-not $Python) {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    $uvExecutable = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
    if (-not (Test-Path -LiteralPath $uvExecutable)) {
        throw "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and try again."
    }
    & $uvExecutable sync --locked --project $PSScriptRoot --link-mode copy
    if ($LASTEXITCODE -ne 0) {
        throw "uv could not prepare the terminal environment. Check the output above and try again."
    }
    $Python = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python not found: $Python. Run 'uv sync --locked --project terminal' from the project root, or 'uv sync --locked' inside terminal."
}
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
if (Test-Path -LiteralPath $stopFile) {
    Remove-Item -LiteralPath $stopFile
}

# Start-Process 会拼接 ArgumentList，按 Windows argv 规则独立引用每个参数以保留空格。
function ConvertTo-NativeArgument([string]$Value) {
    if ($Value -notmatch '[\s"]' -and $Value.Length -gt 0) { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}
$launchArgs = @("-m", "terminal", "--stop-file", $stopFile, "--log-file", (Join-Path $runtimeDir "terminal.log")) + $TerminalArgs
$quotedArgs = ($launchArgs | ForEach-Object { ConvertTo-NativeArgument $_ }) -join " "
$process = Start-Process -FilePath $Python -ArgumentList $quotedArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
@{ pid = $process.Id; started = $process.StartTime.ToUniversalTime().ToString("o") } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
if ($process.WaitForExit(1500)) {
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    throw "Terminal exited during startup (code $($process.ExitCode)). Check $runtimeDir\terminal.log."
}
Write-Output "Terminal started in background (PID $($process.Id)). Log: $runtimeDir\terminal.log"
