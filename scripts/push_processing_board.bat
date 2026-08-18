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
node "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\catalog-platform\scripts\push-processing-board.mjs" --by "board-push-task@home-pc" >> output_files\processing_push.log 2>&1
exit /b 0
