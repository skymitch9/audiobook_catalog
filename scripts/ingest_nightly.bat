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
