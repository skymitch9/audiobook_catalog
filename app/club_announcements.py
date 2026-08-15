# app/club_announcements.py
"""
Club Discord announcements — server-side consumer of the reading-schedule
contract (backlog #2; the contract shipped with backlog #1, 2026-08-14) plus
the events created by backlog #2b (polls, blind ratings, meeting RSVP, TBR —
all 2026-08-14).

For every club with the per-club feature `discordAnnouncements` enabled AND a
webhook saved, detect announceable events since the last run and post tasteful
embeds to the CLUB'S OWN webhook:

  - schedule       : the read's reading schedule was set/changed
                     (`scheduleUpdatedAt` on the read doc newer than last
                     announce)
  - due            : one or more milestone due dates arrived
                     (`milestones[].dueAt` now past) — all newly-due sections
                     batch into ONE embed, with an on-track/behind summary
                     from the read's `progress` subcollection
  - started        : a new active read appeared (read docs carry `startedAt`)
  - finished       : an active read's `status` flipped to 'finished'
                     ('abandoned' is recorded silently — no fanfare for a DNF)
  - poll closed    : a `clubs/{id}/polls/{pollId}` flips open → closed —
                     announces the question + winning option(s), tallied from
                     the poll's `votes` subcollection. Options are plain
                     strings today (backlog #3); backlog #3b (next-book
                     polls, shipped 2026-08-14) added book-ref options, so
                     the winner line is decoded defensively for BOTH shapes
                     (see `_poll_option_text`). Poll events are ADDITIONALLY
                     gated by their own per-club sub-toggle,
                     `discordPollAnnouncements` (backlog #2c, default OFF) —
                     a club can want meeting/due nudges without poll chatter.
                     The master `discordAnnouncements` flag still gates the
                     whole engine; the sub-toggle only controls whether poll
                     events specifically get an embed once the engine already
                     runs for that club. Transitions are still TRACKED in the
                     state doc even while the sub-toggle is off, so turning it
                     on later never floods the channel with poll history that
                     already happened (see `plan_club`).
  - ratings reveal : a read's `ratingsRevealed` flips true — announces the
                     book + average + count from the now-readable `ratings`
                     subcollection (unreadable by rules while blind, exactly
                     like this engine's own webhook/state docs).
  - meeting        : the club doc's `nextMeetingAt` is set or changes since
                     the last announce — announces date + notes; a separate
                     reminder embed fires once when the meeting falls inside
                     the next ~8h run window (one reminder per meeting
                     instant, marker-keyed by that exact timestamp — the same
                     staleness convention `rsvps` uses for `meetingAt`).
  - TBR leader     : the club's `tbr` subcollection has a new strict vote
                     leader since the last announce. Needs NO new state
                     browsers have to write — `tbr/{itemId}.voterSlugs` (an
                     open, already-existing field) is enough to compute a
                     leader server-side each run; see `tbr_leader()`. A tied
                     top spot is treated as "no clear leader" (silence, not a
                     flap) rather than announcing an arbitrary tiebreak.

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
No rules change was needed. Shape (backlog #2b keys are ADDITIVE — every one
is read with a default/None fallback so a pre-#2b state doc keeps working;
an absent key means "adopt whatever is true right now, silently" — the same
baseline-first-silence convention #2 already used for brand-new clubs):

  {
    "lastRunAt": iso-utc,
    "reads": { readId: { "status": str, "scheduleAnnouncedMs": int,
                         "duePositions": [int, ...],
                         "ratingsRevealedSeen": bool } },   # #2b
    "polls": { pollId: { "status": "open" | "closed" } },  # #2b
    "meetingAnnouncedMs": int,             # #2b - last nextMeetingAt posted
    "meetingReminderSentFor": int | None,  # #2b - meetingAt a reminder went out for
    "tbrLeaderId": str | None,             # #2b - last-announced TBR leader itemId
    "consecutiveFailures": int,   # webhook posts failing back-to-back
    "lastError": str | None,
  }

Rate safety: at most MAX_EMBEDS_PER_RUN embeds per club per run, all sent in a
single webhook call. The FIRST run for a club establishes a silent baseline —
existing reads, schedules, past due dates, polls, the meeting and the TBR
leader are all recorded, nothing is posted — so enabling the feature never
floods the channel with history. When more events fire than the cap allows,
embeds are kept by priority rather than arrival order: due/reminder > ratings
reveal > poll closed > meeting scheduled > started/finished/schedule/TBR
leader (the least time-sensitive tier overflows to next run).

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
# backlog #2c: poll chatter's own opt-in sub-toggle, checked ON TOP OF
# FEATURE_KEY (which still master-gates the whole engine) — see the "poll
# closed" bullet in the module docstring above.
POLL_FEATURE_KEY = "discordPollAnnouncements"  # mirrors site/clubs.js FEATURE_DEFAULTS (default OFF)
STATE_DOC_ID = "announceState"         # clubs/{id}/settings/announceState — browser-unreadable
WEBHOOK_DOC_ID = "discord"             # clubs/{id}/settings/discord — {webhookUrl, ...}

MAX_EMBEDS_PER_RUN = 6                 # a handful per club per run; Discord allows 10/message
REMINDER_WINDOW_MS = 8 * 60 * 60 * 1000  # ~one pipeline cadence (app/main.py runs every 8h)

COLOR_BLUE = 5814783                   # matches send_discord_notification.py
COLOR_GREEN = 3066993
COLOR_AMBER = 15105570
COLOR_PURPLE = 10181046                # backlog #2b: polls / ratings reveal / TBR leader

# Cap priority (lower sorts first, kept when a run has more events than
# MAX_EMBEDS_PER_RUN allows) — due/reminder > ratings reveal > poll closed >
# meeting scheduled > everything else (started/finished/schedule/TBR leader,
# the least time-sensitive tier, overflows to next run).
EVENT_PRIORITY = {
    "due": 0,
    "meeting_reminder": 0,
    "ratings_revealed": 1,
    "poll_closed": 2,
    "meeting": 3,
    "schedule": 4,
    "started": 4,
    "finished": 4,
    "tbr_leader": 4,
}


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


def feature_enabled(club: Dict[str, Any], key: str = FEATURE_KEY) -> bool:
    """
    Python mirror of site/clubs.js clubFeatureEnabled() (default OFF).
    `key` defaults to the master FEATURE_KEY; pass POLL_FEATURE_KEY for the
    backlog #2c poll-chatter sub-toggle.
    """
    feats = club.get("features")
    if isinstance(feats, dict) and key in feats:
        return bool(feats[key])
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


def member_position(
    milestones: List[Dict[str, Any]], progress: Dict[str, Any], chaptered: Optional[bool] = None
) -> int:
    """
    Mirror of memberSchedulePosition(milestones, progress, chaptered): effective
    milestone position from a progress doc. Chapter-mapped milestones (chEnd)
    count as complete once chapterIndex reaches chEnd; else milestonePosition.
    -1 = not started.

    ⚠️ `chaptered` is optional here and INFERRED from milestone shape (any
    chEnd present) when omitted — that inference is what this function did
    before this parameter existed, kept as the default for existing callers.
    But it is NOT what the JS canon does: `memberSchedulePosition` takes
    `chaptered` as an explicit argument, sourced from `hasChapters()`
    (`read.chapterTitles.length > 0`) — a fact about the READ, not about
    whether any one milestone happens to carry `chEnd`.

    In production the two signals are set atomically together at read
    creation (`site/club-reads.js:startRead` stamps `chapterTitles` and
    `milestones` from the same `bookChapters` object in one write), so
    inference is safe for every read this app has ever created. It is NOT
    safe in general: catalog-platform/data/club-fixtures.json carries two
    ADVERSARIAL cases where an explicit `chaptered` disagrees with what the
    milestone shape implies, and passing `chaptered=None` there reproduces a
    REAL measured divergence from the JS canon (position 1 vs 0, and -1 vs 2
    — see title-key-fixtures.json's sibling club-fixtures.json and
    tests/test_club_fixtures.py). Callers that have the read doc in hand
    should pass `chaptered=bool(read.get("chapterTitles"))` explicitly, as
    `on_track_summary` below now does, rather than lean on the inference.
    """
    mlist = milestones or []
    last = max((int(m.get("position", 0)) for m in mlist), default=-1)
    if not progress:
        return -1
    if progress.get("finished"):
        return last
    if chaptered is None:
        chaptered = any(isinstance(m.get("chEnd"), (int, float)) for m in mlist)
    if chaptered:
        # Mirrors the JS branch exactly: once chaptered is true, the function
        # NEVER falls back to milestonePosition, even when chapterIndex is
        # missing or not a number — that reads as chapterIndex = -1, same as
        # memberSchedulePosition's `typeof progress.chapterIndex === 'number'
        # ? progress.chapterIndex : -1`. An earlier version of this function
        # fell through to milestonePosition here instead, which is wrong
        # whenever chaptered is true but chapterIndex is absent.
        ch = progress.get("chapterIndex")
        ch = ch if isinstance(ch, (int, float)) and not isinstance(ch, bool) else -1
        best = -1
        for m in mlist:
            end = m.get("chEnd")
            if isinstance(end, (int, float)) and ch >= end and int(m.get("position", 0)) > best:
                best = int(m.get("position", 0))
        return best
    pos = progress.get("milestonePosition")
    return int(pos) if isinstance(pos, (int, float)) and not isinstance(pos, bool) else -1


def on_track_summary(
    milestones: List[Dict[str, Any]],
    progress_docs: List[Dict[str, Any]],
    now_ms: int,
    chaptered: Optional[bool] = None,
) -> Optional[str]:
    """
    'N of M readers on track' against the schedule (mirrors scheduleStatus():
    a member is on track when no past-due milestone is beyond their position).
    None when nobody has a progress doc yet.

    `chaptered` should be `bool(read.get("chapterTitles"))` — the same signal
    JS's `hasChapters()` reads — whenever the caller has the read doc, which
    `build_embed` below does. Left `None` (member_position's shape-inference
    fallback) only for callers, like the existing unit tests, that predate
    this parameter and never had a read doc to read it from.
    """
    if not progress_docs:
        return None
    due_positions = past_due_positions(milestones, now_ms)
    on_track = 0
    for p in progress_docs:
        pos = member_position(milestones, p, chaptered)
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
        "ratingsRevealedSeen": bool(read.get("ratingsRevealed")),
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
    revealed = bool(read.get("ratingsRevealed"))
    entry = {
        "status": status,
        "scheduleAnnouncedMs": (prior or {}).get("scheduleAnnouncedMs", 0),
        "duePositions": list((prior or {}).get("duePositions", [])),
        "ratingsRevealedSeen": (prior or {}).get("ratingsRevealedSeen"),
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
        # Ratings can't be revealed on a read this new, but adopt whatever is
        # there rather than assume False, for symmetry with every other field.
        entry["scheduleAnnouncedMs"] = sched_ms
        entry["duePositions"] = past_due_positions(milestones, now_ms)
        entry["ratingsRevealedSeen"] = revealed
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

    # Ratings reveal (backlog #2b). A missing marker — an older state doc, or
    # a read tracked before this key existed — adopts the CURRENT revealed
    # status silently instead of comparing against False, so upgrading the
    # engine never announces a reveal that already happened long ago. Once
    # seen, the flag only ever moves False -> True (a manager cannot re-hide
    # ratings), so there is no un-seen path to worry about.
    seen = entry["ratingsRevealedSeen"]
    if seen is None:
        entry["ratingsRevealedSeen"] = revealed
    elif revealed and not seen:
        events.append({"type": "ratings_revealed", "read": read})
        entry["ratingsRevealedSeen"] = True

    return events, entry


# ---------------------------------------------------------------------------
# Event detection — polls / ratings reveal (piggybacked above) / meeting /
# TBR leader (backlog #2b, all pure — unit tested without Firestore)
# ---------------------------------------------------------------------------

def detect_poll_events(
    poll: Dict[str, Any], prior: Optional[Dict[str, Any]], club_polls_tracked: bool
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Compare one poll doc against its prior per-poll state entry.

    `club_polls_tracked` tells the two "this poll id is new to me" cases
    apart: when the whole club's `polls` dict is being seen for the first
    time (an older announceState doc, pre-#2b), every current poll — even
    one closed ages ago — must be adopted SILENTLY (baseline-first-silence,
    same discipline as a brand-new club). Only once poll-tracking already
    existed for this club does a genuinely new poll id get the read-events
    precedent: one that arrives already closed still announces once (mirrors
    detect_read_events' "appeared AND finished between runs" case).
    """
    status = poll.get("status") or "open"
    entry = {"status": status}
    if prior is None:
        events = [{"type": "poll_closed", "poll": poll}] if (club_polls_tracked and status == "closed") else []
        return events, entry
    events = []
    if prior.get("status") == "open" and status == "closed":
        events.append({"type": "poll_closed", "poll": poll})
    return events, entry


