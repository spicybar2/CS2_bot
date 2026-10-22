@echo off
chcp 65001 >nul
rem ============================================================
rem  update_and_restart.bat - pull latest code, then restart bot
rem
rem  Intended for the HOME machine (or any second machine).
rem  Flow: git pull --ff-only  ->  stop bot  ->  start bot
rem
rem  Safe by design:
rem    - --ff-only never creates merge commits; if it fails you
rem      have local edits and must resolve them yourself.
rem    - runtime data (data\, logs\, config.yaml) is untouched.
rem ============================================================
setlocal
cd /d "%~dp0.."

echo [1/3] pulling latest code...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [WARN] pull did not fast-forward. Nothing was restarted.
  echo        check "git status" and resolve local changes first.
  pause
  exit /b 1
)

echo [2/3] dependencies (skip if unchanged)...
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt
) else (
  echo [WARN] .venv missing - run scripts\setup_home.bat first
)

echo [3/3] restarting bot...
wscript.exe //B //Nologo "%~dp0restart_bot_hidden.vbs"
timeout /t 12 >nul
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*cs2-qq-bot*main.py*' }; if ($p) { 'OK  bot pid=' + ($p.ProcessId -join ',') } else { 'WARN bot not running yet' }"

echo.
echo Done. Log: logs\bot.log
pause
