@echo off
rem Fan Control - Windows Background Daemon Launcher
rem Hardware control requires an Administrator command prompt.
cd /d "%~dp0\.."

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

%PYTHON% fan-daemon.py %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo Fan Daemon exited with error code %ERRORLEVEL%.
    pause
)
