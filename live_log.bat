@echo off
rem V0.3.7: live log viewer (ASCII name on purpose - ops artifacts in this project stay ASCII).
rem The bot's own console window is blank BY DESIGN (production writes only to logs\bot.log,
rem see V0.3.6 freeze fix). This window is READ-ONLY: it follows logs\bot.log and never
rem starts/stops/touches the bot process.
chcp 65001 >nul
title CS2 QQ Bot - live log (read-only, Ctrl+C to quit)
cd /d "D:\cs2-qq-bot"
echo ============================================================
echo  CS2 QQ Bot live log  ->  logs\bot.log
echo  Read-only view. The bot itself runs under scheduled task CS2QQBot.
echo  Press Ctrl+C to close this window (the bot keeps running).
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Path 'D:\cs2-qq-bot\logs\bot.log' -Encoding UTF8 -Tail 40 -Wait"
pause >nul
