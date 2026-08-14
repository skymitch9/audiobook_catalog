# app/club_announcements.py
"""
Club Discord announcements — server-side consumer of the reading-schedule
contract (backlog #2; the contract shipped with backlog #1, 2026-08-14).

For every club with the per-club feature `discordAnnouncements` enabled AND a
webhook saved, detect announceable events since the last run and post tasteful
embeds to the CLUB'S OWN webhook:

  - schedule  : the read's reading schedule was set/changed
                (`scheduleUpdatedAt` on the read doc newer than last announce)
  - due       : one or more milestone due dates arrived (`milestones[].dueAt`
                now past) — all newly-due sections batch into ONE embed,
                with an on-track/behind summary from the read's `progress`
                subcollection
  - started   : a new active read appeared (read docs carry `startedAt`)
  - finished  : an active read's `status` flipped to 'finished'
                ('abandoned' is recorded silently — no fanfare for a DNF)

Why this is PIPELINE-side Python and not browser code: the webhook URL is a
capability (anyone holding it can post to the club's channel), so it lives in
`clubs/{id}/settings/discord`, which firestore.rules makes browser-UNREADABLE
(`allow read: if false`). Only the service account bypasses rules — the same
pattern as app/pipeline_status.py, whose credential plumbing this reuses
(scripts/firebase_service_account.json or $FIREBASE_SERVICE_ACCOUNT; see
docs/access/FIREBASE.md).

State lives server-side in `clubs/{id}/settings/announceState` — under the
same rules block as the webhook, so it is browser-unreadable too, and browsers
cannot forge it either (settings create/update requires the webhookUrl shape).
No rules change was needed. Shape:

  {
    "lastRunAt": iso-utc,
    "reads": { readId: { "status": str, "scheduleAnnouncedMs": int,
                         "duePositions": [int, ...] } },
    "consecutiveFailures": int,   # webhook posts failing back-to-back
    "lastError": str | None,
  }

Rate safety: at most MAX_EMBEDS_PER_RUN embeds per club per run, all sent in a
single webhook call. The FIRST run for a club establishes a silent baseline —
existing reads, schedules and past due dates are recorded, nothing is posted —
so enabling the feature never floods the channel with history.

Failure posture (mirrors app/index_push.py / app/pipeline_status.py):
  - No service account / firebase-admin → one log line, nothing else.
  - A dead or revoked webhook must not cost a run: log, skip the club,
    bump `consecutiveFailures` in the state doc (surfaceable later; the
    club's own setting is NEVER auto-cleared), and — because event markers
    are only recorded after a successful post — retry next run.
  - app/main.py wraps the whole step in try/except as well (soft step).

Manual runs (the main verification path — never post to a real webhook in
testing):

    python -m app.club_announcements --dry-run                # prod clubs
    PIPELINE_LANE=dev python -m app.club_announcements --dry-run   # clubs_dev
    python -m app.club_announcements --dry-run --lane dev          # same

Scheduled: wired as a soft step in app/main.py, so it runs on the existing
8-hour pipeline cadence.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Same source + default as app/index_push.py and send_discord_notification.py —
# the repo variable SITE_URL wins when set. Links always point at the live site.
DEFAULT_SITE_URL = "https://audiobooks.heygabi.ai/"

FEATURE_KEY = "discordAnnouncements"   # mirrors site/clubs.js FEATURE_DEFAULTS (default OFF)
STATE_DOC_ID = "announceState"         # clubs/{id}/settings/announceState — browser-unreadable
WEBHOOK_DOC_ID = "discord"             # clubs/{id}/settings/discord — {webhookUrl, ...}

MAX_EMBEDS_PER_RUN = 6                 # a handful per club per run; Discord allows 10/message

COLOR_BLUE = 5814783                   # matches send_discord_notification.py
COLOR_GREEN = 3066993
COLOR_AMBER = 15105570


def _lane_suffix() -> str:
    """'' for prod, '_dev' for dev runs — same knob as app/pipeline_status.py."""
    return "_dev" if os.getenv("PIPELINE_LANE", "").lower() == "dev" else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ts_millis(value: Any) -> Optional[int]:
    """Firestore timestamp (datetime) or epoch millis number → int millis, else None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return None


def feature_enabled(club: Dict[str, Any]) -> bool:
    """Python mirror of site/clubs.js clubFeatureEnabled() for THIS key (default OFF)."""
    feats = club.get("features")
    if isinstance(feats, dict) and FEATURE_KEY in feats:
        return bool(feats[FEATURE_KEY])
    return False


