@echo off
rem Reactive filesystem watcher (Task Scheduler: "AudiobookFsWatcher", 1 min).
rem One tick of app/tools/fs_watcher.py: snapshot ROOT_DIR, diff against the
rem persisted baseline; a settled+quiet change runs the full pipeline with
rem PIPELINE_TRIGGER=reactive. Cheap when idle. The 8-hourly
rem AudiobookSyncPipeline task is UNCHANGED (the self-healing pass).
rem
rem WARNING: keep this file PURE ASCII. It was first written with UTF-8
rem emoji and em-dashes in these comments, and cmd.exe (which parses the
rem ANSI codepage, not UTF-8) misread the rem lines and EXECUTED comment
rem fragments as commands on every tick. Found 2026-08-16 by running it.
rem
rem Registration is the owner's call: docs/access/PIPELINE.md, "Reactive
rem watcher". Log: output_files\fs_watcher.log (watcher decisions only;
rem the pipeline still logs to pipeline_8h.log).
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
python -m app.tools.fs_watcher >> output_files\fs_watcher.log 2>&1
