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
batPath = "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\scripts\run_purchase_audit.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
