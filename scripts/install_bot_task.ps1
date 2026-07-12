# Register the context-graph Slack bot as a per-user Scheduled Task that starts
# at logon and restarts if it stops. No administrator rights required.
#
# Run once:   powershell -ExecutionPolicy Bypass -File scripts\install_bot_task.ps1
# Remove:     Unregister-ScheduledTask -TaskName ContextGraphBot -Confirm:$false

$ErrorActionPreference = "Stop"

$runner = Join-Path $PSScriptRoot "run_bot.ps1"
if (-not (Test-Path $runner)) { throw "run_bot.ps1 not found next to this script" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "ContextGraphBot" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Context-graph Slack bot (auto-start at logon, auto-restart)" `
    -Force

Write-Host "Registered scheduled task 'ContextGraphBot'."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName ContextGraphBot"
