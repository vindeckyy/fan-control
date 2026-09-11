@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo   Fan Control - Windows PyInstaller Build Script
echo ===================================================

cd /d "%~dp0\.."

REM 1. Check Python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not found in PATH.
    echo Please install Python 3.10 or newer and make sure it is added to PATH.
    pause
    exit /b 1
)

echo [1/4] Checking Python environment...
python -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    pause
    exit /b 1
)

REM 2. Install / verify dependencies
echo [2/4] Installing / updating build requirements...
python -m pip install --upgrade pip
python -m pip install pyinstaller
if exist requirements-windows.txt (
    python -m pip install -r requirements-windows.txt
)

REM 3. Build UI if needed
if not exist "ui\dist\index.html" (
    echo [3/4] Building UI assets...
    where npm >nul 2>nul
    if errorlevel 1 (
        echo [WARNING] npm is not found; skipping UI build. Ensure ui\dist is populated.
    ) else (
        pushd ui
        call npm ci
        call npm run build
        popd
    )
) else (
    echo [3/4] UI assets found in ui\dist.
)

REM 4. Run PyInstaller
echo [4/4] Building Windows binaries with PyInstaller...
pyinstaller --clean -y packaging\windows\fan-control-pyinstaller.spec

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

REM Copy service installer helper to dist folder
if exist "dist\fan-control-windows" (
    copy /y "scripts\install-service-windows.ps1" "dist\fan-control-windows\" >nul 2>nul
    copy /y "packaging\icons\fan-control.ico" "dist\fan-control-windows\" >nul 2>nul
    echo.
    echo ===================================================
    echo   BUILD SUCCESSFUL!
    echo   Executables created in:
    echo     dist\fan-control-windows\fan-control.exe
    echo     dist\fan-control-windows\fan-ctl.exe
    echo     dist\fan-control-windows\fan-daemon.exe
    echo ===================================================
)

endlocal
pause
