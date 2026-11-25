' Task Scheduler entry point for "AudiobookPurchaseAudit".
' Runs run_purchase_audit.bat with no visible console window (window style 0)
' and waits so the task's LastTaskResult stays meaningful. Fires every 15
' minutes, so a visible window would be intolerable.
' Tick decisions land in output_files\purchase_audit.log; the auto_acquire
' subprocess it runs prints into the same file, and the pipeline it QUEUES
' after a download is started by AudiobookPipelineWatcher and logs to
' output_files\pipeline_8h.log.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
' The .bat sits beside this file: resolved from WScript.ScriptFullName
' (2026-11-25) so a repo move needs no edit here. The old hardcoded
' OneDrive path kept working after the move only through a junction.
batPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\run_purchase_audit.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
