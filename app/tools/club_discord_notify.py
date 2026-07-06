"""
Post new book-club comments to Discord via the existing webhook.

Runs on a schedule (GitHub Actions, every 15 minutes — see
.github/workflows/club-notify.yml). Queries Firestore's public REST API for
comments created since the last interval and posts one Discord message per
comment. The webhook URL stays a repo secret — the site's client code never
sees it, so visitors can't spam the channel.

Spoiler safety: comments tagged with a chapter post their text inside
Discord spoiler bars (||like this||); untagged comments post in the clear.

Env:
  DISCORD_WEBHOOK   required — the webhook URL
  WINDOW_MINUTES    lookback window (default 16 to overlap the 15-min cron;
                    the rare overlap duplicate is harmless)
  COLLECTIONS       comma-separated club collections (default "clubs,clubs_dev")
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://firestore.googleapis.com/v1/projects/audiobook-catalog/databases/(default)/documents"
API_KEY = "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y"  # public web API key (same one the site ships)
WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "16"))
COLLECTIONS = [c.strip() for c in os.getenv("COLLECTIONS", "clubs,clubs_dev").split(",") if c.strip()]


def fetch(path):
    url = f"{BASE}/{path}?key={API_KEY}&pageSize=300"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read()).get("documents", [])


def gv(fields, name, kind="stringValue", default=""):
    return fields.get(name, {}).get(kind, default)


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_message(collection, club_name, book_title, comment_fields):
    author = gv(comment_fields, "displayName") or "Someone"
    text = gv(comment_fields, "text")
    if len(text) > 300:
        text = text[:297] + "..."
    # Discord spoiler bars break on ||; strip them from user text
    text = text.replace("||", "")
    ch = comment_fields.get("chapterIndex", {})
    tagged = "integerValue" in ch
    where = f" (@ Ch {int(ch['integerValue']) + 1})" if tagged else ""
    body = f"||{text}||" if tagged else text
    lane = " [dev]" if collection.endswith("_dev") else ""
    return f"\U0001F4AC{lane} **{author}** on *{book_title}*{where} in **{club_name}**:\n{body}"


def post_discord(message):
    data = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def collect_new_comments(cutoff):
    """Yield (collection, club_name, book_title, comment_fields, created_at)."""
    for coll in COLLECTIONS:
        try:
            clubs = fetch(coll)
        except Exception as e:
            print(f"[WARN] listing {coll} failed: {e}")
            continue
        for club in clubs:
            club_id = club["name"].split("/")[-1]
            club_name = gv(club["fields"], "name") or club_id
            try:
                reads = fetch(f"{coll}/{club_id}/reads")
            except Exception:
                continue
            for read in reads:
                read_id = read["name"].split("/")[-1]
                book_title = gv(read["fields"], "bookTitle") or "a book"
                try:
                    comments = fetch(f"{coll}/{club_id}/reads/{read_id}/comments")
                except Exception:
                    continue
                for c in comments:
                    created = parse_ts(gv(c["fields"], "createdAt", "timestampValue"))
                    if created and created >= cutoff:
                        yield coll, club_name, book_title, c["fields"], created


def main():
    if not WEBHOOK:
        print("DISCORD_WEBHOOK not set — nothing to do.")
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    new = sorted(collect_new_comments(cutoff), key=lambda x: x[4])
    print(f"comments since {cutoff.isoformat()}: {len(new)}")
    for coll, club_name, book_title, fields, _created in new:
        msg = format_message(coll, club_name, book_title, fields)
        try:
            status = post_discord(msg)
            print(f"  posted ({status}): {msg.splitlines()[0][:80]}")
        except Exception as e:
            print(f"  [WARN] post failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
