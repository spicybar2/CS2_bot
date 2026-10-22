@echo off
chcp 65001 >nul
rem ============================================================
rem  push.bat - commit all local changes and push to the remote
rem
rem  Usage:
rem     push.bat                     commit with a default message
rem     push.bat fix watch baseline  commit with your own message
rem
rem  Secrets are safe: config.yaml, data\, logs\ and archive\ are
rem  listed in .gitignore and never leave this machine.
rem ============================================================
setlocal
cd /d "%~dp0.."

set "MSG=%*"
if "%MSG%"=="" set "MSG=chore: update %DATE% %TIME%"

rem --- preflight: is this a repo with a remote? ---
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
  echo [ERROR] not a git repository: %CD%
  pause
  exit /b 1
)
git remote get-url origin >nul 2>&1
if errorlevel 1 (
  echo [ERROR] no remote named "origin" yet.
  echo         run:  git remote add origin ^<your-repo-url^>
  pause
  exit /b 1
)

rem --- anything to commit? ---
git add -A
git diff --cached --quiet
if errorlevel 1 (
  echo [1/2] committing...
  git commit -m "%MSG%"
  if errorlevel 1 (
    echo [ERROR] commit failed
    pause
    exit /b 1
  )
) else (
  echo [1/2] nothing new to commit
)

echo [2/2] pushing to origin...
git push
if errorlevel 1 (
  echo [ERROR] push failed - check network / credentials
  pause
  exit /b 1
)

echo.
echo Done. Remote: 
git remote get-url origin
pause
