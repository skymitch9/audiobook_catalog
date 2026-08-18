' Task Scheduler entry point for "AudiobookIngestNightly".
' Runs ingest_nightly.bat with no visible console window (window style 0) and
' waits for it to finish so the task's LastTaskResult stays meaningful.
' All output still lands in output_files\ingest_nightly.log via the .bat.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
batPath = "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\scripts\ingest_nightly.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
