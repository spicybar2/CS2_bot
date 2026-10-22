# CS2QQBot deploy restart (V0.3.0): stop task -> clear python -> start task -> verify heartbeat.
#
# ASCII-ONLY ON PURPOSE: Windows PowerShell 5.1 (the interpreter used by scheduled tasks)
# decodes .ps1 files without a BOM as ANSI/GBK. A UTF-8 file with Chinese literals then
# either parses garbage or fails outright (observed: task exit code 1, no log written).
# Keep this file ASCII-only, or add a BOM if Chinese messages are ever needed.
#
# MUST be launched without a console (wscript.exe / Task Scheduler):
# starting the task from an interactive shell makes the bot inherit the caller console,
# sys.stdout.isatty() becomes True, the stdout StreamHandler is installed, and once the
# pipe backs up the main thread blocks forever in StreamHandler.emit (freeze incident,
# see CHANGELOG V0.2.29, and V0.3.6 for the root-cause fix).
#
# The script never throws (non-interactive context); everything lands in logs/restart.log.

$ErrorActionPreference = "SilentlyContinue"
# V0.3.26: paths are derived from this script's own location, so a clone in any
# folder (home machine / another drive) works without editing the file.
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "logs\restart.log"
$botLog = Join-Path $root "logs\bot.log"
$task = "CS2QQBot"

function Write-ReLog($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File $log -Append -Encoding UTF8
}

function Get-LogTail {
    param([string]$path, [int]$lines = 40)
    if (-not (Test-Path $path)) { return "" }
    return (Get-Content $path -Encoding UTF8 -Tail $lines) -join "`n"
}

Write-ReLog "=== deploy restart start ==="
$before = (Get-Item $botLog -ErrorAction SilentlyContinue).LastWriteTime
Write-ReLog "bot.log last write before restart: $before"

Stop-ScheduledTask -TaskName $task
Start-Sleep -Seconds 3
$procs = Get-Process python -ErrorAction SilentlyContinue
if ($procs) {
    Write-ReLog "killing leftover python processes: $($procs.Count) (pids: $($procs.Id -join ','))"
    $procs | Stop-Process -Force
} else {
    Write-ReLog "no leftover python process"
}
Start-Sleep -Seconds 3

Start-ScheduledTask -TaskName $task
Write-ReLog "scheduled task $task started; verifying heartbeat for 90s"

# Wait for "[ANOMALY] ... started N tasks" AFTER the restart: that line is the key
# discriminator between a healthy boot and the stdout-freeze half-boot.
$ok = $false
for ($i = 0; $i -lt 18; $i++) {
    Start-Sleep -Seconds 5
    $tail = Get-LogTail $botLog 80
    if ($tail -match "ANOMALY") {
        $mtime = (Get-Item $botLog -ErrorAction SilentlyContinue).LastWriteTime
        if ($mtime -gt $before) {
            $ok = $true
            break
        }
    }
}
$after = (Get-Item $botLog -ErrorAction SilentlyContinue).LastWriteTime
Write-ReLog "bot.log last write after restart: $after"
if ($ok) {
    Write-ReLog "VERIFY OK: bot.log is being written after restart"
} else {
    Write-ReLog "WARN: no fresh bot.log write within 90s - check logs/bot.log manually"
}
Write-ReLog "---- bot.log tail ----"
Write-ReLog (Get-LogTail $botLog 25)
Write-ReLog "=== deploy restart done ==="
