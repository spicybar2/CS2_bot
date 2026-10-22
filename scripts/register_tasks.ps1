# CS2QQBot scheduled tasks registration (V0.3.26)
#
# Creates the three tasks the bot needs, using THIS clone's path, so the project
# can run from any folder (home machine, another drive, another user profile):
#
#   CS2QQBot          At logon  -> .venv\Scripts\python.exe main.py        (the bot itself)
#   CS2QQBotWatchdog  Every 10m -> wscript watchdog.vbs                    (restart if bot.log stalls >15m)
#   CS2QQBotOutcome   Daily 03:10 -> wscript track_outcomes_hidden.vbs     (settle T+24/72/168h outcomes)
#
# ASCII-ONLY ON PURPOSE: Windows PowerShell 5.1 decodes a BOM-less .ps1 as ANSI (cp936),
# so any non-ASCII literal here would break parsing. See PROJECT.md "ops scripts are ASCII-only".
#
# Run:  scripts\register_tasks.bat   (right-click > Run as administrator not required,
#                                    but the window must not be closed early)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = Split-Path -Parent $here
$python = Join-Path $root ".venv\Scripts\python.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $python)) {
    Write-Host "[ERROR] venv not found: $python"
    Write-Host "        run scripts\setup_home.bat first"
    exit 1
}

function New-Task($name, $action, $trigger, $description) {
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description $description -Force | Out-Null
    Write-Host ("[OK] " + $name)
}

Write-Host "project root: $root"
Write-Host "python      : $python"
Write-Host ""

# 1) the bot itself, at logon
$a1 = New-ScheduledTaskAction -Execute $python -Argument "main.py" -WorkingDirectory $root
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $user
New-Task "CS2QQBot" $a1 $t1 "CS2 QQ bot (anomaly radar + watch loop)"

# 2) watchdog, every 10 minutes forever
$a2 = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('//B //Nologo "' + (Join-Path $here 'watchdog.vbs') + '"')
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
New-Task "CS2QQBotWatchdog" $a2 $t2 "restart bot when logs/bot.log stalls >15 min"

# 3) outcome settlement, daily at 03:10
$a3 = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('//B //Nologo "' + (Join-Path $here 'track_outcomes_hidden.vbs') + '"')
$t3 = New-ScheduledTaskTrigger -Daily -At "03:10"
New-Task "CS2QQBotOutcome" $a3 $t3 "settle T+24h/72h/168h signal outcomes"

Write-Host ""
Write-Host "Verify with:  Get-ScheduledTask -TaskName CS2QQBot*"
Write-Host "Start now  :  Start-ScheduledTask -TaskName CS2QQBot"
