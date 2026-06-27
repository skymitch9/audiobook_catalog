# Audiobook Catalog — Setup Guide

Everything needed to get this running on a new machine. No chat history required.

---

## Prerequisites

- Python 3.12+
- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`winget install Google.CloudSDK`)
- A Google account with access to the `audiobook-catalog` GCP project
- OpenAudible installed with your library at a known local path

---

## New Machine Setup (3 steps)

### 1. Install dependencies

```powershell
pip install -r requirements.txt
```

### 2. Authenticate with GCP (opens browser — one time per machine)

```powershell
gcloud auth application-default login
```

Sign in with the Google account that owns the `audiobook-catalog` Firebase/GCP project.

### 3. Generate your local .env from GCP

```powershell
python scripts/setup_env_from_gcp.py
```

The script fetches all config and secrets from GCP automatically. It will then prompt you for the two machine-specific values that can't be stored centrally:

```
ROOT_DIR=    ← path to your OpenAudible books folder on this machine
INSPECT_DIR= ← optional subfolder, press Enter to leave blank
```

Example `ROOT_DIR` values:
- Windows: `C:\Users\yourname\OpenAudible\books`
- Mac: `/Users/yourname/Music/OpenAudible/books`

That's it. Run the catalog:

```powershell
python -m app.main
```

---

## What's Stored in GCP Secret Manager

**Project:** `audiobook-catalog`

Everything below is fetched automatically by `setup_env_from_gcp.py`. You never need to copy these manually.

| GCP Secret | .env key | What it is |
|---|---|---|
| `HARDCOVER_TOKEN` | `HARDCOVER_TOKEN` | Hardcover.app API JWT |
| `GITHUB_TOKEN` | `GITHUB_TOKEN` | GitHub Personal Access Token |
| `CLAUDE_API_KEY` | `Claude-llm` | Anthropic API key |
| `OUTPUT_DIR` | `OUTPUT_DIR` | Build output folder (relative to project) |
| `SITE_DIR` | `SITE_DIR` | Generated HTML site folder |
| `SITE_INDEX_NAME` | `SITE_INDEX_NAME` | HTML index filename |
| `SITE_CSV_NAME` | `SITE_CSV_NAME` | Catalog CSV filename |
| `DRIVE_FOLDER_URL` | `DRIVE_FOLDER_URL` | Google Drive shared folder link |
| `DRIVE_AUTHORS_ROOT` | `DRIVE_AUTHORS_ROOT` | Root Drive folder for per-author subfolders |
| `AUTHOR_DRIVE_MAP` | `AUTHOR_DRIVE_MAP` | Path to author → Drive folder ID map |
| `CSV_LINK` | `CSV_LINK` | Public URL path to the catalog CSV |
| `EMAIL` | `Email` | Owner email |
| `GITHUB_USER` | `GITHUB_USER` | GitHub username |
| `HARDCOVER_ENABLED` | `HARDCOVER_ENABLED` | Whether to run Hardcover enrichment at build time |

### Updating a value in GCP

To update any secret (e.g. when a token expires):

```powershell
# Option A — GCP Console (easiest)
# Go to: https://console.cloud.google.com/security/secret-manager?project=audiobook-catalog
# Click the secret → Add Version → paste new value

# Option B — gcloud CLI
echo -n "new-value" | gcloud secrets versions add SECRET_NAME --data-file=- --project=audiobook-catalog
```

Then re-run `python scripts/setup_env_from_gcp.py` to pull the new value into your local `.env`.

### Adding a new secret

1. Upload it to GCP:
   ```powershell
   echo -n "the-value" | gcloud secrets create SECRET_NAME --data-file=- --project=audiobook-catalog
   ```

2. Add the mapping to `SECRETS` in [scripts/setup_env_from_gcp.py](../scripts/setup_env_from_gcp.py):
   ```python
   "SECRET_NAME": "ENV_KEY_NAME",
   ```

---

## Where Tokens Come From

| Token | Where to get it |
|---|---|
| `HARDCOVER_TOKEN` | hardcover.app → your avatar → Settings → API → copy the token. Paste just the `eyJ...` part, not the `Bearer ` prefix. |
| `GITHUB_TOKEN` | github.com → Settings → Developer settings → Personal access tokens → Fine-grained. Needs `Contents` write + `Pages` write on the `audiobook_catalog` repo. |
| `CLAUDE_API_KEY` | console.anthropic.com → API Keys |

---

## Hardcover Enrichment

At build time, `python -m app.main` calls the Hardcover GraphQL API to match each audiobook and fill in community rating, description, genre, release year, cover image, and series info.

- Controlled by `HARDCOVER_ENABLED=true` in `.env`
- Results cached in `.cache/hardcover.json` — repeat builds skip already-matched books
- Token expires periodically — get a new one from hardcover.app → Settings → API, update it in GCP, re-run setup script

---

## Troubleshooting

**`setup_env_from_gcp.py` says "not found" for secrets**
Run `gcloud auth application-default login` again — credentials may have expired (they last ~12 hours without refresh).

**Hardcover returns 401**
Token expired. Get a new one from hardcover.app → Settings → API. Update in GCP Secret Manager, then re-run `python scripts/setup_env_from_gcp.py`.

**`ROOT_DIR` not found at build time**
Set `ROOT_DIR` in your `.env` to the local path where OpenAudible saves books. This is the only value that differs per machine.

**`HARDCOVER_ENABLED` is true but no enrichment runs**
Verify the token loaded: `python -c "from app.config import HARDCOVER_TOKEN; print(bool(HARDCOVER_TOKEN))"`.
