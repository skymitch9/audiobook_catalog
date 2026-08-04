' Task Scheduler entry point for "AudiobookDriveAudit".
' Runs drive_audit.bat with no visible console window (window style 0)
' and waits for it to finish so the task's LastTaskResult stays meaningful.
' All output still lands in output_files\drive_audit.log via the .bat.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
batPath = "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\scripts\drive_audit.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
