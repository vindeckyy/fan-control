@echo off
setlocal enabledelayedexpansion

echo =========================================================
echo   Fan Control - Create Standalone Portable Windows Bundle
echo =========================================================

cd /d "%~dp0\.."

set "TARGET_DIR=dist\fan-control-portable"
set "PYTHON_VER=3.12.8"
set "PYTHON_ZIP=python-%PYTHON_VER%-embed-amd64.zip"
set "PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VER%/%PYTHON_ZIP%"

echo [1/5] Preparing output directory: %TARGET_DIR%
if exist "%TARGET_DIR%" (
    rmdir /s /q "%TARGET_DIR%"
)
mkdir "%TARGET_DIR%"
mkdir "%TARGET_DIR%\python"

echo [2/5] Downloading embeddable Python %PYTHON_VER%...
powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%TARGET_DIR%\%PYTHON_ZIP%'"
if errorlevel 1 (
    echo [ERROR] Failed to download embeddable Python package.
    pause
    exit /b 1
)

echo [3/5] Extracting Python runtime...
powershell -Command "Expand-Archive -Path '%TARGET_DIR%\%PYTHON_ZIP%' -DestinationPath '%TARGET_DIR%\python' -Force"
del /f /q "%TARGET_DIR%\%PYTHON_ZIP%"

REM Enable site-packages in embeddable Python by uncommenting 'import site' in python312._pth
powershell -Command "$p = (Get-Item '%TARGET_DIR%\python\python3*._pth').FullName; (Get-Content $p) | Foreach-Object { $_ -replace '^#import site', 'import site' } | Set-Content $p"

echo [4/5] Copying application files and executables...
copy /y "fan-control.exe" "%TARGET_DIR%\" >nul
copy /y "fan-gui.exe" "%TARGET_DIR%\" >nul
copy /y "fan-ctl.exe" "%TARGET_DIR%\" >nul
copy /y "fan-daemon.exe" "%TARGET_DIR%\" >nul

copy /y "*.py" "%TARGET_DIR%\" >nul
copy /y "requirements-windows.txt" "%TARGET_DIR%\" >nul
copy /y "scripts\install-service-windows.ps1" "%TARGET_DIR%\" >nul
copy /y "packaging\icons\fan-control.ico" "%TARGET_DIR%\" >nul

if exist "ui\dist" (
    mkdir "%TARGET_DIR%\ui\dist"
    xcopy /e /i /y "ui\dist" "%TARGET_DIR%\ui\dist" >nul
)

echo [5/5] Installing pip and optional dependencies into portable bundle...
powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%TARGET_DIR%\get-pip.py'"
"%TARGET_DIR%\python\python.exe" "%TARGET_DIR%\get-pip.py" --no-warn-script-location
del /f /q "%TARGET_DIR%\get-pip.py"
"%TARGET_DIR%\python\python.exe" -m pip install --no-warn-script-location -r "%TARGET_DIR%\requirements-windows.txt"

echo.
echo =========================================================
echo   PORTABLE PACKAGE READY!
echo   Location: %TARGET_DIR%
echo.
echo   You can run:
echo     %TARGET_DIR%\fan-control.exe
echo     %TARGET_DIR%\fan-ctl.exe
echo     %TARGET_DIR%\fan-daemon.exe
echo.
echo   This folder is completely portable and requires no
echo   existing Python installation on target machines.
echo =========================================================

endlocal
pause
