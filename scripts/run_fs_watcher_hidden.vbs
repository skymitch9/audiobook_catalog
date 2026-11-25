' Task Scheduler entry point for "AudiobookFsWatcher".
' Runs run_fs_watcher.bat with no visible console window (window style 0) and
' waits so the task's LastTaskResult stays meaningful. Fires every minute, so
' a visible window would be intolerable.
' Watcher decisions land in output_files\fs_watcher.log; the pipeline it
' launches still logs to output_files\pipeline_8h.log.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
' The .bat sits beside this file: resolved from WScript.ScriptFullName
' (2026-11-25) so a repo move needs no edit here. The old hardcoded
' OneDrive path kept working after the move only through a junction.
batPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\run_fs_watcher.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
