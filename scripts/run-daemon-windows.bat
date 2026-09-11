@echo off
rem Fan Control - Windows Background Daemon Launcher
cd /d "%~dp0\.."

python fan-daemon.py %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo Fan Daemon exited with error code %ERRORLEVEL%.
    pause
)
