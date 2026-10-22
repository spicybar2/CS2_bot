@echo off
chcp 65001 >nul
rem Wrapper: register the three scheduled tasks with THIS clone's path.
rem See register_tasks.ps1 for details. Safe to re-run (-Force overwrites).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_tasks.ps1"
pause
