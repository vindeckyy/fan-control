@echo off
rem Fan Control - Windows GUI Launcher
cd /d "%~dp0\.."

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

%PYTHON% fan-gui.py %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo Fan Control exited with error code %ERRORLEVEL%.
    pause
)
