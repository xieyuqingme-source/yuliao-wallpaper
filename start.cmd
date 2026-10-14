@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
if errorlevel 1 (
    echo.
    echo Yuliao Wallpaper could not start. See the message above and README.md.
    pause
    exit /b 1
)
exit /b 0
