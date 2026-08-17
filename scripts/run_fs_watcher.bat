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
rem ABSOLUTE interpreter, never bare "python": under Task Scheduler the
rem bare name resolved to the WindowsApps Store stub and the first tick
rem HUNG invisibly forever - every later tick was then refused with
rem 0x800710E0 ("instance already running"), which read as a conditions
rem problem and was not one. Found 2026-08-16, the day it was registered.
rem The venv also guarantees mutagen for settle-validation.
"C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\.venv\Scripts\python.exe" -m app.tools.fs_watcher >> output_files\fs_watcher.log 2>&1