def club_page_url(club_id: str, lane_suffix: str = "", site_url: Optional[str] = None) -> str:
    """Deep link to the club page on the live site (dev-lane clubs → /dev/)."""
    site = (site_url or os.environ.get("SITE_URL") or DEFAULT_SITE_URL).rstrip("/")
    lane = "/dev" if lane_suffix else ""
    from urllib.parse import quote

    return f"{site}{lane}/club.html?id={quote(club_id, safe='')}"


# ---------------------------------------------------------------------------
# Pure schedule maths — Python mirrors of site/club-reads.js
# ---------------------------------------------------------------------------

def _due_of(m: Dict[str, Any]) -> Optional[int]:
    due = m.get("dueAt")
    return int(due) if isinstance(due, (int, float)) and not isinstance(due, bool) else None


def past_due_positions(milestones: List[Dict[str, Any]], now_ms: int) -> List[int]:
    """Positions of every milestone whose dueAt has passed."""
    out = []
    for m in milestones or []:
        due = _due_of(m)
        if due is not None and due <= now_ms:
            out.append(int(m.get("position", 0)))
    return sorted(set(out))


def next_due_milestone(milestones: List[Dict[str, Any]], now_ms: int) -> Optional[Dict[str, Any]]:
    """Mirror of nextDueMilestone(): the upcoming milestone with the smallest future dueAt."""
    best = None
    for m in milestones or []:
        due = _due_of(m)
        if due is not None and due > now_ms and (best is None or due < _due_of(best)):
            best = m
    return best


def member_position(milestones: List[Dict[str, Any]], progress: Dict[str, Any]) -> int:
    """
    Mirror of memberSchedulePosition(): effective milestone position from a
    progress doc. Chapter-mapped milestones (chEnd) count as complete once
    chapterIndex reaches chEnd; else milestonePosition. -1 = not started.
    """
    mlist = milestones or []
    last = max((int(m.get("position", 0)) for m in mlist), default=-1)
    if not progress:
        return -1
    if progress.get("finished"):
        return last
    chaptered = any(isinstance(m.get("chEnd"), (int, float)) for m in mlist)
    ch = progress.get("chapterIndex")
    if chaptered and isinstance(ch, (int, float)) and not isinstance(ch, bool):
        best = -1
        for m in mlist:
            end = m.get("chEnd")
            if isinstance(end, (int, float)) and ch >= end and int(m.get("position", 0)) > best:
                best = int(m.get("position", 0))
        return best
    pos = progress.get("milestonePosition")
    return int(pos) if isinstance(pos, (int, float)) and not isinstance(pos, bool) else -1


def on_track_summary(milestones: List[Dict[str, Any]], progress_docs: List[Dict[str, Any]], now_ms: int) -> Optional[str]:
    """
    'N of M readers on track' against the schedule (mirrors scheduleStatus():
    a member is on track when no past-due milestone is beyond their position).
    None when nobody has a progress doc yet.
    """
    if not progress_docs:
        return None
    due_positions = past_due_positions(milestones, now_ms)
    on_track = 0
    for p in progress_docs:
        pos = member_position(milestones, p)
        behind_by = sum(1 for d in due_positions if d > pos)
        if behind_by == 0:
            on_track += 1
    total = len(progress_docs)
    return f"{on_track} of {total} reader{'s' if total != 1 else ''} on track"


# ---------------------------------------------------------------------------
# Event detection (pure — unit tested without Firestore)
# ---------------------------------------------------------------------------

