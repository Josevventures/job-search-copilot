# Registers (or re-registers) the "Jobs Slack Intake" Windows scheduled task - idempotent.
#   daily 05:30 local -> jobs_slack_intake.py   (replies wait for the end of quiet hours inside the script)
# Interactive principal, no stored password, runs only while you are logged on. StartWhenAvailable runs a
# missed 05:30 start as soon as the machine is available; WakeToRun wakes it. On macOS/Linux use cron instead
# (docs/SETUP.md).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_jobs_intake_task.ps1 -Python C:\path\to\venv\Scripts\pythonw.exe
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_jobs_intake_task.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_jobs_intake_task.ps1 -Uninstall
param([switch]$Uninstall, [switch]$Status, [string]$Time = "05:30", [string]$Python = "")
$ErrorActionPreference = "Stop"

$name = "Jobs Slack Intake"
$script = Join-Path $PSScriptRoot "jobs_slack_intake.py"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if ($Uninstall) {
  if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false; Write-Host "Removed '$name'."
  } else { Write-Host "'$name' not installed." }
  return
}
if ($Status) {
  $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if ($t) { $i = $t | Get-ScheduledTaskInfo
    Write-Host ("{0} {1} last run {2} (result {3}) next {4}" -f $name, $t.State, $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime)
  } else { Write-Host "$name : NOT INSTALLED" }
  return
}
if (-not $Python) { throw "pass -Python <path to the venv's pythonw.exe>" }
foreach ($p in @($Python, $script)) { if (-not (Test-Path $p)) { throw "missing: $p" } }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description "Slack job-link intake -> /job-apply-fleet triage -> thread replies (job-search-copilot)" `
  -Force | Out-Null
Write-Host "Registered '$name' (daily $Time, working dir $repo)."
