@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "terminal_exit=%errorlevel%"
if "%terminal_exit%"=="0" (
    echo Terminal start completed.
) else (
    echo Terminal start failed. Check the message above and .runtime logs.
    if not "%TERMINAL_LAUNCHER_NO_PAUSE%"=="1" pause
)
exit /b %terminal_exit%
