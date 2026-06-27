"""
Pull all config from GCP Secret Manager and write it into the local .env file.

Usage:
    python scripts/setup_env_from_gcp.py

Requires:
    gcloud auth application-default login   (one-time per machine)
    pip install -r requirements.txt

Only ROOT_DIR (and optionally INSPECT_DIR) are machine-specific and will be
prompted for interactively. Everything else comes from GCP automatically.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
GCP_PROJECT = "audiobook-catalog"

# GCP secret name → .env key written to disk.
# All values here are the same on every machine.
SECRETS: dict[str, str] = {
    # Tokens / credentials
    "HARDCOVER_TOKEN":  "HARDCOVER_TOKEN",
    "GITHUB_TOKEN":     "GITHUB_TOKEN",
    "CLAUDE_API_KEY":   "Claude-llm",
    # Project config
    "OUTPUT_DIR":        "OUTPUT_DIR",
    "SITE_DIR":          "SITE_DIR",
    "SITE_INDEX_NAME":   "SITE_INDEX_NAME",
    "SITE_CSV_NAME":     "SITE_CSV_NAME",
    "DRIVE_FOLDER_URL":  "DRIVE_FOLDER_URL",
    "DRIVE_AUTHORS_ROOT":"DRIVE_AUTHORS_ROOT",
    "AUTHOR_DRIVE_MAP":  "AUTHOR_DRIVE_MAP",
    "CSV_LINK":          "CSV_LINK",
    "EMAIL":             "Email",
    "GITHUB_USER":       "GITHUB_USER",
    "HARDCOVER_ENABLED": "HARDCOVER_ENABLED",
}

# Keys that differ per machine. Script will prompt if not already set in .env.
MACHINE_SPECIFIC: list[tuple[str, str]] = [
    ("ROOT_DIR",    "Path to your local audiobook library (e.g. C:\\Users\\you\\OpenAudible\\books)"),
    ("INSPECT_DIR", "Optional subfolder to inspect instead of ROOT_DIR — press Enter to leave blank"),
]


def fetch_secret(client, name: str) -> str | None:
    resource = f"projects/{GCP_PROJECT}/secrets/{name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": resource}, timeout=5.0)
        return response.payload.data.decode().strip() or None
    except Exception as exc:
        print(f"  [skip] {name}: {exc}")
        return None


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def write_env(path: Path, data: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in data.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    try:
        from google.cloud import secretmanager  # type: ignore
    except ImportError:
        print("google-cloud-secret-manager is not installed.")
        print("Run: pip install -r requirements.txt")
        sys.exit(1)

    print(f"Connecting to GCP project '{GCP_PROJECT}'...")
    try:
        client = secretmanager.SecretManagerServiceClient()
    except Exception as exc:
        print(f"Failed to connect: {exc}")
        print("Run: gcloud auth application-default login")
        sys.exit(1)

    env = load_env(ENV_FILE)
    fetched = 0

    print("\nFetching from GCP Secret Manager...")
    for secret_name, env_key in SECRETS.items():
        print(f"  {secret_name} → {env_key} ...", end=" ", flush=True)
        value = fetch_secret(client, secret_name)
        if value:
            env[env_key] = value
            print("OK")
            fetched += 1
        else:
            print("not found")

    print(f"\n{fetched}/{len(SECRETS)} secrets fetched.")

    print("\nMachine-specific config (not stored in GCP):")
    for key, prompt in MACHINE_SPECIFIC:
        current = env.get(key, "")
        if current:
            print(f"  {key} already set — keeping: {current}")
        else:
            value = input(f"  {prompt}\n  {key}= ").strip()
            env[key] = value

    write_env(ENV_FILE, env)
    print(f"\n.env written to {ENV_FILE}")
    print("Done. Run 'python -m app.main' to build the catalog.")


if __name__ == "__main__":
    main()