def tally_poll_votes(options: List[Any], votes: List[Dict[str, Any]]) -> List[int]:
    """Python mirror of tallyPollVotes() in site/club-reads.js."""
    counts = [0] * len(options or [])
    for v in votes or []:
        idx = v.get("optionIndex")
        if isinstance(idx, (int, float)) and not isinstance(idx, bool) and 0 <= int(idx) < len(counts):
            counts[int(idx)] += 1
    return counts


def poll_winners(options: List[Any], votes: List[Dict[str, Any]]) -> Tuple[List[int], List[int]]:
    """Index/indices tied for the most votes. Empty list when nobody voted."""
    counts = tally_poll_votes(options, votes)
    if not counts or not any(counts):
        return [], counts
    top = max(counts)
    return [i for i, c in enumerate(counts) if c == top], counts


def tally_ratings(ratings: List[Dict[str, Any]]) -> Tuple[float, int]:
    """Python mirror of tallyRatings() in site/club-reads.js: (average, count)."""
    values = [r.get("rating") for r in ratings or []
              if isinstance(r.get("rating"), (int, float)) and not isinstance(r.get("rating"), bool)]
    if not values:
        return 0.0, 0
    return round(sum(values) / len(values) * 10) / 10, len(values)


def detect_meeting_events(
    club: Dict[str, Any], prior_state: Dict[str, Any], now_ms: int
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Compare club.nextMeetingAt against the club-level markers in the state
    doc. An absent `meetingAnnouncedMs` key (older state doc, or first
    contact) means "adopt whatever's on the club doc right now, announce
    nothing this run" — baseline-first-silence, so upgrading never spams an
    already-scheduled meeting. `meetingReminderSentFor` is marker-keyed by
    the exact meeting instant (the rsvps `meetingAt` staleness convention):
    a reminder fires at most once per distinct timestamp, and a reschedule
    (a new, different meetingAt) clears it so the new instant gets its own.
    """
    prior_state = prior_state or {}
    meeting_ms = ts_millis(club.get("nextMeetingAt"))
    events: List[Dict[str, Any]] = []

    if "meetingAnnouncedMs" not in prior_state:
        return events, {"meetingAnnouncedMs": meeting_ms or 0, "meetingReminderSentFor": None}

    announced_ms = prior_state.get("meetingAnnouncedMs", 0)
    reminder_for = prior_state.get("meetingReminderSentFor")

    if meeting_ms and meeting_ms != announced_ms:
        events.append({"type": "meeting", "club": club, "meetingMs": meeting_ms})
        announced_ms = meeting_ms
        reminder_for = None
    elif not meeting_ms:
        announced_ms = 0  # cleared — a later meeting at any real instant will re-announce

    if meeting_ms and now_ms <= meeting_ms <= now_ms + REMINDER_WINDOW_MS and reminder_for != meeting_ms:
        events.append({"type": "meeting_reminder", "club": club, "meetingMs": meeting_ms})
        reminder_for = meeting_ms

    return events, {"meetingAnnouncedMs": announced_ms, "meetingReminderSentFor": reminder_for}


def tbr_leader(tbr_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    The TBR item with STRICTLY the most voterSlugs, or None when the TBR is
    empty, nothing has a vote yet, or the top spot is tied. A tie is treated
    as "no clear leader" rather than picking a tiebreak — otherwise an
    unrelated event (someone suggesting a new 0-vote book) could flip which
    equally-voted item sorts first and read as a meaningless "leader change".
    """
    scored = [(len(item.get("voterSlugs") or []), item) for item in (tbr_items or [])]
    scored = [s for s in scored if s[0] > 0]
    if not scored:
        return None
    scored.sort(key=lambda s: s[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def detect_tbr_event(
    tbr_items: List[Dict[str, Any]], prior_state: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Compare the current strict TBR leader (see tbr_leader) against the
    club-level marker. An absent `tbrLeaderId` key (older state doc, or
    first contact) adopts the current leader silently — baseline-first-
    silence, same as every other #2b marker.
    """
    prior_state = prior_state or {}
    leader = tbr_leader(tbr_items)
    leader_id = leader.get("id") if leader else None
    events: List[Dict[str, Any]] = []
    if "tbrLeaderId" not in prior_state:
        return events, {"tbrLeaderId": leader_id}
    prior_leader_id = prior_state.get("tbrLeaderId")
    if leader_id and leader_id != prior_leader_id:
        events.append({"type": "tbr_leader", "item": leader})
        return events, {"tbrLeaderId": leader_id}
    # No clear leader this run (empty/tied), or the leader is unchanged: keep
    # the marker as it was rather than overwriting it with None — a
    # transient tie must not cause a duplicate re-announcement once it
    # resolves back to the same book that was already announced.
    return events, {"tbrLeaderId": prior_leader_id}


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
        summary = on_track_summary(milestones, progress_docs or [], now_ms, bool(read.get("chapterTitles")))
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


def _club_field(club: Dict[str, Any], link: str) -> Dict[str, Any]:
    club_name = club.get("name") or "your club"
    return {"name": "Club", "value": f"[{club_name}]({link})", "inline": True}


def _poll_option_text(option: Any) -> str:
    """
    One poll option, decoded defensively for both shapes: today's plain
    string (backlog #3), or a book-ref object (backlog #3b "next-book
    polls", queued but NOT shipped as of this engine's build — see the
    module docstring). Tries both today's and the plausible future field
    names so this keeps working whichever the eventual shape turns out to be.
    """
    if isinstance(option, dict):
        title = option.get("title") or option.get("bookTitle") or "a book"
        author = (option.get("author") or option.get("bookAuthor") or "").strip()
        return f"**{title}**" + (f" by {author}" if author else "")
    return str(option)


def build_poll_embed(event: Dict[str, Any], club: Dict[str, Any], link: str) -> Dict[str, Any]:
    """Poll-closed embed: question + winning option(s), tie-aware."""
    poll = event["poll"]
    votes = event.get("votes") or []
    options = poll.get("options") or []
    winners, counts = poll_winners(options, votes)
    total = sum(counts)

    if winners:
        text = "; ".join(_poll_option_text(options[i]) for i in winners)
        result = ("Tied at the top: " if len(winners) > 1 else "Winner: ") + text
    else:
        result = "No votes were cast."
    lines = [f'"{poll.get("question") or "?"}"', result]
    if total:
        lines.append(f"{total} vote{'s' if total != 1 else ''} total.")

    embed = {
        "title": "🗳️ Poll closed",
        "description": _clip("\n".join(lines)),
        "color": COLOR_PURPLE,
        "fields": [_club_field(club, link)],
    }
    # Only when a single winner carries a book-ref shape (backlog #3b, once
    # it exists) — a tie or a plain-string option has nothing to show.
    if len(winners) == 1 and isinstance(options[winners[0]], dict):
        cover = (options[winners[0]].get("cover") or options[winners[0]].get("coverHref") or "").strip()
        if cover.startswith("http"):
            embed["thumbnail"] = {"url": cover}
    embed["timestamp"] = _now_iso()
    return embed


def build_ratings_embed(event: Dict[str, Any], club: Dict[str, Any], link: str,
                        average: float, count: int) -> Dict[str, Any]:
    """Ratings-revealed embed: book + average + count."""
    read = event["read"]
    lines = [
        f"Ratings for {_book_line(read)} are in!",
        f"Average: **{average}**/5 from {count} rating{'s' if count != 1 else ''}.",
    ]
    embed = {
        "title": f"⭐ Ratings revealed — {read.get('bookTitle') or 'a book'}",
        "description": _clip("\n".join(lines)),
        "color": COLOR_PURPLE,
        "fields": [_club_field(club, link)],
    }
    thumb = _thumbnail(read)
    if thumb:
        embed["thumbnail"] = thumb
    embed["timestamp"] = _now_iso()
    return embed


def build_meeting_embed(event: Dict[str, Any], club: Dict[str, Any], link: str) -> Dict[str, Any]:
    """Meeting-scheduled or meeting-reminder embed."""
    kind = event["type"]
    club_name = club.get("name") or "your club"
    meeting_ts = event["meetingMs"] // 1000
    notes = (club.get("nextMeetingNotes") or "").strip()

    if kind == "meeting_reminder":
        title = "⏰ Meeting reminder"
        head = f"{club_name} meets <t:{meeting_ts}:F> (<t:{meeting_ts}:R>)."
        color = COLOR_AMBER
    else:
        title = "🗓️ Meeting scheduled"
        head = f"{club_name}'s next meeting is <t:{meeting_ts}:F>."
        color = COLOR_BLUE
    lines = [head] + ([notes] if notes else [])

    return {
        "title": title,
        "description": _clip("\n".join(lines)),
        "color": color,
        "fields": [_club_field(club, link)],
        "timestamp": _now_iso(),
    }


def build_tbr_embed(event: Dict[str, Any], club: Dict[str, Any], link: str) -> Dict[str, Any]:
    """New-TBR-leader embed."""
    item = event["item"]
    votes = len(item.get("voterSlugs") or [])
    title = item.get("bookTitle") or "a book"
    author = (item.get("bookAuthor") or "").strip()
    book = f"**{title}**" + (f" by {author}" if author else "")

    embed = {
        "title": "📈 New TBR leader",
        "description": _clip(f"{book} is now leading the club's TBR with {votes} vote{'s' if votes != 1 else ''}."),
        "color": COLOR_PURPLE,
        "fields": [_club_field(club, link)],
    }
    cover = (item.get("coverHref") or "").strip()
    if cover.startswith("http"):
        embed["thumbnail"] = {"url": cover}
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

    def polls(self, club_id: str) -> List[Dict[str, Any]]:
        out = []
        ref = self._db.collection(self.collection_name).document(club_id).collection("polls")
        for snap in ref.stream():
            data = snap.to_dict() or {}
            data["id"] = snap.id
            out.append(data)
        return out

    def poll_votes(self, club_id: str, poll_id: str) -> List[Dict[str, Any]]:
        ref = (self._db.collection(self.collection_name).document(club_id)
               .collection("polls").document(poll_id).collection("votes"))
        return [snap.to_dict() or {} for snap in ref.stream()]

    def ratings(self, club_id: str, read_id: str) -> List[Dict[str, Any]]:
        # Only readable once the read's ratingsRevealed flips (firestore.rules
        # `ratingsRevealed()`); the service account bypasses rules regardless,
        # so callers gate the call on the event instead (see plan_club).
        ref = (self._db.collection(self.collection_name).document(club_id)
               .collection("reads").document(read_id).collection("ratings"))
        return [snap.to_dict() or {} for snap in ref.stream()]

    def tbr(self, club_id: str) -> List[Dict[str, Any]]:
        out = []
        ref = self._db.collection(self.collection_name).document(club_id).collection("tbr")
        for snap in ref.stream():
            data = snap.to_dict() or {}
            data["id"] = snap.id
            out.append(data)
        return out


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
        # First contact: record everything as seen, announce nothing —
        # reads, polls, the meeting and the TBR leader all baseline together.
        state = {
            "lastRunAt": _now_iso(),
            "reads": {r["id"]: baseline_entry(r, now_ms) for r in reads},
            "polls": {p["id"]: {"status": p.get("status") or "open"} for p in source.polls(club_id)},
            "meetingAnnouncedMs": ts_millis(club.get("nextMeetingAt")) or 0,
            "meetingReminderSentFor": None,
            "tbrLeaderId": (tbr_leader(source.tbr(club_id)) or {}).get("id"),
            "consecutiveFailures": 0,
            "lastError": None,
        }
        return [], state, True

    pending: List[Tuple[int, Dict[str, Any]]] = []  # (priority, embed) — capped by priority, not arrival order

    # ---- reads: schedule / due / started / finished / ratings reveal ----
    prior_reads = prior_state.get("reads") or {}
    new_reads_state: Dict[str, Any] = {}
    for read in reads:
        events, entry = detect_read_events(read, prior_reads.get(read["id"]), now_ms)
        new_reads_state[read["id"]] = entry
        for event in events:
            if event["type"] == "ratings_revealed":
                average, count = tally_ratings(source.ratings(club_id, read["id"]))
                embed = build_ratings_embed(event, club, link, average, count)
            else:
                progress = source.progress(club_id, read["id"]) if event["type"] == "due" else None
                embed = build_embed(event, club, link, now_ms, progress)
            pending.append((EVENT_PRIORITY[event["type"]], embed))
    # Reads deleted from Firestore fall out of the state doc naturally.

    # ---- polls: open -> closed ----
    # Transitions are tracked in new_polls_state regardless of the backlog
    # #2c sub-toggle below, so flipping discordPollAnnouncements on later
    # never retroactively announces a poll that closed while it was off.
    polls_tracked = "polls" in prior_state
    prior_polls = prior_state.get("polls") or {}
    poll_announcements_on = feature_enabled(club, POLL_FEATURE_KEY)
    new_polls_state: Dict[str, Any] = {}
    for poll in source.polls(club_id):
        events, entry = detect_poll_events(poll, prior_polls.get(poll["id"]), polls_tracked)
        new_polls_state[poll["id"]] = entry
        if not poll_announcements_on:
            continue
        for event in events:
            event["votes"] = source.poll_votes(club_id, poll["id"])
            pending.append((EVENT_PRIORITY["poll_closed"], build_poll_embed(event, club, link)))
    # Polls deleted from Firestore fall out of the state doc naturally.

    # ---- meeting scheduled/changed + reminder ----
    meeting_events, meeting_state = detect_meeting_events(club, prior_state, now_ms)
    for event in meeting_events:
        pending.append((EVENT_PRIORITY[event["type"]], build_meeting_embed(event, club, link)))

    # ---- TBR leader ----
    tbr_events, tbr_state = detect_tbr_event(source.tbr(club_id), prior_state)
    for event in tbr_events:
        pending.append((EVENT_PRIORITY["tbr_leader"], build_tbr_embed(event, club, link)))

    # Sort is stable: within a priority tier, embeds keep the order they were
    # discovered in above (reads, then polls, then meeting, then TBR).
    pending.sort(key=lambda p: p[0])
    if len(pending) > MAX_EMBEDS_PER_RUN:
        print(f"[WARN] club '{club_id}': {len(pending)} events this run, capping at {MAX_EMBEDS_PER_RUN}")
    embeds = [embed for _, embed in pending[:MAX_EMBEDS_PER_RUN]]

    new_state = {
        "lastRunAt": _now_iso(),
        "reads": new_reads_state,
        "polls": new_polls_state,
        **meeting_state,
        **tbr_state,
        "consecutiveFailures": prior_state.get("consecutiveFailures", 0),
        "lastError": prior_state.get("lastError"),
    }
    changed = (
        new_reads_state != prior_reads
        or new_polls_state != prior_polls
        or meeting_state.get("meetingAnnouncedMs") != prior_state.get("meetingAnnouncedMs")
        or meeting_state.get("meetingReminderSentFor") != prior_state.get("meetingReminderSentFor")
        or tbr_state.get("tbrLeaderId") != prior_state.get("tbrLeaderId")
    )
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
