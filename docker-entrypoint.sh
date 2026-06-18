#!/bin/bash
set -e

echo "=== Audiobook Sync Pipeline (Docker) ==="
echo "Started at: $(date)"

# --- Git configuration ---
if [ -z "$GITHUB_TOKEN" ]; then
    echo "  [ERROR] GITHUB_TOKEN not set. Cannot push."
    echo "  Generate one at: https://github.com/settings/tokens"
    echo "  Scope needed: repo (Full control of private repositories)"
    echo ""
    echo "  Add to .env: GITHUB_TOKEN=ghp_your_token_here"
    exit 1
fi

# Configure git credentials
git config --global credential.helper store
echo "https://${GITHUB_USER}:${GITHUB_TOKEN}@github.com" > ~/.git-credentials
git config --global user.email "${GIT_EMAIL:-nbaslamking@gmail.com}"
git config --global user.name "${GIT_USER:-BookBuddy Bot}"
git config --global --add safe.directory /app
echo "  Git configured: ${GIT_USER:-BookBuddy Bot} <${GIT_EMAIL:-nbaslamking@gmail.com}>"

# --- Ensure repo is up to date ---
cd /app
git remote set-url origin "https://${GITHUB_USER}:${GITHUB_TOKEN}@github.com/${GITHUB_USER}/audiobook_catalog.git" 2>/dev/null || true
git fetch origin main 2>/dev/null || true
git checkout main 2>/dev/null || true
git pull origin main 2>/dev/null || true
echo "  Repo synced to latest main."

# --- Pass environment variables to cron ---
env | grep -E '^(ROOT_DIR|Claude|Email|GITHUB_TOKEN|GITHUB_USER|GIT_EMAIL|GIT_USER|DRIVE_|DISCORD_|PATH|HOME|PYTHONPATH)' > /etc/environment

# --- Run once immediately on container start ---
echo ""
echo "=== Running initial sync... ==="
python scripts/sync_to_drive.py --upload-only 2>&1 | tee /var/log/audiobook-sync/sync.log
echo ""
echo "=== Initial sync complete. Starting hourly cron... ==="
echo "  Schedule: Every hour at :00"
echo "  Logs: docker compose -f docker-compose.sync.yml logs -f"
echo ""

# --- Start cron daemon in foreground ---
cron -f
