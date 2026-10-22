' CS2QQBot watchdog hidden launcher (path-portable since V0.3.26)
' Called by scheduled task "CS2QQBotWatchdog" every 10 minutes.
' Runs watchdog.ps1 with a hidden window (SW_HIDE) so no PowerShell
' console window flashes on the desktop.
'   arg 1 = 0     -> hidden window
'   arg 2 = False -> do not wait for the child process
Option Explicit
Dim fso, here, ps1, sh
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(here, "watchdog.ps1")
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & ps1 & """", 0, False
