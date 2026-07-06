@echo off
rem 8-hourly book pipeline (Task Scheduler: "AudiobookSyncPipeline"):
rem   1. auto_acquire: container up -> sync both Audible accounts -> top-50
rem      purchase audit -> Discord ping if downloads needed -> container stop
rem   2. full sync: sort/ingest new books (incl. container downloads), upload
rem      to Drive, rebuild catalog, extract chapters, fetch content warnings,
rem      fulfill warning requests, auto-commit to main (deploys /dev/)
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
echo ================= %date% %time% ================= >> output_files\pipeline_8h.log
python -m app.tools.auto_acquire --notify --stop-after >> output_files\pipeline_8h.log 2>&1
python scripts\sync_to_drive.py >> output_files\pipeline_8h.log 2>&1
