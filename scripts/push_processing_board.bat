@echo off
rem Dedicated status push for https://heygabi.ai/status/processing
rem (Task Scheduler: "EstateProcessingBoardPush", every 15 minutes, all day).
rem
rem WHY THIS EXISTS BESIDE THE STEP IN ingest_nightly.bat, added the same day:
rem that step only fires when its own invocation RETURNS. A transcription can
rem hold the single-flight lock for hours, and during those hours the invocation
rem doing the work pushes nothing - so the one moment the owner most wants the
rem page live ("which book is being processed right now") is exactly the moment
rem the piggy-backed cadence goes quiet. This task is decoupled from the
rem pipeline entirely and keeps publishing through a long run.
rem
rem !! IT NEVER TOUCHES THE PIPELINE. The pusher reads ingest_state.json, the
rem pack index, the receipts and the logs, and READS - never acquires -
rem output_files\ingest_books.lock. It starts no python, waits on nothing, and
rem writes only its own log and the board file.
rem
rem !! ALWAYS EXITS 0. A failed status push is not a failed machine, and a task
rem history full of red for a page that renders its own staleness honestly
rem would train the owner to ignore the one row that matters. The failure is
rem visible where a failure belongs: in the log below, and on the page itself,
rem which says how old its data is.
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
rem KI-3: every scheduled wrapper this repo owns sets this, without exception,
rem so the rule is "all of them" and not "the python ones". These three pushers
rem are node today; a python step added below tomorrow would otherwise inherit
rem a cp1252 pipe and die mid-run on the first non-ASCII book or author name.
set PYTHONIOENCODING=utf-8
node "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\catalog-platform\scripts\push-processing-board.mjs" --by "board-push-task@home-pc" >> output_files\processing_push.log 2>&1

rem ---------------------------------------------------------------------------
rem The other two home-side board sections, added 2026-08-18 with the /status
rem storage panel and the click-into-logs tails.
rem
rem !! ONE TASK, THREE SECTIONS, DELIBERATELY. Each could have had its own Task
rem Scheduler entry, and that was rejected: three tasks is three things to
rem notice have stopped, three histories to read and three cadences to keep in
rem step, for three jobs that all answer the same question - "what does the home
rem machine know that a Worker cannot see". They share this entry, its 15-minute
rem cadence and its log.
rem
rem !! EACH PUSHES ITS OWN SECTION AND MERGES THE REST. They read-modify-write
rem .local\agent-board.json and push it WHOLE (contract section 9), so running
rem them in sequence is safe - the later push carries the earlier one's work.
rem Running them in PARALLEL would NOT be: two processes read-modify-writing one
rem file race, and the loser's section is silently dropped. Keep them sequential.
rem
rem !! STILL ALWAYS EXITS 0, for the reason above: a failed status push is not a
rem failed machine.
node "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\catalog-platform\scripts\push-storage-board.mjs" --by "board-push-task@home-pc" >> output_files\processing_push.log 2>&1
node "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\catalog-platform\scripts\push-logs-board.mjs" --by "board-push-task@home-pc" >> output_files\processing_push.log 2>&1
exit /b 0
