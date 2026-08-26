@echo off
rem Drive-side reactive trigger (Task Scheduler: "AudiobookDrivePoll", 15 min).
rem One tick of app/tools/drive_poll.py: ask the Drive Changes API what has
rem changed since the persisted page token; if a new book file appeared in a
rem library folder, run scripts\drive_pull.py --enforce and then queue a
rem pipeline_requests "run now" that AudiobookPipelineWatcher picks up within
rem 3 minutes. Cheap when idle: one small Changes request, no output.
rem
rem Why it exists: AudiobookFsWatcher sees LOCAL arrivals only, so a book
rem dropped straight into Drive waited for the next 8h STEP 0b pull -- up to
rem 8 hours. The 8-hourly AudiobookSyncPipeline is UNCHANGED and remains the
rem self-healing pass.
rem
rem WARNING: keep this file PURE ASCII. cmd.exe parses the ANSI codepage, not
rem UTF-8, and a UTF-8 em-dash in a rem line got EXECUTED as a command on
rem every tick of run_fs_watcher.bat (2026-08-16).
rem
rem Registration is the owner's call: docs/access/README.md, "Drive poll".
rem Log: output_files\drive_poll.log (this watcher's decisions only; the pull
rem and the pipeline still log to their own files).
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
rem SYNC_NON_INTERACTIVE=1: a run this task causes must never prompt (F2).
set SYNC_NON_INTERACTIVE=1
rem ABSOLUTE interpreter, never bare "python": under Task Scheduler the bare
rem name resolved to the WindowsApps Store stub and the first tick HUNG
rem invisibly forever - every later tick was then refused with 0x800710E0
rem ("instance already running"), which reads as a conditions problem and is
rem not one. Found 2026-08-16 on run_fs_watcher.bat, the day it was registered.
"C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\.venv\Scripts\python.exe" -m app.tools.drive_poll >> output_files\drive_poll.log 2>&1
