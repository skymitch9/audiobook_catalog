@echo off
rem Reactive filesystem watcher (Task Scheduler: "AudiobookFsWatcher", every 1 min).
rem
rem One tick of app/tools/fs_watcher.py: snapshot ROOT_DIR, diff against the
rem persisted baseline, and if a settled+quiet change is present, run the full
rem pipeline with PIPELINE_TRIGGER=reactive. Cheap when idle: one directory
rem scan, no network, no output unless something happens.
rem
rem This is the ADDITIVE half of the hybrid design — the 8-hourly
rem AudiobookSyncPipeline task is UNCHANGED and remains the self-healing pass.
rem The pipeline's own log still goes to output_files\pipeline_8h.log; this
rem file only records the watcher's own decisions.
rem
rem ⚠️ NOT registered automatically — see docs/access/PIPELINE.md ("Reactive
rem watcher") for the schtasks command; registering is the owner's call.
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
python -m app.tools.fs_watcher >> output_files\fs_watcher.log 2>&1
