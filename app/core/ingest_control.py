# app/core/ingest_control.py
# When the nightly ingester is allowed to start work: the window, the GPU
# guard, and the owner's remote pause control.
#
# THREE INDEPENDENT GATES, ALL OF WHICH MUST SAY YES BEFORE A BOOK STARTS.
# They are separate on purpose - each answers a different question, and
# collapsing them would make a refusal unattributable:
#
#   1. the WINDOW   - is this a time we promised to work?      (owner: 12am-8am)
#   2. the GUARD    - is the machine free enough?              (owner: GPU <50%)
#   3. the CONTROL  - has the owner paused us?                 (owner: dashboard)
#
# ⚠️ MID-BOOK WORK ALWAYS COMPLETES. Every gate here is a START gate. A book
# part-way through transcription runs to the end and packs, because killing it
# wastes the GPU-hours already spent and leaves a WAV on disk; the state file
# then records it done and the next start decision is taken cleanly. This is the
# same semantics at the window's end, at a pause, and at a guard trip.
#
# TIMEZONE: America/Phoenix, and the machine agrees.
# MEASURED 2026-08-18: this PC's timezone is `US Mountain Standard Time`,
# UTC-07:00, `SupportsDaylightSavingTime=False`, 0 adjustment rules - i.e. it IS
# Arizona and it never shifts. Phoenix time is nevertheless computed EXPLICITLY
# from UTC here rather than trusting `datetime.now()`, so that re-homing this
# machine (or a session running it under a different TZ) cannot silently move
# the window. `phoenix_now()` is the only clock this module reads.

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

# Arizona observes no DST, so a fixed offset is correct here and a tz database
# is not needed. ⚠️ This constant is only valid BECAUSE of that; it must never be
# copied for a zone that shifts.
PHOENIX = timezone(timedelta(hours=-7), "America/Phoenix")

WINDOW_OPEN_HOUR = 0        # 00:00 Phoenix
WINDOW_CLOSE_HOUR = 8       # 08:00 Phoenix
NO_NEW_STARTS_AFTER = (7, 45)   # 07:45 Phoenix - owner's clause

GPU_BUSY_PCT = 50           # owner: "above 50% gpu usage don't start"
GPU_POLL_SECONDS = 120      # owner: poll every 2 min while waiting

# Batch sizes. Pilot-measured: batch 8 = 85.3x realtime at 10.3 GB VRAM;
# batch 16 = 102.6x at 12.8 GB. ⚠️ 16 is for the WINDOW ONLY - the machine is
# idle at midnight, and 12.8 GB of 16 GB leaves too little for a person using
# the PC. Outside the window the ingester is a guest and takes the smaller one.
BATCH_IN_WINDOW = 16
BATCH_OUTSIDE_WINDOW = 8


def phoenix_now() -> datetime:
    """The one clock. UTC -> Phoenix explicitly; never the machine's local tz."""
    return datetime.now(timezone.utc).astimezone(PHOENIX)


def machine_tz_is_phoenix() -> bool:
    """True when the PC's own local clock already equals Phoenix.

    Not used to DECIDE anything - `phoenix_now()` is correct either way - but
    reported by the tool and asserted by a test, so that a machine move shows up
    as a stated fact rather than as a window that quietly runs at the wrong hour.
    """
    local = datetime.now().astimezone()
    return local.utcoffset() == PHOENIX.utcoffset(None)


# --------------------------------------------------------------------------
# 1. the window
# --------------------------------------------------------------------------

def in_window(now: Optional[datetime] = None) -> bool:
    now = now or phoenix_now()
    return WINDOW_OPEN_HOUR <= now.hour < WINDOW_CLOSE_HOUR


def may_start_new_book(now: Optional[datetime] = None) -> bool:
    """The window's START gate: inside it, and before 07:45.

    ⚠️ The 07:45 cutoff is NOT the window's end. It is the owner's clause that a
    book started at 07:50 would run past 8am; a book already running at 07:45
    finishes normally (see the module header).
    """
    now = now or phoenix_now()
    if not in_window(now):
        return False
    return (now.hour, now.minute) < NO_NEW_STARTS_AFTER


def batch_size_for(now: Optional[datetime] = None) -> int:
    return BATCH_IN_WINDOW if in_window(now) else BATCH_OUTSIDE_WINDOW


def seconds_until_window_open(now: Optional[datetime] = None) -> float:
    now = now or phoenix_now()
    if in_window(now):
        return 0.0
    nxt = (now + timedelta(days=1)).replace(
        hour=WINDOW_OPEN_HOUR, minute=0, second=0, microsecond=0)
    if now.hour >= WINDOW_CLOSE_HOUR:
        return (nxt - now).total_seconds()
    today = now.replace(hour=WINDOW_OPEN_HOUR, minute=0, second=0, microsecond=0)
    return max(0.0, (today - now).total_seconds())


