# audiobook_catalog — working rules

> **Audience:** future Claude sessions first, humans second. **Status:** TRACKED
> — committed **even though `docs/` is not** (below), so a clone gets these rules
> and nothing else. Last verified: **2026-09-07**.

Read `docs/README.md` (the map) first, every session — then
`docs/KNOWN_ISSUES.md` **before you "fix" anything**, and `docs/info/gotchas.md`,
which titles every trap by its symptom. This file is only what bites in the
first ten minutes; it points at the docs and never restates them.

## ⚠️ `docs/` is GITIGNORED here — a clone has no documentation at all

`.gitignore:7` ignores `docs/` wholesale (`KI-5`, `ACCEPTED` by design: the tree
holds `access/CREDENTIALS.md` and `access/keys/`, and this repo is **public**).
The safety net is the R2 docs backup — `catalog-platform/scripts/backup-docs.mjs`,
restore drilled 2026-08-21, runbook `catalog-platform/docs/access/backup-restore.md`
§6b. A session that assumes git protects a file under `docs/` is wrong twice over.

⚠️ **`docs/TODO.md` is 0 bytes right now**, awaiting an owner-run restore. Do not
read it as evidence of anything, and do not write to it.

## 🔴 THIS MACHINE IS THE LIVE PIPELINE BOX

Not a checkout of a pipeline — the pipeline. Windows Task Scheduler runs
`AudiobookSyncPipeline` (8 h, 00/08/16:00), `AudiobookFsWatcher` (1 min),
`AudiobookDrivePoll` (15 min), `AudiobookIngestNightly` (30 min) and others
against the real ~1,080-book library. Consequences, all load-bearing:

- ⚠️ **Owner standing order, 2026-08-15:** *"no matter what do not kill any part
  of my pipeline."* Never kill a python process here, never `/End` a task to
  unblock yourself, never revert its output. Mid-book ingestion is never killed.
- ⚠️ **Ingestion runs 00:00–08:00 America/Phoenix** (owner's order; the gate
  works continuously to 07:45 and `--opportunistic` outside it). Phoenix has
  **no DST** — fixed UTC-7. `app/core/ingest_control.py` owns every gate; the
  task itself is dumb and fires all day. The window is
  `AudiobookIngestNightly`'s alone — `AudiobookPurchaseAudit` is not window-bound.
- ⚠️ **Never run `ingest_books` / `transcribe_audiobook` / `purchase_audit` /
  `drive_poll` casually.** Each has a `--dry-run` or `--status` read-only form;
  use it. A bare `python -m app.tools.purchase_audit` **downloads** and queues a
  run. Runbooks: `docs/access/PIPELINE.md`.
- ⚠️ **`output_files/pipeline.lock` means a run is in flight**
  (`app/core/pipeline_lock.py`); `output_files/ingest_books.lock` is ingestion's.
- ⚠️ **Never hand-edit a state file.** `C:\Users\nbasl\estate-training-data\ingest_state.json`
  is outside every repo **by path, on purpose**, and a running pipeline writes
  it — the supported route is `ingest_books --requeue-failed` / `--requeue-ocr`
  (via `ingest_queue.apply_requeue`, so `done` stays untouchable). Same for
  `output_files/*_state.json`.
- ⚠️ **The working tree changes under you** — the pipeline auto-commits. Before
  reverting a dirty file, **establish who wrote it** (`docs/info/gotchas.md`,
  "The test suite dirtied my working tree"): a `git checkout` once destroyed
  live pipeline output.
- ⚠️ **Pipeline code is not touched without asking the owner** — additive is not
  zero-risk. That covers `app/core/ingest_*.py`, `app/tools/ingest_books.py`,
  `scripts/transcribe_audiobook.py`, `scripts/sync_to_drive.py` and any
  live-path `scripts/` file including `scripts/build_ebook_manifest.py`.

## `site/index.html` is GENERATED — edit the template

`app/web/html_builder.py` renders `app/web/templates/index.html` into the
committed 9.4 MB `site/index.html`. Editing the output is lost work; editing the
template without rebuilding ships nothing, silently. After any template edit run
`python -m app.tools.rebuild_site_html` — **not** `app.main`, which rewrites the
catalogue underneath your commit. `ebooks.html`, `read.html`, `guess-game.html`
and `stats.html` are generated too; `community.html`, `club.html`,
`club-read.html`, `clubs.html` are hand-written. Details: `docs/info/gotchas.md`.

## Deploy: two lanes, and prod is not yours

`audiobooks.heygabi.ai/dev/` auto-deploys from every push to `main`. Prod
(`audiobooks.heygabi.ai/`) moves **only** when `promote.yml` runs — an owner-run
`workflow_dispatch`. Push to `main` only; never push `prod`. The one exception
is book-only auto-commits, which self-promote by design.
`docs/access/GIT_CI_DEPLOY.md` owns the guards and the rollback recipe.

## Tests

```bash
PYTHONIOENCODING=utf-8 python -m pytest tests/ -q   # the suite CI runs
npx vitest run                                      # JS (site/__tests__)
```

`pytest` is the only Python runner. ⚠️ `PYTHONIOENCODING=utf-8` is not optional
here — the cp1252 console crash kills reporters near the *end*, where it looks
like success. `tests/test_catalog_completeness.py` is **read-only as of
2026-09-07** (its 1,090 cover writes now go to a temp dir) and carries a
module-level `pytest.mark.skipif`, so all 11 of its cases skip on CI, which has
no audio library. ⚠️ A green local run is not evidence for CI: this box is
Windows, CI is ubuntu, and they disagree on paths, case and line endings.

## Committing on Windows

**Always `git commit -F <file>`. Never `-m`.** PowerShell mangles quotes, em
dashes and newlines before git sees them; the observed failure is
`error: unknown option` with no commit. PowerShell has no heredocs, and `&&` is
a parser error in 5.1 — use `;`.

- 🔴 **Never `git stash` / `stash pop` in this tree.** There are **4
  pre-existing stash entries** here (measured 2026-09-07) belonging to nobody in
  this session; a `pop` lands the wrong one. To ask "is this pre-existing?", use
  a throwaway `git worktree add`.
- **Stage an explicit allowlist of paths, never `git add -A`** — a scheduled
  pipeline run or another agent may be mid-work in the same tree. The pipeline's
  own STEP 6 does exactly this.
- ⚠️ **Doc encodings differ and are load-bearing:** `docs/DONE.md` carries a
  UTF-8 **BOM**, `docs/TODO.md` does not. Write docs via a temp file + rename;
  rewriting one through PowerShell can silently cp1252-round-trip it (it has
  happened here — `docs/KNOWN_ISSUES.md`, 86 mojibake lines).
- `git pull` can intermittently fail with "unable to write new index file" —
  the repo is inside OneDrive.

## Secrets, and one rule that does NOT apply here

Never read, print or paste the contents of `.env` / `.env.*`, `.dev.vars*`,
`docs/access/CREDENTIALS.md`, or anything under `docs/access/keys/`. Names only,
everywhere; `CREDENTIALS.md` is LOCAL ONLY and must never be tracked.

The estate's "every catalog change lands on **both** instances" rule belongs to
the **library catalog** (`bookbuddy/library_catalog`, `[env.friend]`). This repo
publishes a single site — no paired deploy, migrate or sweep. Do not invent one.
