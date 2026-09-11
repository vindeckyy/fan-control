@echo off
rem Fan Control - Windows GUI Launcher
cd /d "%~dp0\.."

python fan-gui.py %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo Fan Control exited with error code %ERRORLEVEL%.
    pause
)
