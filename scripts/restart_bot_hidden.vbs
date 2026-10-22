' CS2QQBot deploy restart hidden launcher (V0.3.0, path-portable since V0.3.26)
' Mirrors watchdog.vbs: wscript.exe is a GUI-subsystem host without a console,
' so neither powershell nor the bot it starts inherits an interactive console
' (prevents the stdout-pipe freeze documented in CHANGELOG V0.2.29; root cause fixed in V0.3.6).
'   arg 1 = 0     -> hidden window
'   arg 2 = False -> do not wait for the child process (restart takes ~2 minutes;
'                    result is verified through logs/restart.log)
' Paths are derived from this script's own folder, so the project can live anywhere.
Option Explicit
Dim fso, here, ps1, sh
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(here, "restart_bot.ps1")
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & ps1 & """", 0, False