def baseline_entry(read: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    """State entry that marks everything CURRENT about a read as already seen."""
    return {
        "status": read.get("status") or "active",
        "scheduleAnnouncedMs": ts_millis(read.get("scheduleUpdatedAt")) or 0,
        "duePositions": past_due_positions(read.get("milestones") or [], now_ms),
    }


def detect_read_events(
    read: Dict[str, Any], prior: Optional[Dict[str, Any]], now_ms: int
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Compare one read doc against its prior state entry.
    Returns (events, new_entry). `prior=None` means the read is new since the
    last run. Events carry everything the embed builders need.
    """
    events: List[Dict[str, Any]] = []
    milestones = read.get("milestones") or []
    status = read.get("status") or "active"
    sched_ms = ts_millis(read.get("scheduleUpdatedAt")) or 0
    entry = {
        "status": status,
        "scheduleAnnouncedMs": (prior or {}).get("scheduleAnnouncedMs", 0),
        "duePositions": list((prior or {}).get("duePositions", [])),
    }

    if prior is None:
        # New read since last run. Announce the start only if it is (still)
        # active; a read that appeared AND finished between runs gets the
        # finish announcement alone.
        if status == "active":
            events.append({"type": "started", "read": read})
        elif status == "finished":
            events.append({"type": "finished", "read": read})
        # Everything already past or already scheduled is part of the birth
        # state — the 'started' embed mentions the schedule if one exists.
        entry["scheduleAnnouncedMs"] = sched_ms
        entry["duePositions"] = past_due_positions(milestones, now_ms)
        return events, entry

    if prior.get("status") == "active" and status == "finished":
        events.append({"type": "finished", "read": read})
    # 'abandoned' is recorded (entry["status"]) but never announced.

    schedule_changed = sched_ms > entry["scheduleAnnouncedMs"]
    if schedule_changed and status == "active":
        events.append({"type": "schedule", "read": read})
        entry["scheduleAnnouncedMs"] = sched_ms

    newly_due = [
        m for m in milestones
        if _due_of(m) is not None and _due_of(m) <= now_ms
        and int(m.get("position", 0)) not in set(entry["duePositions"])
    ]
    if newly_due:
        # A schedule embed already shows where the club stands; folding the
        # newly-past sections into its marker avoids a same-run double post.
        if not schedule_changed and status == "active":
            events.append({"type": "due", "read": read, "milestones": sorted(newly_due, key=lambda m: m.get("position", 0))})
        entry["duePositions"] = sorted(set(entry["duePositions"]) | {int(m.get("position", 0)) for m in newly_due})

    return events, entry


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _book_line(read: Dict[str, Any]) -> str:
    title = read.get("bookTitle") or "a book"
    author = (read.get("bookAuthor") or "").strip()
    return f"**{title}**" + (f" by {author}" if author else "")


def _clip(text: str, limit: int = 4096) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _thumbnail(read: Dict[str, Any]) -> Optional[Dict[str, str]]:
    cover = (read.get("coverHref") or "").strip()
    return {"url": cover} if cover.startswith("http") else None


def build_embed(event: Dict[str, Any], club: Dict[str, Any], link: str, now_ms: int,
                progress_docs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One Discord embed per event. Kept tasteful: title, short body, club link."""
    read = event["read"]
    club_name = club.get("name") or "your club"
    milestones = read.get("milestones") or []
    kind = event["type"]
    fields = [{"name": "Club", "value": f"[{club_name}]({link})", "inline": True}]

    if kind == "started":
        embed = {
            "title": f"📖 New club read — {read.get('bookTitle') or 'a book'}",
            "description": _clip(f"{club_name} started {_book_line(read)}. {len(milestones)} section{'s' if len(milestones) != 1 else ''} to talk about."),
            "color": COLOR_GREEN,
        }
        nxt = next_due_milestone(milestones, now_ms)
        if nxt:
            fields.append({"name": "First due date", "value": f"{nxt.get('label', '?')} — <t:{_due_of(nxt) // 1000}:D>", "inline": True})
    elif kind == "finished":
        embed = {
            "title": f"🎉 Finished — {read.get('bookTitle') or 'a book'}",
            "description": _clip(f"{club_name} finished {_book_line(read)}. Time for wrap-up ratings!"),
            "color": COLOR_GREEN,
        }
    elif kind == "schedule":
        dated = sum(1 for m in milestones if _due_of(m) is not None)
        lines = [f"The reading schedule for {_book_line(read)} was updated — {dated} section{'s' if dated != 1 else ''} now dated."]
        nxt = next_due_milestone(milestones, now_ms)
        if nxt:
            lines.append(f"Next up: **{nxt.get('label', '?')}** by <t:{_due_of(nxt) // 1000}:D>.")
        embed = {"title": f"📅 Schedule updated — {read.get('bookTitle') or 'a book'}",
                 "description": _clip("\n".join(lines)), "color": COLOR_BLUE}
    else:  # due
        due_ms = event["milestones"]
        labels = "\n".join(f"• **{m.get('label', '?')}**" for m in due_ms[:10])
        many = f"\n…and {len(due_ms) - 10} more." if len(due_ms) > 10 else ""
        head = "This section is due" if len(due_ms) == 1 else f"These {len(due_ms)} sections are due"
        lines = [f"{head} for {_book_line(read)}:", labels + many]
        nxt = next_due_milestone(milestones, now_ms)
        if nxt:
            lines.append(f"\nNext up: **{nxt.get('label', '?')}** by <t:{_due_of(nxt) // 1000}:D>.")
        summary = on_track_summary(milestones, progress_docs or [], now_ms)
        embed = {"title": f"⏰ Reading check-in — {read.get('bookTitle') or 'a book'}",
                 "description": _clip("\n".join(lines)), "color": COLOR_AMBER}
        if summary:
            fields.append({"name": "Pace", "value": summary, "inline": True})

    embed["fields"] = fields
    thumb = _thumbnail(read)
    if thumb:
        embed["thumbnail"] = thumb
    embed["timestamp"] = _now_iso()
    return embed


# ---------------------------------------------------------------------------
# Firestore access (service account — bypasses rules by design)
# ---------------------------------------------------------------------------

class FirestoreClubs:
    """Thin data-access wrapper so the engine and tests never touch the SDK."""

    def __init__(self, db, collection_name: str):
        self._db = db
        self.collection_name = collection_name

    def clubs(self) -> List[Dict[str, Any]]:
        out = []
        for snap in self._db.collection(self.collection_name).stream():
            data = snap.to_dict() or {}
            data["id"] = snap.id
            out.append(data)
        return out

    def _settings_doc(self, club_id: str, doc_id: str):
        return self._db.collection(self.collection_name).document(club_id).collection("settings").document(doc_id)

    def webhook_url(self, club_id: str) -> str:
        snap = self._settings_doc(club_id, WEBHOOK_DOC_ID).get()
        data = snap.to_dict() if snap.exists else None
        return (data or {}).get("webhookUrl", "") or ""

    def state(self, club_id: str) -> Optional[Dict[str, Any]]:
        snap = self._settings_doc(club_id, STATE_DOC_ID).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def save_state(self, club_id: str, state: Dict[str, Any]) -> None:
        self._settings_doc(club_id, STATE_DOC_ID).set(state)

    def reads(self, club_id: str) -> List[Dict[str, Any]]:
        out = []
        ref = self._db.collection(self.collection_name).document(club_id).collection("reads")
        for snap in ref.stream():
            data = snap.to_dict() or {}
            data["id"] = snap.id
            out.append(data)
        return out

    def progress(self, club_id: str, read_id: str) -> List[Dict[str, Any]]:
        ref = (self._db.collection(self.collection_name).document(club_id)
               .collection("reads").document(read_id).collection("progress"))
        return [snap.to_dict() or {} for snap in ref.stream()]


def _firestore_client():
    """Same credential plumbing as app/pipeline_status.py. Returns (db, why_not)."""
    from pathlib import Path

    from app.config import PROJECT_ROOT

    key_path = Path(os.getenv("FIREBASE_SERVICE_ACCOUNT") or (PROJECT_ROOT / "scripts" / "firebase_service_account.json"))
    if not key_path.exists():
        return None, f"no service account at {key_path}"
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(str(key_path)))
        return firestore.client(), None
    except ImportError:
        return None, "firebase-admin not installed"
    except Exception as e:  # bad key, no network, project mismatch …
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Webhook posting
# ---------------------------------------------------------------------------

def post_webhook(webhook_url: str, embeds: List[Dict[str, Any]], timeout: int = 30) -> None:
    """One webhook call per club per run. Raises on any failure."""
    import requests

    resp = requests.post(webhook_url, json={"embeds": embeds},
                         headers={"Content-Type": "application/json"}, timeout=timeout)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"webhook returned HTTP {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def plan_club(source: FirestoreClubs, club: Dict[str, Any], now_ms: int,
              lane_suffix: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """
    Work out what one club would post.
    Returns (embeds, new_state, is_baseline). new_state=None → nothing to save.
    """
    club_id = club["id"]
    prior_state = source.state(club_id)
    reads = source.reads(club_id)
    link = club_page_url(club_id, lane_suffix)

    if prior_state is None:
        # First contact: record everything as seen, announce nothing.
        state = {
            "lastRunAt": _now_iso(),
            "reads": {r["id"]: baseline_entry(r, now_ms) for r in reads},
            "consecutiveFailures": 0,
            "lastError": None,
        }
        return [], state, True

    prior_reads = prior_state.get("reads") or {}
    embeds: List[Dict[str, Any]] = []
    new_reads_state: Dict[str, Any] = {}
    for read in reads:
        events, entry = detect_read_events(read, prior_reads.get(read["id"]), now_ms)
        new_reads_state[read["id"]] = entry
        for event in events:
            progress = source.progress(club_id, read["id"]) if event["type"] == "due" else None
            embeds.append(build_embed(event, club, link, now_ms, progress))
    # Reads deleted from Firestore fall out of the state doc naturally.

    if len(embeds) > MAX_EMBEDS_PER_RUN:
        print(f"[WARN] club '{club_id}': {len(embeds)} events this run, capping at {MAX_EMBEDS_PER_RUN}")
        embeds = embeds[:MAX_EMBEDS_PER_RUN]

    new_state = {
        "lastRunAt": _now_iso(),
        "reads": new_reads_state,
        "consecutiveFailures": prior_state.get("consecutiveFailures", 0),
        "lastError": prior_state.get("lastError"),
    }
    changed = new_reads_state != prior_reads
    return embeds, (new_state if (changed or embeds) else None), False


def run(source: FirestoreClubs, now_ms: Optional[int] = None, dry_run: bool = False,
        lane_suffix: str = "", poster=post_webhook) -> Dict[str, int]:
    """
    The whole engine pass. Returns counters for the log line.
    Marker discipline: state advances ONLY after a successful post (or when
    there was nothing to post), so a webhook failure retries next run.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    stats = {"clubs": 0, "enabled": 0, "posted": 0, "embeds": 0, "baselined": 0, "failed": 0, "skipped_no_webhook": 0}

    for club in source.clubs():
        stats["clubs"] += 1
        if not feature_enabled(club):
            continue
        stats["enabled"] += 1
        club_id = club["id"]
        label = f"{source.collection_name}/{club_id} ({club.get('name', '?')})"

        try:
            webhook = source.webhook_url(club_id)
            if not webhook:
                stats["skipped_no_webhook"] += 1
                print(f"[INFO] {label}: feature on but no webhook saved — skipping")
                continue

            embeds, new_state, is_baseline = plan_club(source, club, now_ms, lane_suffix)

            if is_baseline:
                stats["baselined"] += 1
                verb = "would be recorded (dry run)" if dry_run else "recorded"
                print(f"[INFO] {label}: first run — baseline {verb}, nothing announced")
                if not dry_run:
                    source.save_state(club_id, new_state)
                continue

            if dry_run:
                if embeds:
                    print(f"[DRY-RUN] {label}: would post {len(embeds)} embed(s):")
                    for e in embeds:
                        print(json.dumps(e, indent=2, ensure_ascii=False))
                else:
                    print(f"[DRY-RUN] {label}: nothing to announce")
                continue

            if not embeds:
                if new_state is not None:
                    source.save_state(club_id, new_state)  # silent bookkeeping (e.g. abandoned)
                continue

            try:
                poster(webhook, embeds)
            except Exception as e:  # dead/revoked webhook, network, 4xx…
                stats["failed"] += 1
                prior = source.state(club_id) or {}
                prior["consecutiveFailures"] = int(prior.get("consecutiveFailures", 0)) + 1
                prior["lastError"] = f"{_now_iso()}: {e}"[:500]
                source.save_state(club_id, prior)
                print(f"[WARN] {label}: webhook post failed ({e}) — will retry next run; "
                      f"consecutive failures: {prior['consecutiveFailures']}", file=sys.stderr)
                continue

            new_state["consecutiveFailures"] = 0
            new_state["lastError"] = None
            source.save_state(club_id, new_state)
            stats["posted"] += 1
            stats["embeds"] += len(embeds)
            print(f"[INFO] {label}: posted {len(embeds)} embed(s)")
        except Exception as e:  # one broken club must not stop the sweep
            stats["failed"] += 1
            print(f"[WARN] {label}: announcement pass failed ({e}) — continuing", file=sys.stderr)

    return stats


def announce_after_build() -> Optional[Dict[str, int]]:
    """
    The pipeline hook — called by app/main.py after the site is staged.
    No credentials → one log line, return None. Never meant to raise, and
    main.py catches anyway (the catalog build must never break on this).
    """
    db, why_not = _firestore_client()
    if db is None:
        print(f"[INFO] Club announcements skipped: {why_not}")
        return None
    suffix = _lane_suffix()
    source = FirestoreClubs(db, f"clubs{suffix}")
    stats = run(source, lane_suffix=suffix)
    print(f"[INFO] Club announcements: {stats}")
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.club_announcements",
        description="Post club event announcements to each club's own Discord webhook.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print what WOULD post (and skip all state writes); posts nothing")
    parser.add_argument("--lane", choices=["prod", "dev"], default=None,
                        help="data lane: clubs (prod, default) or clubs_dev (overrides PIPELINE_LANE)")
    args = parser.parse_args(argv)

    if args.lane is not None:
        os.environ["PIPELINE_LANE"] = "dev" if args.lane == "dev" else ""

    db, why_not = _firestore_client()
    if db is None:
        print(f"[ERROR] {why_not}", file=sys.stderr)
        return 2

    suffix = _lane_suffix()
    source = FirestoreClubs(db, f"clubs{suffix}")
    stats = run(source, dry_run=args.dry_run, lane_suffix=suffix)
    print(f"[INFO] {'dry run — nothing posted or saved' if args.dry_run else 'done'}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
