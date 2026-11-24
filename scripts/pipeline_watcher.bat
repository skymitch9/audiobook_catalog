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
rem
rem LOG FILE IS pipeline_watcher_v2.log SINCE 2026-11-24 (owner order "B").
rem A watcher process wedged on 2026-11-21 16:12 (Firestore call never
rem returned). After it was force-ended on 2026-11-24 09:24 its teardown
rem stayed stuck and it STILL held pipeline_watcher.log open, so every later
rem tick failed at this >> redirect (Last Result 1) - the 2026-10-19 shape.
rem Only a reboot frees that handle. Writing to a new file gets the watcher
rem running again without one. pipeline_watcher.log is the history before.
rem Switching back after a reboot is optional.
python -m app.tools.pipeline_watcher >> output_files\pipeline_watcher_v2.log 2>&1
