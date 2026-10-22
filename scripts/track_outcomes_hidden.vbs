' CS2QQBot outcome settlement - daily, no console window.
' Launched by scheduled task CS2QQBotOutcome (daily 03:10).
' ASCII only: wscript reads .vbs as ANSI, and PS 5.1 mangles BOM-less UTF-8 (see SKILL).
' Runs scripts\track_signal_outcomes.py which settles T+24h/72h/168h forward returns
' into signal_outcomes. Safe while the bot runs (WAL + short transactions).
' -X utf8 is required: with stdout redirected to a file python uses the ANSI codepage
' (GBK here) and the log becomes unreadable mojibake.
' Path-portable since V0.3.26: repo root is derived from this script's folder.
Option Explicit
Dim fso, here, root, sh, cmd, rc
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)      ' ...\cs2-qq-bot\scripts
root = fso.GetParentFolderName(here)                        ' ...\cs2-qq-bot
Set sh = CreateObject("WScript.Shell")
cmd = "cmd /c cd /d """ & root & """ && .venv\Scripts\python.exe -X utf8 scripts\track_signal_outcomes.py >> logs\outcome.log 2>&1"
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
