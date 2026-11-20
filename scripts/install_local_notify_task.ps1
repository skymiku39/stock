# Register (or remove) the on-demand task that shows queued local notifications
# on the signed-in user's desktop. Background jobs run in session 0 (S4U) and
# cannot show toasts themselves; they queue a notification and run this task.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_local_notify_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_local_notify_task.ps1 -Remove
#
# No administrator rights needed. Keep this file ASCII only (no BOM).
param([switch]$Remove)
$ErrorActionPreference = 'Stop'

$taskName = 'SkyMiku Stock Local Notify'
$root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "Removed task: $taskName"
    exit 0
}

$python = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) { throw "Not found: $python" }

$action = New-ScheduledTaskAction -Execute $python -Argument '-m bot.local_notify --flush' -WorkingDirectory $root
# At logon: show anything queued while nobody was signed in. Otherwise on demand.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances Queue -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Stock Lab: show queued local notifications on the desktop.' -Force | Out-Null
Write-Output "Registered task: $taskName"
