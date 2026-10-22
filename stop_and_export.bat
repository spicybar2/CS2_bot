@echo off
rem ============================================================
rem  Stop the bot and export state (watchlist + push_groups).
rem  Run this BEFORE switching to the other machine.
rem  Then copy data\state_export.json to the other machine.
rem ============================================================
cd /d "%~dp0"

echo [1/2] Stopping bot process ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Killed PID ' + $_.ProcessId) }"

echo [2/2] Exporting state ...
".venv\Scripts\python.exe" scripts\export_state.py
if errorlevel 1 goto :fail

echo.
echo DONE. Now copy  data\state_export.json  to the other machine,
echo then run  import_and_start.bat  there.
pause >nul
exit /b 0

:fail
echo EXPORT FAILED. See messages above.
pause >nul
exit /b 1