# --------------------------------------------------------------------------
# 2. the GPU guard
# --------------------------------------------------------------------------

_SMI_NUM_RE = re.compile(r"(\d+)")


def parse_gpu_utilisation(raw: str) -> Optional[int]:
    """Highest per-GPU utilisation percent from nvidia-smi CSV output.

    ⚠️ Returns None when the output cannot be parsed, and None is NOT zero.
    A machine with no driver, a hung nvidia-smi, or a changed output format must
    not read as "GPU is idle, go ahead" - the caller treats None as busy.
    Takes the MAX across GPUs because one saturated card is a busy machine.
    """
    vals: List[int] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SMI_NUM_RE.search(line.split(",")[0])
        if m:
            vals.append(int(m.group(1)))
    return max(vals) if vals else None


def gpu_utilisation(timeout: float = 20.0) -> Optional[int]:
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return parse_gpu_utilisation(proc.stdout)


def gpu_is_free(threshold: int = GPU_BUSY_PCT) -> bool:
    """⚠️ Unknown utilisation counts as BUSY. Fail safe, not fail open."""
    util = gpu_utilisation()
    return util is not None and util <= threshold


def gpu_sustained_free(polls: int = 2, interval: float = GPU_POLL_SECONDS,
                       threshold: int = GPU_BUSY_PCT, sleep=time.sleep) -> bool:
    """Two consecutive free readings `interval` apart.

    Used for the OPPORTUNISTIC daytime path only. A single instantaneous poll is
    a poor idleness test - transcription itself is bursty and a game between
    loading screens reads 3% - so the bonus path asks for a sustained signal
    before borrowing a machine somebody may be using. The nightly window uses the
    single-poll `gpu_is_free`, because at 2am the window is the guarantee.
    """
    for i in range(max(1, polls)):
        if not gpu_is_free(threshold):
            return False
        if i < polls - 1:
            sleep(interval)
    return True


# --------------------------------------------------------------------------
# 3. the owner's control document
# --------------------------------------------------------------------------

CONTROL_COLLECTION = os.getenv("INGEST_CONTROL_COLLECTION", "ingestion_control")
CONTROL_DOC = "state"


@dataclass
class ControlState:
    """The contract the GABI dashboard writes and this processor reads.

    Firestore: `ingestion_control/state` (prod) and `ingestion_control_dev/state`
    (/dev/ lane). Fields, all optional, absent == permissive:

        paused           bool        hard stop; no new book starts
        paused_until     ISO8601     no new starts before this instant
        dont_check_until ISO8601     do not even EVALUATE the guard before this
        pause_windows    [{from,until}]  scheduled quiet hours, ISO8601 with tz
        updated_by       string      who wrote it (uid or "processor")
        updated_at       ISO8601     when

    ⚠️ `dont_check_until` is DIFFERENT from `paused_until` and the owner asked
    for both: *"I can say don't even check to start until x time."* A pause means
    "you may not start"; a don't-check means "do not spend anything looking" -
    no GPU poll, no queue read. They are separate because a pause still logs a
    considered refusal, which is what makes the dashboard honest.

    ⚠️ An unreadable control is treated as PAUSED. The owner's stop must not
    depend on the network being up; failing open would run the GPU through the
    evening he asked to have it back.
    """

    paused: bool = False
    paused_until: Optional[str] = None
    dont_check_until: Optional[str] = None
    pause_windows: List[dict] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    readable: bool = True
    error: Optional[str] = None

    def __post_init__(self):
        if self.pause_windows is None:
            self.pause_windows = []


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Lenient ISO8601 -> aware datetime. A naive value is read as Phoenix.

    ⚠️ Naive-means-Phoenix is deliberate: the dashboard is used by one household
    in one timezone, and reading a bare "2026-08-18T19:00" as UTC would start the
    pause seven hours early. An unparseable value returns None and the caller
    treats that as "no constraint stated", never as "now".
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=PHOENIX) if dt.tzinfo is None else dt


def control_blocks_start(state: ControlState, now: Optional[datetime] = None) -> Optional[str]:
    """A worded reason a start is blocked, or None to proceed.

    Never a bare boolean: every refusal this estate makes has to say what
    happened and what would clear it, and these strings are what the log and the
    dashboard show.
    """
    now = now or phoenix_now()
    if not state.readable:
        return f"ingestion control unreadable ({state.error or 'unknown'}) - treating as PAUSED"
    if state.paused:
        return "paused by the dashboard (paused=true); clear it there to resume"
    until = parse_iso(state.paused_until)
    if until and now < until:
        return f"paused until {until.isoformat()} (dashboard paused_until)"
    for window in state.pause_windows or []:
        start = parse_iso(window.get("from"))
        end = parse_iso(window.get("until"))
        if start and end and start <= now < end:
            return f"inside a scheduled pause window {start.isoformat()} -> {end.isoformat()}"
    return None


