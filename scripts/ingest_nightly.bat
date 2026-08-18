@echo off
rem Nightly book-knowledge ingestion (Task Scheduler: "AudiobookIngestNightly").
rem
rem Owner's order, 2026-08-18: process books 00:00-08:00 America/Phoenix,
rem reviewed audiobooks first, all EPUBs, and never start while the GPU is
rem above 50%%. Amended the same day with text-layer PDFs, a twin-skip, batch 16
rem inside the window, opportunistic idle-time runs, and a dashboard pause.
rem
rem THIS FIRES EVERY 30 MINUTES, ALL DAY -- and that is deliberate.
rem The task is dumb; app/core/ingest_control.py holds every gate:
rem   * inside 00:00-07:45 Phoenix it works continuously at batch 16;
rem   * outside it, --opportunistic takes ONE book at a time at batch 8, and
rem     only after two GPU polls two minutes apart both read under 50%%;
rem   * the Firestore control doc (ingestion_control/state) can pause all of it
rem     from the GABI dashboard, and an UNREADABLE control counts as paused.
rem A single-flight lock (output_files\ingest_books.lock) means the 30-minute
rem cadence can never start a second Whisper process beside a running one.
rem
rem Why not one long 8-hour run at midnight: a crash at 00:20 would cost the
rem whole night, and nothing would resume it until the next day. A short task on
rem a frequent cadence is self-healing -- the next fire picks the queue back up.
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
echo ================= %date% %time% ================= >> output_files\ingest_nightly.log
python -m app.tools.ingest_books --run --opportunistic >> output_files\ingest_nightly.log 2>&1

rem ================= STATUS PUSH (added 2026-08-18) =================
rem Owner, looking at https://heygabi.ai/status/processing: "processing
rem doesn't seem wired up yet". Correct - the page and its write door shipped
rem that day and the PUSHER did not. This is its cadence.
rem
rem The pusher is catalog-platform\scripts\push-processing-board.mjs. It reads
rem ingest_state.json, the pack index, the receipts and the log above, projects
rem them into the agent-board contract, and POSTs via push-agent-board.mjs.
rem Read-only on every input: it never writes to estate-training-data and it
rem NEVER acquires output_files\ingest_books.lock - it only reads it.
rem
rem !! SOFT-FAIL, ALWAYS. A status surface must never cost a transcription run.
rem The ingester's own exit code is captured FIRST and handed back at the end,
rem so this task's LastTaskResult keeps meaning what it meant before. The push
rem can fail, find no node, or hang - it self-times-out after 60s - and the
rem ingest result is unchanged; only a warn line lands in the log.
rem
rem !! THIS ONLY FIRES WHEN THIS INVOCATION RETURNS. While a long transcription
rem holds the single-flight lock, the 30-minute invocations exit on the lock
rem within seconds and push from here anyway - but the invocation DOING the
rem transcribing pushes nothing for hours. The dedicated 15-minute task
rem "EstateProcessingBoardPush" covers that gap. Both are documented in
rem catalog-platform\docs\access\agent-board.md.
set INGEST_RC=%ERRORLEVEL%
node "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\catalog-platform\scripts\push-processing-board.mjs" --by "ingest-nightly@home-pc" >> output_files\processing_push.log 2>&1
if errorlevel 1 echo [WARN] status push failed - see output_files\processing_push.log - the ingest run above is unaffected >> output_files\ingest_nightly.log
exit /b %INGEST_RC%
