@echo off
rem ============================================================
rem  Import state from the other machine, then start the bot.
rem  Run this AFTER switching to this machine.
rem  Make sure  data\state_export.json  exists (from the other
rem  machine's stop_and_export.bat), then double-click this file.
rem ============================================================
cd /d "%~dp0"

echo [1/3] Stopping any running bot (avoid double instance) ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

echo [2/3] Importing state ...
".venv\Scripts\python.exe" scripts\import_state.py
if errorlevel 1 goto :fail

echo [3/3] Starting bot ...
rem V0.3.7: manual path explicitly turns console logging ON (scheduled-task path stays
rem          silent-by-default and writes only logs\bot.log - see V0.3.6 freeze fix).
start "" /min cmd /c "set CS2QQBOT_LOG_CONSOLE=1 && .venv\Scripts\python.exe -X utf8 main.py"
echo Bot is starting. Verify with:  powershell -Command "Get-Content logs\bot.log -Tail 5"
echo Expect:  robot online + [ANOMALY] started 8 tasks
pause >nul
exit /b 0

:fail
echo IMPORT FAILED. Bot NOT started. See messages above.
pause >nul
exit /b 1
