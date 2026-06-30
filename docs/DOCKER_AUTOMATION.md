# Local OpenAudible Automation Pilot

This stack keeps Dockerized OpenAudible separate from the desktop installation.
Do not point `OPENAUDIBLE_DATA_DIR` at a directory that is open in desktop
OpenAudible; both processes maintain database and lock files.

## Start the safe UI-only pilot

```powershell
New-Item -ItemType Directory -Force runtime/openaudible/books | Out-Null
docker compose -f docker-compose.sync.yml up -d openaudible
```

Open <http://127.0.0.1:3000>. On first launch, connect the Audible account and
verify that downloads and M4B conversion work with one test book.

## Enable automation

In OpenAudible Preferences, choose the desired download and conversion behavior.
Then start the complete profile:

```powershell
docker compose -f docker-compose.sync.yml --profile automation up -d
```

The scheduler requests a Quick Sync every six hours by default. OpenAudible's
own preferences decide whether newly discovered books download and convert.
The upload worker ignores files modified in the last five minutes, uploads ready
M4B files to Drive hourly, rebuilds the catalog, and pushes catalog changes.

## Stop or reset the pilot

```powershell
docker compose -f docker-compose.sync.yml --profile automation down
```

Runtime data is stored under `runtime/openaudible` and is ignored by Git. Remove
that directory only when OpenAudible is stopped and the pilot data is no longer
needed.
