@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem V0.3.7: this is the MANUAL foreground entry, so console logging is explicitly ON;
rem          the scheduled-task path does not set this var and stays silent (writes only
rem          logs\bot.log) to avoid the stdout-blocking freeze documented in V0.3.6.
set CS2QQBOT_LOG_CONSOLE=1
echo Starting CS2 QQ Bot (console log ON, file log: logs\bot.log) ...
".venv\Scripts\python.exe" -X utf8 main.py
echo Bot exited. Press any key to close.
pause >nul
