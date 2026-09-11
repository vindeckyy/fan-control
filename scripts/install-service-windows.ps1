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
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Uninstallation complete."
    exit 0
}

if (-not $PythonPath) {
    $pythonCmd = (Get-Command python.exe -ErrorAction SilentlyContinue)
    if ($pythonCmd) {
        $PythonPath = $pythonCmd.Source
    } else {
        $PythonPath = "python.exe"
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
