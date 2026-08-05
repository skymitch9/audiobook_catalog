' Task Scheduler entry point for "AudiobookPipelineWatcher".
' Runs pipeline_watcher.bat with no visible console window (window style 0) and
' waits so the task's LastTaskResult stays meaningful. Fires every few minutes,
' so a visible window would be intolerable.
' Watcher decisions land in output_files\pipeline_watcher.log; the pipeline it
' launches still logs to output_files\pipeline_8h.log.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
batPath = "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\scripts\pipeline_watcher.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
