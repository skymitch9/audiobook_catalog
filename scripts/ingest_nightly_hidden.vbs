' Task Scheduler entry point for "AudiobookIngestNightly".
' Runs ingest_nightly.bat with no visible console window (window style 0) and
' waits for it to finish so the task's LastTaskResult stays meaningful.
' All output still lands in output_files\ingest_nightly.log via the .bat.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
' The .bat sits beside this file: resolved from WScript.ScriptFullName
' (2026-11-25) so a repo move needs no edit here. The old hardcoded
' OneDrive path kept working after the move only through a junction.
batPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\ingest_nightly.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
