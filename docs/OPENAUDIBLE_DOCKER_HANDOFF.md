# OpenAudible Docker Pilot — Handoff Notes

Last checked: 2026-06-29 (America/Phoenix)

## Goal

Run OpenAudible in Docker, periodically refresh the Audible library, and feed
completed M4B files into the existing Drive upload/catalog publishing pipeline.

## Current runtime state

- Docker Desktop is running.
- Container `openaudible` is running at <http://127.0.0.1:3000>.
- Port 3000 is bound to `127.0.0.1` only; it is not exposed to the LAN.
- OpenAudible 4.8.6 rendered successfully in the local browser.
- The pilot uses `runtime/openaudible`, not the live desktop directory.
- Pilot status at last check: disconnected, demo license, one welcome book.
- `openaudible-scheduler` and `audiobook-sync` are not running. They were only
  built/created for validation and then removed to prevent Drive/GitHub changes.
- Local images for OpenAudible, the Python scheduler, and `audiobook-sync` have
  been pulled/built successfully.

Check current state with:

```powershell
docker compose -f docker-compose.sync.yml --profile automation ps -a
docker logs --tail 100 openaudible
```

## Git state

All pilot source, configuration, documentation, and tests were consolidated and
committed on:

```text
codex/openaudible-docker-pilot
```

The original worktree remains on `main`; the consolidation was performed in a
separate temporary worktree so newer catalog work was not overwritten. To resume:

```powershell
git status --short
git switch codex/openaudible-docker-pilot
```

If local `main` work prevents switching, preserve that work first rather than
resetting or cleaning it. Never stage or commit `runtime/`; it may contain Audible
credentials, license data, metadata, and audiobook files.

## What was implemented

The Compose update defines three services:

1. `openaudible` — safe default service, localhost-only UI, stable releases,
   isolated bind-mounted data.
2. `openaudible-scheduler` — automation-profile controller that writes
   `["Sync_Quick"]` to OpenAudible's current `commands.json` interface every six
   hours. Download/convert behavior remains controlled by OpenAudible preferences.
3. `audiobook-sync` — existing hourly Drive/catalog worker sharing the pilot's
   `books` directory.

Other changes:

- Upload manifest/cache state moves to persistent `/app/data`.
- The uploader ignores audio modified within the last five minutes so it cannot
  upload a file while OpenAudible is still converting it.
- Runtime data is excluded from both Git and Docker build context.
- `.env.example` documents local Docker settings.

## Validation completed

- Full Python suite: `77 passed`.
- Focused scheduler/file-age tests: `4 passed`.
- `docker compose ... config --quiet`: passed.
- OpenAudible UI: verified in the local browser.
- `audiobook-sync` image: built successfully.
- Scheduler and worker mounts: inspected successfully.
- Worker configuration verified as:
  - manifest: `/app/data/upload_manifest.json`
  - minimum file age: `300` seconds

Repeat validation after restoring the stash:

```powershell
New-Item -ItemType Directory -Force runtime/openaudible/books | Out-Null
python -m pytest -q
docker compose -f docker-compose.sync.yml config --quiet
docker compose -f docker-compose.sync.yml --profile automation build audiobook-sync
```

## Next test

1. Open <http://127.0.0.1:3000>.
2. Connect one Audible account manually. Handle password, MFA, and any CAPTCHA in
   the browser yourself.
3. Activate the existing OpenAudible license in the isolated pilot if needed.
4. Download and convert exactly one owned test book to M4B.
5. Confirm the completed file appears under `runtime/openaudible/books` and its
   timestamp is older than five minutes.
6. Run the upload worker in dry-run mode before enabling the automation profile.
7. Verify the expected author folder, Drive destination, catalog output, and that
   no partial or duplicate file is selected.

Do not point `OPENAUDIBLE_DATA_DIR` at `C:\Users\nbasl\OpenAudible` while the
desktop OpenAudible process is running. Both installations maintain database and
lock files, and simultaneous access risks corruption.

## Enabling full automation later

After the single-book test succeeds:

1. Review the OpenAudible backlog before enabling automatic downloads. The live
   desktop installation previously reported hundreds of downloadable titles, so
   enabling everything blindly could consume substantial disk, bandwidth, and
   Drive space.
2. Enable the desired automatic download and automatic M4B conversion preferences
   inside Dockerized OpenAudible.
3. Confirm `.env`, Google OAuth files, GitHub credentials, and Drive destination.
4. Start the full profile:

```powershell
docker compose -f docker-compose.sync.yml --profile automation up -d
docker compose -f docker-compose.sync.yml --profile automation logs -f
```

This starts externally mutating behavior: Audible sync/download, Google Drive
upload/download, catalog regeneration, Git commits/pushes, deployment, and Discord
notifications. Monitor the first complete cycle before leaving it unattended.

## Stop commands

Stop only the UI pilot:

```powershell
docker compose -f docker-compose.sync.yml stop openaudible
```

Stop the complete stack without deleting pilot data:

```powershell
docker compose -f docker-compose.sync.yml --profile automation down
```

The bind-mounted pilot data remains under `runtime/openaudible` after containers
are removed.
