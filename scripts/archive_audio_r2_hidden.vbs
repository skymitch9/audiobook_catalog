' Task Scheduler entry point for "AudiobookArchiveR2".
' Runs archive_audio_r2.bat with no visible console window (window style 0) and
' waits for it to finish so the task's LastTaskResult stays meaningful.
' All output still lands in output_files\audio_archive.log via the .bat.
'
' Same shape as ingest_nightly_hidden.vbs -- a multi-day upload seed must not
' put a console window in front of whatever the owner is doing, every hour.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
' The .bat sits beside this file: resolved from WScript.ScriptFullName
' (2026-11-25) so a repo move needs no edit here. The old hardcoded
' OneDrive path kept working after the move only through a junction.
batPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\archive_audio_r2.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
