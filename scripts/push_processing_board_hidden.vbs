' Task Scheduler entry point for "EstateProcessingBoardPush".
' Runs push_processing_board.bat with no visible console window (window style 0)
' and waits for it to finish so the task's LastTaskResult stays meaningful.
' Same pattern as ingest_nightly_hidden.vbs, for the same reason: this fires
' every 15 minutes all day and a console window flashing across the owner's
' screen four times an hour is how a status surface gets switched off.
' All output still lands in output_files\processing_push.log via the .bat.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
batPath = "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\scripts\push_processing_board.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
