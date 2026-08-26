' Task Scheduler entry point for "AudiobookDrivePoll".
' Runs run_drive_poll.bat with no visible console window (window style 0) and
' waits so the task's LastTaskResult stays meaningful. Fires every 15 minutes,
' so a visible window would be intolerable.
' Poll decisions land in output_files\drive_poll.log; the pull it launches
' prints into the same file, and the pipeline it QUEUES is started by
' AudiobookPipelineWatcher and logs to output_files\pipeline_8h.log.
Dim shell, batPath
Set shell = CreateObject("Wscript.Shell")
batPath = "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\scripts\run_drive_poll.bat"
WScript.Quit shell.Run("""" & batPath & """", 0, True)
