# Fan Control - Windows Service / Task Installer
# Run in PowerShell as Administrator

[CmdletBinding()]
param (
    [switch]$Uninstall,
    [string]$PythonPath = "",
    [string]$ConfigPath = ""
)

$TaskName = "FanControlDaemon"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script in an elevated PowerShell session (Run as Administrator)."
    exit 1
}

if ($Uninstall) {
    Write-Host "Removing Scheduled Task '$TaskName'..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    try {
        $obj = Get-CimInstance -Namespace root/wmi -ClassName AcpiTest_MULong -ErrorAction Stop | Select-Object -First 1
        $read = Invoke-CimMethod -InputObject $obj -MethodName GetSetULong -Arguments @{ Data = [uint64]0x0000010000000751 }
        $mode = [int]($read.Return -band 0xFF)
        if ($mode -band 0x40) {
            $data = [uint64](([int]($mode -band 0xBF) -shl 16) -bor 0x0751)
            Invoke-CimMethod -InputObject $obj -MethodName GetSetULong -Arguments @{ Data = $data } | Out-Null
            Write-Host "Released manual EC fan control; firmware automatic control restored."
        }
    } catch {
        Write-Host "Note: could not release manual EC fan control automatically."
    }
    Write-Host "Uninstallation complete."
    exit 0
}

if (-not $PythonPath) {
    $venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    $venvPythonAlt = Join-Path $ProjectDir "venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $PythonPath = $venvPython
    } elseif (Test-Path $venvPythonAlt) {
        $PythonPath = $venvPythonAlt
    } else {
        $pythonCmd = (Get-Command python.exe -ErrorAction SilentlyContinue)
        if ($pythonCmd) {
            $PythonPath = $pythonCmd.Source
        } else {
            $PythonPath = "python.exe"
        }
    }
}

$RequirementsFile = Join-Path $ProjectDir "requirements-windows.txt"
if ((Test-Path $RequirementsFile) -and ($PythonPath -ne "python.exe")) {
    & $PythonPath -c "import clr" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing Python dependencies required for hardware control..."
        & $PythonPath -m pip install --no-warn-script-location -r $RequirementsFile
    }
}

$DaemonScript = Join-Path $ProjectDir "fan-daemon.py"
if (-not (Test-Path $DaemonScript)) {
    Write-Error "fan-daemon.py not found at $DaemonScript"
    exit 1
}

$ArgsList = "`"$DaemonScript`""
if ($ConfigPath) {
    $ArgsList += " --config `"$ConfigPath`""
}

Write-Host "Configuring Windows Scheduled Task '$TaskName' to run at system startup..."
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument $ArgsList -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null

Write-Host "Starting '$TaskName'..."
Start-ScheduledTask -TaskName $TaskName

Write-Host "Fan Control daemon successfully installed and started as a background system task."
Write-Host "To view task status: Get-ScheduledTask -TaskName $TaskName"
Write-Host "To uninstall: .\install-service-windows.ps1 -Uninstall"
