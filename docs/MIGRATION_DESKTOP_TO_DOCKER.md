# Migrating desktop OpenAudible to Docker — adopt-in-place (no copy)

The library is ~650 GB. Do NOT copy or move it. The Docker container adopts the
existing desktop data directory via a bind mount, and the sync worker ends up
watching the exact same `books/` folder it watches today.

The single hard rule: **the desktop app and the container must never run at
the same time** against this directory. Sharing is sequential, not concurrent.

## How it works

`.env`:

```
OPENAUDIBLE_DATA_DIR=C:\Users\nbasl\OpenAudible
```

- `openaudible` container mounts it at `/config/OpenAudible` (its whole data dir).
- `audiobook-sync` mounts `${OPENAUDIBLE_DATA_DIR}/books` at `/library` —
  which is `C:\Users\nbasl\OpenAudible\books`, the same path the pipeline
  used before Dockerization. No manifest churn, no re-uploads from a path change.

## Phase 0 — safety net (before anything)

1. Verify Drive holds a complete copy of the library (this is the real backup
   of the 650 GB — no local second copy is needed or possible):
   `python scripts/audit_drive_vs_local.py` (report only, no --fix).
2. Back up the small metadata (everything except `books/` and `aax/`, ~120 MB):
   books.json, books_backup.json, settings.json, credentials.json, license.json,
   web/, art/. Zip it somewhere outside `C:\Users\nbasl\OpenAudible`.
   This is the instant-rollback for anything the Linux app rewrites.
3. The `books/` files themselves are only read/added by OpenAudible in normal
   operation; the risk surface of a handover is the metadata, which is backed up.

## Phase 1 — smoke test on a scratch dir (uses ~1 book of disk)

1. Leave `OPENAUDIBLE_DATA_DIR` at its default (`./runtime/openaudible`).
2. `docker compose -f docker-compose.sync.yml up -d openaudible`
3. Sign in at http://127.0.0.1:3000, download + convert ONE book end to end.
4. Confirm the converted file lands in `runtime/openaudible/books/<Author>/…`
   (this validates the `/books` layout assumption) and that `status.json`
   shows empty `queues` when idle.
5. `docker compose -f docker-compose.sync.yml down`, delete `runtime/`.

## Phase 2 — cutover (minutes, reversible)

1. Quit desktop OpenAudible. Disable its auto-start.
2. Set `OPENAUDIBLE_DATA_DIR=C:\Users\nbasl\OpenAudible` in `.env`.
3. `docker compose -f docker-compose.sync.yml up -d openaudible`
4. In the container UI, check Preferences → folders. The desktop settings.json
   contains Windows paths (`C:\…`); the Linux app needs them to resolve under
   `/config/OpenAudible` (books → `/config/OpenAudible/books`). Fix if needed —
   this edits metadata only, never the audio files.
5. Confirm the full library (~988 books) is listed and covers/art render.

Rollback at any point: stop the container, restore the metadata zip if the
Linux app rewrote paths, reopen desktop OpenAudible. The books were never touched.

## Phase 3 — enable automation

1. Seed the container manifest so the worker doesn't treat all 988 books as new
   (which would mass re-upload to Drive): after the first
   `--profile automation up`, copy the current manifest into the volume:
   `docker cp scripts/upload_manifest.json audiobook-sync:/app/data/upload_manifest.json`
   then restart the sync container. (Do this BEFORE its first hourly cron run;
   the initial run on container start is the one to beat — start the container,
   cp immediately, restart.) Safer order: create the volume first via
   `docker compose ... create audiobook-sync`, cp, then `up`.
2. `docker compose -f docker-compose.sync.yml --profile automation up -d`
3. Watch one full cycle: Quick Sync queued → download/convert → file settles
   (MIN_FILE_AGE_SECONDS) → upload → catalog rebuild → Discord ping.

## Optional hardening during the trial period

Mount the library read-only for the sync worker until trust is established
(disables `audit_drive_vs_local.py --fix` restores, nothing else):
change the `/library` bind to add `read_only: true`, or run the audit
manually from Windows in the meantime.
