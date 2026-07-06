@echo off
rem 8-hourly book pipeline (Task Scheduler: "AudiobookSyncPipeline"):
rem   1. auto_acquire: container up -> sync both Audible accounts -> top-50
rem      purchase audit -> Discord ping if downloads needed -> container stop
rem   2. sync_to_drive --upload-only: ingest container downloads + upload new
rem      files to Drive, refresh catalog, fulfill content-warning requests
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
echo ================= %date% %time% ================= >> output_files\pipeline_8h.log
python -m app.tools.auto_acquire --notify --stop-after >> output_files\pipeline_8h.log 2>&1
python scripts\sync_to_drive.py --upload-only >> output_files\pipeline_8h.log 2>&1
