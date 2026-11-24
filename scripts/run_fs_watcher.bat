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
rem
rem !! AND IT IS THE STORE PYTHON 3.12, NOT .venv (changed 2026-11-23).
rem This interpreter is inherited by the pipeline the watcher fires
rem (fs_watcher._fire runs sync_to_drive.py with sys.executable), and
rem .venv has NO audible-cli: every reactive run logged
rem   [audible-cli] export failed ... No module named audible_cli
rem in its STEP 0, i.e. a reactive run could never reach Audible.
rem audible-cli lives in the Store Python 3.12 below - the same one
rem run_purchase_audit.bat uses and the 8h sync_pipeline_8h.bat bare
rem "python" resolves to. Measured 2026-11-23 before changing this line:
rem mutagen 1.47.0 (settle-validation), audible_cli, app.tools.fs_watcher
rem and scripts.sync_to_drive all import under it, and
rem "-m app.tools.fs_watcher --status" runs. The old line said the venv
rem "guarantees mutagen"; the Store Python has it too.
"C:\Users\nbasl\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe" -m app.tools.fs_watcher >> output_files\fs_watcher.log 2>&1