def control_defers_check(state: ControlState, now: Optional[datetime] = None) -> Optional[str]:
    """True-ish when the poller must not even evaluate the guard yet."""
    now = now or phoenix_now()
    until = parse_iso(state.dont_check_until)
    if until and now < until:
        return f"not checking until {until.isoformat()} (dashboard dont_check_until)"
    return None


def _firestore_client():
    """Service-account Firestore, or None. Import is deferred and soft so a
    machine without firebase-admin degrades rather than crashing - the same
    posture `app/pipeline_status.py` takes."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except Exception:
        return None
    key_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "firebase_service_account.json")
    if not os.path.exists(key_path):
        return None
    try:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(key_path))
        return firestore.client()
    except Exception:
        return None


def read_control(collection: str = None) -> ControlState:
    collection = collection or CONTROL_COLLECTION
    client = _firestore_client()
    if client is None:
        # ⚠️ No credential is NOT a pause. A machine that has never had the
        # service account (a fresh checkout, CI) must still be able to run the
        # ingester by hand; the owner's pause lives in Firestore and an absent
        # Firestore means the owner has not paused anything.
        return ControlState(readable=True, error="firestore unavailable (no client)")
    try:
        snap = client.collection(collection).document(CONTROL_DOC).get()
    except Exception as exc:
        return ControlState(readable=False, error=str(exc)[:200])
    if not snap.exists:
        return ControlState(readable=True)
    data = snap.to_dict() or {}
    return ControlState(
        paused=bool(data.get("paused", False)),
        paused_until=data.get("paused_until"),
        dont_check_until=data.get("dont_check_until"),
        pause_windows=list(data.get("pause_windows") or []),
        updated_by=data.get("updated_by"),
        updated_at=data.get("updated_at"),
        readable=True,
    )


def write_control(payload: dict, collection: str = None,
                  updated_by: str = "processor") -> bool:
    collection = collection or CONTROL_COLLECTION
    client = _firestore_client()
    if client is None:
        return False
    body = dict(payload)
    body["updated_by"] = updated_by
    body["updated_at"] = phoenix_now().isoformat()
    client.collection(collection).document(CONTROL_DOC).set(body, merge=True)
    return True


# --------------------------------------------------------------------------
# the composed decision
# --------------------------------------------------------------------------

@dataclass
class StartDecision:
    may_start: bool
    reason: str
    batch_size: int
    gpu_pct: Optional[int] = None
    opportunistic: bool = False


def decide_start(state: ControlState, now: Optional[datetime] = None,
                 needs_gpu: bool = True, allow_opportunistic: bool = True,
                 sustained_polls: int = 2, sleep=time.sleep) -> StartDecision:
    """Should a new book start right now? One place, three gates, worded.

    ⚠️ Order matters and is chosen so the cheapest and most authoritative gate
    runs first: the owner's don't-check beats everything (it exists precisely to
    stop us spending anything), then the pause, then the clock, then the GPU -
    which is the only gate that costs a subprocess and, on the opportunistic
    path, four minutes of waiting.
    """
    now = now or phoenix_now()

    deferred = control_defers_check(state, now)
    if deferred:
        return StartDecision(False, deferred, batch_size_for(now))

    blocked = control_blocks_start(state, now)
    if blocked:
        return StartDecision(False, blocked, batch_size_for(now))

    window_ok = may_start_new_book(now)

    if not needs_gpu:
        # EPUB and text-PDF extraction is CPU work measured in seconds. It obeys
        # the window and the pause but is exempt from the GPU guard: waiting for
        # a graphics card to idle before parsing a zip file would be theatre.
        if window_ok:
            return StartDecision(True, "in window; CPU-only work is guard-exempt",
                                 batch_size_for(now))
        return StartDecision(False, "outside the 00:00-07:45 Phoenix window",
                             batch_size_for(now))

    if window_ok:
        util = gpu_utilisation()
        if util is None:
            return StartDecision(False, "GPU utilisation unreadable - treating as busy",
                                 batch_size_for(now), util)
        if util > GPU_BUSY_PCT:
            return StartDecision(False, f"GPU at {util}% (> {GPU_BUSY_PCT}%); waiting",
                                 batch_size_for(now), util)
        return StartDecision(True, f"in window; GPU at {util}%", batch_size_for(now), util)

    if not allow_opportunistic:
        return StartDecision(False, "outside the window; opportunistic runs disabled",
                             batch_size_for(now))

    # Opportunistic daytime path: the window is the guarantee, idle time is bonus.
    if not gpu_sustained_free(polls=sustained_polls, sleep=sleep):
        return StartDecision(False,
                             f"outside the window and the GPU is not sustained-free "
                             f"({sustained_polls} polls, {GPU_POLL_SECONDS}s apart)",
                             batch_size_for(now))
    util = gpu_utilisation()
    return StartDecision(True, f"opportunistic: GPU sustained-free (now {util}%)",
                         batch_size_for(now), util, opportunistic=True)
