@echo off
rem Manual-run watcher (Task Scheduler: "AudiobookPipelineWatcher", every 3 min).
rem
rem Polls Firestore for a "run now" request from the admin panel and, if one is
rem present AND carries the right token, runs the full pipeline. Cheap when idle:
rem one small Firestore read, no output unless something happens.
rem
rem The pipeline's own log still goes to output_files\pipeline_8h.log; this file
rem only records the watcher's own decisions.
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
python -m app.tools.pipeline_watcher >> output_files\pipeline_watcher.log 2>&1
