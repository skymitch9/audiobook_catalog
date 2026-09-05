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
rem
rem !! AND IT IS THE STORE 3.12, NOT .venv - MEASURED, NOT PREFERRED.
rem The first live tick (2026-09-05 14:07) ran the .venv interpreter, like
rem run_drive_poll.bat does, and audible-cli IS NOT INSTALLED THERE:
rem   [audible-cli] export failed for skylar: ...\.venv\Scripts\python.exe:
rem   No module named audible_cli
rem audible-cli 0.3.3 lives in the Store Python 3.12 below - the same one the
rem 8h sync_pipeline_8h.bat's bare "python" resolves to - and audit_new_
rem purchases shells out as "sys.executable -m audible_cli", so the
rem interpreter running this module decides whether Audible can be asked at
rem all. When it cannot, the audit silently falls back to the container's
rem books.json and reports "0 missing" WITH EXIT CODE 0. Same "which
rem interpreter: BOTH" trap docs/access/PIPELINE.md records for the OCR lane.
rem The tick classifies that fallback as a FAILING tick and says so by name,
rem but the fix is this line. Escape hatch: PURCHASE_AUDIT_PYTHON.
"C:\Users\nbasl\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe" -m app.tools.purchase_audit >> output_files\purchase_audit.log 2>&1
