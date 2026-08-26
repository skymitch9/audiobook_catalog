@echo off
rem 8-hourly book pipeline (Task Scheduler: "AudiobookSyncPipeline"):
rem   1. auto_acquire: container up -> sync both Audible accounts -> top-50
rem      purchase audit -> Discord ping if downloads needed -> container stop
rem   2. full sync: sort/ingest new books (incl. container downloads), upload
rem      to Drive, rebuild catalog, extract chapters, fetch content warnings,
rem      fulfill warning requests, auto-commit to main (deploys /dev/)
rem
rem PIPELINE_TRIGGER=scheduled (2026-08-16, docs/info/ROLES.md SS1d) marks
rem this as the ONE true 8-hourly slot: if the single-flight pipeline lock
rem is held (app/core/pipeline_lock.py) when this fires, sync_to_drive.py's
rem defer/retry logic (app/core/pipeline_schedule.py) waits up to 2h for it
rem to clear before giving up on this slot -- every other invocation (a
rem human running this script by hand, --rebuild-only, the remote "run now"
rem watcher trigger) defaults to trigger=manual and fails immediately
rem instead of waiting. Do not remove this line without also revisiting
rem pipeline_schedule.py's docstring.
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
set PIPELINE_TRIGGER=scheduled
rem SYNC_NON_INTERACTIVE=1 (F2, 2026-08-26): this task has no console,
rem so sync_to_drive.py must never prompt. It already infers that from
rem stdin.isatty(); this is the EXPLICIT statement of it, because an
rem inference is wrong in the case nobody anticipated and the cost of
rem being wrong here is a run that hangs holding the single-flight lock
rem until the 4h stale rule reclaims it. An ambiguous author-folder
rem match becomes a named skip on the /status upload step instead.
rem Keep this file PURE ASCII -- see the warning in run_fs_watcher.bat.
set SYNC_NON_INTERACTIVE=1
echo ================= %date% %time% ================= >> output_files\pipeline_8h.log
python -m app.tools.auto_acquire --notify --stop-after >> output_files\pipeline_8h.log 2>&1
python scripts\sync_to_drive.py >> output_files\pipeline_8h.log 2>&1
