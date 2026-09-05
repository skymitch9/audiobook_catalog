@echo off
rem Audible purchase audit on a 15-minute cadence (Task Scheduler:
rem "AudiobookPurchaseAudit"). One tick of app/tools/purchase_audit.py: run the
rem 8h pipeline's OWN acquisition command (python -m app.tools.auto_acquire
rem --notify --stop-after) and, only when a book actually downloads, queue the
rem same pipeline_requests "run now" document the /status button writes, which
rem AudiobookPipelineWatcher picks up within 3 minutes.
rem
rem Why it exists (owner ask 2026-09-05, option "a" = 15 min with back-off):
rem a PURCHASE was discovered only by the 8-hourly AudiobookSyncPipeline, since
rem the two reactive watchers react to FILES and a purchase is not a file until
rem something downloads it. Worst-case purchase -> site latency was ~8 hours.
rem The 8-hourly AudiobookSyncPipeline is UNCHANGED and remains the self-healing
rem pass; this task adds no step of its own.
rem
rem SINGLE-FLIGHT: the tick defers on app/core/pipeline_lock.py AND on the
rem AudiobookSyncPipeline task's own Status, because the 8h run's acquisition
rem stage holds no lock. See the module docstring.
rem
rem BACK-OFF lives in the module, not here: this task stays at a flat 15 min and
rem purchase_audit_state.json carries the current interval (15 -> 30 -> 60 on
rem errors, reset on a clean tick). Kill switch: PURCHASE_AUDIT_ENABLED=0.
rem
rem WARNING: keep this file PURE ASCII. cmd.exe parses the ANSI codepage, not
rem UTF-8, and a UTF-8 em-dash in a rem line got EXECUTED as a command on
rem every tick of run_fs_watcher.bat (2026-08-16).
rem
rem Registration is the owner's call: docs/access/PIPELINE.md, "Purchase audit".
rem Log: output_files\purchase_audit.log (one line per tick, including the ticks
rem that do nothing).
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
rem SYNC_NON_INTERACTIVE=1: a run this task causes must never prompt (F2).
set SYNC_NON_INTERACTIVE=1
rem ABSOLUTE interpreter, never bare "python": under Task Scheduler the bare
rem name resolved to the WindowsApps Store stub and the first tick HUNG
rem invisibly forever - every later tick was then refused with 0x800710E0
rem ("instance already running"), which reads as a conditions problem and is
rem not one. Found 2026-08-16 on run_fs_watcher.bat, the day it was registered.
"C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog\.venv\Scripts\python.exe" -m app.tools.purchase_audit >> output_files\purchase_audit.log 2>&1
