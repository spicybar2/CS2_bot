@echo off
chcp 65001 >nul
rem ============================================================
rem  setup_home.bat - one-time setup of a NEW machine (home PC)
rem
rem  Steps: check git+python -> create .venv -> install deps
rem         -> check config.yaml -> run offline tests -> next steps
rem
rem  Run this AFTER:  git clone <your-repo-url> cs2-qq-bot
rem  then:            cd cs2-qq-bot && scripts\setup_home.bat
rem
rem  ASCII output on purpose (cmd + chcp 65001 + GBK fallback).
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."
set "ROOT=%CD%"
echo.
echo Project root: %ROOT%
echo.

rem ---------- 1. git ----------
where git >nul 2>&1
if errorlevel 1 (
  echo [X] git not found. Install: https://git-scm.com/download/win
  pause
  exit /b 1
)
echo [OK] git: & git --version

rem ---------- 2. python 3.10+ ----------
set "PY="
where py >nul 2>&1 && set "PY=py -3.10"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [X] Python not found. Install Python 3.10+ and tick "Add to PATH".
  pause
  exit /b 1
)
echo [OK] python: & %PY% --version

rem ---------- 3. venv ----------
if not exist ".venv\Scripts\python.exe" (
  echo [1/4] creating venv ...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [X] venv creation failed & pause & exit /b 1 )
) else (
  echo [1/4] venv already exists
)

rem ---------- 4. dependencies ----------
echo [2/4] installing dependencies ...
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 ( echo [X] pip install failed & pause & exit /b 1 )

rem ---------- 5. config.yaml ----------
echo [3/4] checking config.yaml ...
if not exist "config.yaml" (
  copy /y "config.example.yaml" "config.yaml" >nul
  echo.
  echo   ============================================================
  echo    config.yaml was created from the template.
  echo    Open it and fill in:
  echo      csqaq.api_token      - from https://csqaq.com/home
  echo      qq.appid / qq.secret - QQ bot console
  echo      llm.api_key          - optional, for /ask replies
  echo    Then run this script again.
  echo   ============================================================
  pause
  exit /b 2
)
echo [OK] config.yaml exists

rem ---------- 6. tests ----------
echo [4/4] running offline tests ...
.venv\Scripts\python.exe -X utf8 tests\run_all.py
if errorlevel 1 (
  echo.
  echo [WARN] some tests failed - the bot may still run, check output above.
)

echo.
echo ============================================================
echo  Setup done. Next steps:
echo    1) scripts\register_tasks.bat   register logon watchdog outcome tasks
echo    2) scripts\restart_bot_hidden.vbs   start the bot now (hidden)
echo    3) live_log.bat                 watch logs\bot.log
echo.
echo  Optional - carry over watchers / push groups from the other machine:
echo    copy data\state_export.json here, then run import_and_start.bat
echo ============================================================
pause
