# CS2QQBot heartbeat watchdog (V0.2.12 landed; V0.3.7 made ASCII-only)
# Called every 10 minutes by scheduled task CS2QQBotWatchdog:
#   bot.log not updated for >15 minutes = bot hung/stalled -> restart task + log it.
# This script never throws (scheduled task has no interactive console; failures go to watchdog.log).
# ASCII-ONLY on purpose: Windows PowerShell 5.1 decodes a BOM-less .ps1 as ANSI (cp936),
# so any non-ASCII literal here would land in watchdog.log as mojibake (documented pitfall #8).

$ErrorActionPreference = "SilentlyContinue"
# V0.3.26: paths derived from this script's location (works for any clone folder).
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "logs\watchdog.log"
$botLog = Join-Path $root "logs\bot.log"
$staleMinutes = 15

function Write-WdLog($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File $log -Append -Encoding UTF8
}

$mtime = (Get-Item $botLog -ErrorAction SilentlyContinue).LastWriteTime
if (-not $mtime) { Write-WdLog "bot.log missing, skip"; exit }

$ageMin = ((Get-Date) - $mtime).TotalMinutes
if ($ageMin -lt $staleMinutes) { exit }   # heartbeat OK -> silent exit (no log line by design)

Write-WdLog "bot.log stalled for $([math]::Round($ageMin)) min (threshold $staleMinutes), restarting CS2QQBot"
try {
    Stop-ScheduledTask -TaskName "CS2QQBot" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName "CS2QQBot" -ErrorAction Stop
    Start-Sleep -Seconds 20
    $newMtime = (Get-Item $botLog -ErrorAction SilentlyContinue).LastWriteTime
    if ($newMtime -gt $mtime) {
        Write-WdLog "restart done, bot.log is being written again [OK]"
    } else {
        Write-WdLog "[WARN] bot.log still not updated after restart, check manually"
    }
} catch {
    Write-WdLog "restart action failed: $($_.Exception.Message)"
}
