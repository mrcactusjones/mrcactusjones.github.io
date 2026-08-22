# Registers the daily scan with Windows Task Scheduler. Run once:
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1
# Remove it later with:
#   Unregister-ScheduledTask -TaskName "GapTracker" -Confirm:$false
param(
    [string]$At = "9:00AM",
    [string]$TaskName = "GapTracker"
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\run-daily.ps1"
if (-not (Test-Path $script)) { throw "Can't find $script" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At

# StartWhenAvailable matters: if the machine is asleep at 9am the run catches
# up later instead of silently losing a day of price history.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily Pokemon card grade-gap scan" -Force | Out-Null

Write-Host "Registered '$TaskName' - daily at $At"
Write-Host "Test it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:         $root\data\logs\"
