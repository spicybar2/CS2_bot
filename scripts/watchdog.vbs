' CS2QQBot watchdog hidden launcher
' Called by scheduled task "CS2QQBotWatchdog" every 10 minutes.
' Runs watchdog.ps1 with a hidden window (SW_HIDE) so no PowerShell
' console window flashes on the desktop.
'   arg 1 = 0     -> hidden window
'   arg 2 = False -> do not wait for the child process
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\cs2-qq-bot\scripts\watchdog.ps1""", 0, False
