# app/core/ingest_control.py
# When the nightly ingester is allowed to start work: the window, the machine
# guards, the owner's remote pause control, and the deadline.
#
# FIVE INDEPENDENT GATES, ALL OF WHICH MUST SAY YES BEFORE A BOOK STARTS.
# They are separate on purpose - each answers a different question, and
# collapsing them would make a refusal unattributable:
#
#   1. the WINDOW   - is this a time we promised to work?      (owner: 12am-8am)
#   2. the GPU GUARD- is the graphics card free enough?        (owner: GPU <50%)
#   3. the CONTROL  - has the owner paused us?                 (owner: dashboard)
#   4. the CPU GUARD- is the processor free enough?            (owner: 2026-08-18)
#   5. the DEADLINE - will this book FINISH before the next
#                     boundary, or would it run past it?       (owner: 2026-08-18)
#
# Gates 4 and 5 were added 2026-08-18 on the owner's order. Verbatim, for the
# deadline: *"add another check on the time every 5 minutes so by 630 the
# ingestion is done. we have a bit of buffer for the 7 hard stop so use that
# knowledge to build tolerance. if 630 hits and its about to start a new book,
# dont, if 630 hits and its almost done with a book finish, if it'll finish
# before 7 finish, etc."*
#
# ⚠️ THE OWNER'S 06:30/07:00 PAIR IS THE *SHAPE*, NOT A SECOND SET OF HOURS.
# It maps exactly onto the two numbers this module already holds: a soft
# no-new-starts line (NO_NEW_STARTS_AFTER, 07:45) and a hard stop the work must
# be finished by (WINDOW_CLOSE_HOUR, 08:00). The deadline gate reads BOTH from
# those constants and from the dashboard's pause windows, so moving either one
# moves the gate with it - there is no second hard-coded hour to forget.
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
from pathlib import Path
from typing import List, Optional, Tuple

# Arizona observes no DST, so a fixed offset is correct here and a tz database
# is not needed. ⚠️ This constant is only valid BECAUSE of that; it must never be
# copied for a zone that shifts.
PHOENIX = timezone(timedelta(hours=-7), "America/Phoenix")

WINDOW_OPEN_HOUR = 0        # 00:00 Phoenix
WINDOW_CLOSE_HOUR = 8       # 08:00 Phoenix
NO_NEW_STARTS_AFTER = (7, 45)   # 07:45 Phoenix - owner's clause

GPU_BUSY_PCT = 50           # owner: "above 50% gpu usage don't start"
GPU_POLL_SECONDS = 120      # owner: poll every 2 min while waiting

# ---- the CPU guard (owner asked whether one was worth it; answer: yes) ----
# ⚠️ 75 IS A CHOSEN NUMBER, NOT AN OWNER NUMBER. The owner set 50 for the GPU
# and asked only whether a CPU check was worth having. 75 rather than 50
# because the two signals are not comparable on this machine:
#   * MEASURED 2026-08-18 on this PC (32 logical cores): total CPU sat at
#     7-14% through a live Whisper transcription, and the ingester's own
#     pack/gzip/upload work is single-threaded - about 3% of the machine. A 50%
#     bar would therefore never fire for our own work and would only catch a
#     person's, which is what a 75% bar catches too, later and with fewer
#     false stops.
#   * the phase this gate actually protects is the ffmpeg m4b->WAV conversion
#     at the head of every audio book, which is multi-core and wants headroom.
#     Starting it on a machine already at 75% oversubscribes both jobs.
# 25 points of headroom is the argument for 75 over 80/90: above that, the
# conversion has nowhere to go and everything on the box gets slower together.
CPU_BUSY_PCT = 75
CPU_POLL_SECONDS = 30       # 30s, not the GPU's 120s - see cpu_guard()
CPU_CONFIRM_POLLS = 2       # a refusal needs TWO busy polls; an allow needs one

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


def window_close_at(now: Optional[datetime] = None) -> Optional[datetime]:
    """The instant this window shuts, or None when we are not inside one.

    ⚠️ This is the HARD stop the deadline gate measures a finish against, and it
    is NOT `NO_NEW_STARTS_AFTER`. 07:45 is the soft line that stops new starts;
    08:00 is the line the work should be finished by. The owner named the same
    two lines as "630" and "7" - the gap between them is the tolerance he asked
    us to spend, not a second budget to add on top.
    """
    now = now or phoenix_now()
    if not in_window(now):
        return None
    return now.replace(hour=WINDOW_CLOSE_HOUR, minute=0, second=0, microsecond=0)


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
# 2b. the CPU guard
# --------------------------------------------------------------------------
#
# ⚠️ THERE IS NO psutil ON THE INTERPRETER THAT RUNS THIS PIPELINE.
# MEASURED 2026-08-18: `scripts/ingest_nightly.bat` calls a bare `python`, which
# on this machine resolves to the Store build
# `...WindowsApps\PythonSoftwareFoundation.Python.3.12...\python.exe` (3.12.10),
# and `import psutil` there is a ModuleNotFoundError. So psutil is used ONLY if
# some future interpreter happens to have it - it is not a dependency, nothing
# was installed, and the readers below are Windows-native.
#
# Two native readers, in this order, both measured on this PC the same day:
#   1. `typeperf "\Processor(_Total)\% Processor Time" -sc 1`  ~1.25 s, PDH -
#      the same counter Task Manager draws. Primary.
#   2. `wmic cpu get loadpercentage /value`                    ~1.14 s, WMI.
#      Secondary because wmic is deprecated and will eventually be removed -
#      but it survives on Windows 11 26200 today AND it is locale-independent,
#      which typeperf's ENGLISH counter path is not. On a non-English Windows
#      the typeperf path parses to nothing and this fallback is what stops the
#      guard reading "unreadable -> busy" forever.

_TYPEPERF_FIELD_RE = re.compile(r'"([^"]*)"')
_WMIC_LOAD_RE = re.compile(r"LoadPercentage\s*=\s*(\d+)", re.IGNORECASE)


@dataclass
class CpuReading:
    """What the CPU guard saw, in enough detail to word a refusal."""

    busy: bool
    pct: Optional[float]     # the last reading; ⚠️ None means UNREADABLE
    polls: int
    waited_s: float          # sleep actually spent confirming a busy reading


def parse_typeperf_cpu(raw: str) -> Optional[float]:
    """Highest sampled percentage from typeperf's PDH-CSV output.

    ⚠️ Returns None when nothing parses, and None is NOT zero - the caller
    treats it as busy, exactly as the GPU parser's None is treated.

    The header row's last field is the counter's NAME, so float() rejects it and
    it drops out without a special case. Fields are pulled with a quoted-field
    regex rather than `split(",")` so that a locale using a decimal comma inside
    the quotes cannot be sliced in half. Takes the MAX across samples: one busy
    sample is a busy machine, same posture as the GPU parser's max across cards.
    """
    best: Optional[float] = None
    for line in (raw or "").splitlines():
        fields = _TYPEPERF_FIELD_RE.findall(line)
        if len(fields) < 2:
            continue
        try:
            value = float(fields[-1])
        except ValueError:
            continue
        # NaN and the infinities are what a PDH counter emits when it breaks;
        # they must not become a reading, and `value != value` catches NaN.
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            continue
        best = value if best is None else max(best, value)
    return best


def parse_wmic_cpu(raw: str) -> Optional[float]:
    """Highest `LoadPercentage=` from wmic. Max across sockets, None if absent."""
    vals = [int(v) for v in _WMIC_LOAD_RE.findall(raw or "")]
    return float(max(vals)) if vals else None


def cpu_utilisation(timeout: float = 25.0) -> Optional[float]:
    """Total CPU utilisation percent, or None when nothing could read it."""
    try:
        import psutil  # noqa: F401  - optional, never a dependency (see above)
    except Exception:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            value = psutil.cpu_percent(interval=1.0)
            if value is not None:
                return float(value)
        except Exception:
            pass

    for cmd, parser in (
        (["typeperf", r"\Processor(_Total)\% Processor Time", "-sc", "1"],
         parse_typeperf_cpu),
        (["wmic", "cpu", "get", "loadpercentage", "/value"], parse_wmic_cpu),
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except Exception:
            continue
        value = parser(proc.stdout or "")
        if value is not None:
            return value
    return None


def cpu_guard(polls: int = CPU_CONFIRM_POLLS, interval: float = CPU_POLL_SECONDS,
              threshold: float = CPU_BUSY_PCT, sleep=time.sleep) -> CpuReading:
    """Is the processor free enough to start a book? Confirm-before-refusing.

    ⚠️ THE ASYMMETRY IS THE POINT, AND IT IS THE ONE THING THE GPU GUARD DOES
    NOT NEED. An ALLOW takes one poll; a REFUSAL takes two, `interval` apart.
    The reason is that this gate is asked its question at the exact moment the
    ingester has just finished its own burst - packing, gzipping, uploading the
    previous book and its transcript. A single sample taken there can measure
    OUR OWN EXHAUST and refuse the whole night on it. A second poll 30 s later
    cannot: our burst is over in seconds, a person's game or build is not.
    That is also why this is a confirm loop rather than a fixed "settle" sleep -
    the idle case, which is every night at 2am and all 138 EPUBs, pays nothing
    at all (one ~1.2 s poll), and only a machine that actually looks busy pays
    the 30 s.
    ⚠️ 30 s and not the GPU's 120 s: the GPU's interval exists to see through
    transcription's own burstiness over minutes; the CPU signal is a 1 s average
    of 32 cores and settles far faster, and 138 EPUB starts a night cannot each
    afford two minutes.

    ⚠️ UNREADABLE COUNTS AS BUSY. `pct is None` never reads as "idle, go
    ahead" - the same fail-safe as `gpu_is_free`, and for the same reason.
    """
    pct: Optional[float] = None
    waited = 0.0
    taken = 0
    for i in range(max(1, polls)):
        pct = cpu_utilisation()
        taken += 1
        if pct is not None and pct <= threshold:
            return CpuReading(False, pct, taken, waited)
        if i < polls - 1:
            sleep(interval)
            waited += interval
    return CpuReading(True, pct, taken, waited)


def cpu_busy_words(reading: CpuReading,
                   threshold: float = CPU_BUSY_PCT) -> str:
    """The worded refusal. Never a bare number and never a bare boolean."""
    if reading.pct is None:
        return (f"CPU utilisation unreadable after {reading.polls} "
                f"poll{'s' if reading.polls != 1 else ''} "
                f"(psutil absent, typeperf and wmic both silent) - treating as BUSY")
    return (f"CPU at {reading.pct:.0f}% (> {threshold:.0f}%) on {reading.polls} "
            f"polls {CPU_POLL_SECONDS:.0f}s apart; waiting")


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
# 5. the deadline - "will this book FINISH before the next boundary?"
# --------------------------------------------------------------------------
#
# Owner, 2026-08-18: *"if 630 hits and its about to start a new book, dont, if
# 630 hits and its almost done with a book finish, if it'll finish before 7
# finish"*. Three clauses and this module answers all three:
#
#   "about to start a new book"  -> this gate, a START gate like every other.
#   "almost done ... finish"     -> unchanged: mid-book work is NEVER killed.
#   "if it'll finish before 7"   -> the estimate below, compared to the HARD
#                                   boundary rather than to the soft one. The
#                                   buffer between them is the tolerance the
#                                   owner explicitly told us to spend.
#
# ⚠️ EVERY NUMBER HERE LEANS THE SAME WAY: OVER-ESTIMATE THE BOOK.
# The two errors are not symmetric. Over-estimating costs a start that would in
# fact have fitted - the book runs tomorrow instead. Under-estimating leaves
# transcription running through the hour the owner reserved, which is the thing
# he asked for and cannot be undone once begun.

# Transcription realtime factor. MEASURED 2026-08-18 from the 17 transcripts on
# disk (each carries its own `meta.realtime_factor`):
#     clean GPU, PH books 1-11 ....... 75.5 - 85.4x
#     under contention, later books .. 31.8 - 59.8x
# So the honest input is not "the machine does 85x" - it is "the machine does
# between 32x and 85x depending on what else is happening", and the gate must
# assume the regime it is actually in.
RT_SAMPLES = 5              # how many recent books define "the regime we're in"
RT_SCAN_FILES = 40          # newest-N transcripts to open; the corpus grows
RT_SAFETY_DIVISOR = 1.30    # assume 30% worse than the WORST of those books
# ⚠️ RT_CEILING: never assume better than this, however good the recent run
# looked - a lucky clean stretch must not buy optimism the boundary then has to
# pay for. 40 sits below every clean measurement (75-85x) and above the worst
# contended one (31.8x).
RT_CEILING = 40.0
# ...and RT_FLOOR: never assume worse than this, so a single catastrophic
# outlier cannot silently stall the queue forever. At 10x a 12 h book estimates
# 80 minutes, which is a sane worst case rather than a permanent refusal.
RT_FLOOR = 10.0
RT_DEFAULT = 20.0           # no transcripts on disk yet: pessimistic but usable

# Everything that is not transcription: ffmpeg m4b->WAV, model load, chunk,
# pack, gzip, upload, and the transcript's own backup upload.
# MEASURED 2026-08-18 end to end from output_files/ingest_nightly.log against
# each transcript's meta:
#     Fourth Wing (21.4 h)  23.4 min wall,  22.1 min transcribing -> 1.3 min
#     Harry Potter (8.7 h)   9.7 min wall,   8.7 min transcribing -> 1.0 min
#     I'm Glad My Mom Died   9.1 min wall,   8.5 min transcribing -> 0.6 min
#   plus ~2 min after the pack for the transcript backup upload (wrangler).
# So the measured figure is ~3.5 min. 8 is set at more than double it, because
# conversion scales with duration (these were 9-21 h books, the shelf goes to
# 85 h) and a 2 GB WAV write on a busy disk is not the 1 min it was on an idle
# one. Over-estimating overhead is the safe direction; see the banner above.
OVERHEAD_SECONDS = 8 * 60

# ⚠️ An UNKNOWN duration is not zero and not "probably average". MEASURED
# 2026-08-18: all 1,079 catalog rows carry a parseable `duration_hhmm`, so this
# should never fire - but if a row ever loses it, the gate assumes a long book
# (p90 of the shelf is 22.8 h; median 12.1 h). That refuses a start near a
# boundary and allows one with real runway, which is the correct shape for a
# fact we do not have.
UNKNOWN_AUDIO_SECONDS = 24 * 3600


def _transcript_meta(path: Path) -> Optional[dict]:
    """A transcript's `meta` block, read from the file's HEAD.

    ⚠️ A transcript is ~13 MB of word timings and `meta` is its FIRST key, so
    4 KB answers this in microseconds where `json.load` costs ~1.5 s each. The
    same reasoning (and the same measured hang) as `ingest_books._transcript_source`.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    cut = head.find('"segments"')
    if cut < 0:
        return None
    try:
        return json.loads(head[:cut].rstrip().rstrip(",") + "}").get("meta")
    except ValueError:
        return None


def recent_realtime_factors(limit: int = RT_SAMPLES, scan: int = RT_SCAN_FILES,
                            directory: Optional[Path] = None) -> List[float]:
    """The realtime factors of the most recently completed transcriptions.

    ⚠️ mtime IS legitimate here and is NOT the additions-log mistake. That rule
    exists because `site/` lives in OneDrive, which rewrites mtimes on sync;
    `estate-training-data` is outside OneDrive and outside every repo, and its
    mtimes are this machine's own writes. It is used only to pick which files to
    OPEN - the actual ordering is each transcript's recorded `run_utc`.
    """
    if directory is None:
        from app.core.ingest_queue import TRANSCRIPTS_DIR

        directory = TRANSCRIPTS_DIR
    try:
        paths = sorted(Path(directory).glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, scan)]
    except OSError:
        return []
    rows: List[Tuple[str, float]] = []
    for path in paths:
        meta = _transcript_meta(path)
        if not meta:
            continue
        factor = meta.get("realtime_factor")
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            continue
        if factor <= 0:
            continue
        rows.append((str(meta.get("run_utc") or ""), factor))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [f for _, f in rows[:max(1, limit)]]


def realtime_factor(factors: Optional[List[float]] = None) -> Tuple[float, str]:
    """The factor the estimate uses, and the words explaining where it came from.

    ⚠️ The statistic is the MINIMUM of the recent books, not the mean or the
    median. A mean over a night that started clean and ended contended says 60x
    while the machine is doing 32x, and the gate would then start a book on a
    number that stopped being true an hour ago. The worst recent book is the
    regime we are actually in.
    """
    if factors is None:
        factors = recent_realtime_factors()
    if not factors:
        return RT_DEFAULT, (f"{RT_DEFAULT:.0f}x realtime assumed "
                            f"(no completed transcripts to measure)")
    worst = min(factors)
    value = min(worst / RT_SAFETY_DIVISOR, RT_CEILING)
    value = max(value, RT_FLOOR)
    return value, (f"{value:.1f}x realtime (worst of the last {len(factors)} "
                   f"books was {worst:.1f}x)")


def next_boundary(state: ControlState,
                  now: Optional[datetime] = None) -> Tuple[Optional[datetime], str]:
    """The next instant after which work must not still be running, and its name.

    Two sources, whichever comes first:
      * the WINDOW's hard close (08:00 Phoenix) while we are inside it;
      * the start of the next scheduled pause window the dashboard has written.

    ⚠️ `paused` and `paused_until` are deliberately NOT boundaries. They say when
    starting is forbidden, not when running must have stopped - `paused_until`
    is the END of a pause, and treating it as a deadline would refuse every
    start for a reason that has already expired.
    """
    now = now or phoenix_now()
    candidates: List[Tuple[datetime, str]] = []

    # ⚠️ The labels carry NO time of their own - the caller pairs them with the
    # boundary it was given, and a label that also stated an hour produced
    # "past the 08:00 window close at 08:00" in the log.
    close = window_close_at(now)
    if close and close > now:
        candidates.append((close, "the Phoenix window close"))

    for window in state.pause_windows or []:
        start = parse_iso(window.get("from"))
        if start and start > now:
            reason = (window.get("reason") or "").strip()
            label = "the scheduled pause window"
            candidates.append((start, f"{label} ({reason})" if reason else label))

    if not candidates:
        return None, "no boundary ahead"
    return min(candidates, key=lambda c: c[0])


@dataclass
class DeadlineEstimate:
    fits: bool
    words: str
    boundary: Optional[datetime] = None
    finish: Optional[datetime] = None
    est_seconds: float = 0.0
    factor: float = 0.0


def estimate_against_deadline(state: ControlState, now: Optional[datetime] = None,
                              audio_seconds: Optional[float] = None,
                              factors: Optional[List[float]] = None) -> DeadlineEstimate:
    """Would a book started now finish before the next boundary? Worded either way.

    ⚠️ The duration source is the CATALOG's `duration_hhmm`, not chapters.json.
    chapters.json only knows where the LAST chapter STARTS - on the first row of
    the file that is 36,427 s for a 10:07 book, i.e. 8 minutes short. Using it
    would under-estimate every book, which is the one direction this gate must
    never lean.
    """
    now = now or phoenix_now()
    boundary, label = next_boundary(state, now)

    audio = float(audio_seconds) if audio_seconds and audio_seconds > 0 else None
    audio_words = (f"{audio / 3600:.1f} h audio" if audio is not None else
                   f"an assumed {UNKNOWN_AUDIO_SECONDS / 3600:.0f} h "
                   f"(this book's runtime is unknown)")
    if audio is None:
        audio = float(UNKNOWN_AUDIO_SECONDS)

    factor, factor_words = realtime_factor(factors)
    est = audio / factor + OVERHEAD_SECONDS
    finish = now + timedelta(seconds=est)

    if boundary is None:
        return DeadlineEstimate(
            True, f"no boundary ahead; est. finish ~{finish:%H:%M}",
            None, finish, est, factor)

    detail = (f"{audio_words} at {factor_words} "
              f"+{OVERHEAD_SECONDS // 60:.0f} min overhead")
    if finish <= boundary:
        return DeadlineEstimate(
            True, f"would finish ~{finish:%H:%M}, {label} is {boundary:%H:%M} -> OK "
                  f"({detail})",
            boundary, finish, est, factor)
    return DeadlineEstimate(
        False,
        f"would finish ~{finish:%H:%M}, past {label} at {boundary:%H:%M} -> "
        f"holding ({detail})",
        boundary, finish, est, factor)


def deadline_blocks_start(state: ControlState, now: Optional[datetime] = None,
                          audio_seconds: Optional[float] = None,
                          factors: Optional[List[float]] = None) -> Optional[str]:
    """A worded reason the deadline refuses this start, or None to proceed."""
    estimate = estimate_against_deadline(state, now, audio_seconds, factors)
    return None if estimate.fits else estimate.words


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
    cpu_pct: Optional[float] = None
    est_finish: Optional[datetime] = None
    boundary: Optional[datetime] = None


def _decide_cpu_only(window_ok: bool, now: datetime, sleep) -> StartDecision:
    """EPUB and text-PDF work: two of the five gates, and here is why.

    Exempt from the GPU guard - waiting for a graphics card to idle before
    parsing a zip file would be theatre. Exempt from the DEADLINE too, because
    this work is measured in seconds and "will it finish in time" has one
    answer. NOT exempt from the CPU guard: the processor is exactly what it
    competes for, and 138 EPUBs is real work on the machine a person is using.
    """
    if not window_ok:
        return StartDecision(False, "outside the 00:00-07:45 Phoenix window",
                             batch_size_for(now))
    cpu = cpu_guard(sleep=sleep)
    if cpu.busy:
        return StartDecision(False, cpu_busy_words(cpu), batch_size_for(now),
                             cpu_pct=cpu.pct)
    return StartDecision(True, f"in window; GPU-guard exempt, CPU at {cpu.pct:.0f}%",
                         batch_size_for(now), cpu_pct=cpu.pct)


def _gpu_clearance(window_ok: bool, now: datetime, allow_opportunistic: bool,
                   sustained_polls: int, sleep
                   ) -> Tuple[Optional[StartDecision], Optional[int], bool, float]:
    """The GPU half of an audio start: `(refusal | None, pct, opportunistic, waited)`.

    Two different tests behind one return, and the difference is deliberate:
    inside the window a single poll decides, because at 2am the window IS the
    guarantee; outside it the bonus path asks for two polls two minutes apart
    before borrowing a machine somebody may be using. Only the second sleeps,
    and it reports how long so the caller can re-test the deadline against a
    clock that moved.
    """
    if window_ok:
        util = gpu_utilisation()
        if util is None:
            return (StartDecision(False, "GPU utilisation unreadable - treating as busy",
                                  batch_size_for(now), util), util, False, 0.0)
        if util > GPU_BUSY_PCT:
            return (StartDecision(False, f"GPU at {util}% (> {GPU_BUSY_PCT}%); waiting",
                                  batch_size_for(now), util), util, False, 0.0)
        return None, util, False, 0.0

    if not allow_opportunistic:
        return (StartDecision(False, "outside the window; opportunistic runs disabled",
                              batch_size_for(now)), None, True, 0.0)
    if not gpu_sustained_free(polls=sustained_polls, sleep=sleep):
        return (StartDecision(False,
                              f"outside the window and the GPU is not sustained-free "
                              f"({sustained_polls} polls, {GPU_POLL_SECONDS}s apart)",
                              batch_size_for(now)), None, True, 0.0)
    return (None, gpu_utilisation(), True,
            max(0, sustained_polls - 1) * GPU_POLL_SECONDS)


def decide_start(state: ControlState, now: Optional[datetime] = None,
                 needs_gpu: bool = True, allow_opportunistic: bool = True,
                 sustained_polls: int = 2, sleep=time.sleep,
                 audio_seconds: Optional[float] = None,
                 factors: Optional[List[float]] = None) -> StartDecision:
    """Should a new book start right now? One place, five gates, worded.

    ⚠️ Order matters and is chosen so the cheapest and most authoritative gate
    runs first: the owner's don't-check beats everything (it exists precisely to
    stop us spending anything), then the pause, then the clock, then the
    DEADLINE - which is pure arithmetic and can refuse before a single
    subprocess runs - then the GPU, and finally the CPU, which is the only gate
    that may sleep 30 s to confirm a refusal.

    ⚠️ AND THE DEADLINE IS TESTED TWICE. A guard that waits can outlive the
    boundary it was cleared against: two GPU polls two minutes apart plus a CPU
    confirm is four and a half minutes, and a start cleared at 07:44 must not
    begin at 07:48. Every wait adds its sleep to `waited`, and the deadline is
    re-tested at `now + waited` before the decision is returned. It is the
    intended sleep that is accumulated, not a wall-clock read, so a test with an
    injected `sleep` exercises the same arithmetic the night does.
    """
    now = now or phoenix_now()
    waited = 0.0

    deferred = control_defers_check(state, now)
    if deferred:
        return StartDecision(False, deferred, batch_size_for(now))

    blocked = control_blocks_start(state, now)
    if blocked:
        return StartDecision(False, blocked, batch_size_for(now))

    window_ok = may_start_new_book(now)

    if not needs_gpu:
        return _decide_cpu_only(window_ok, now, sleep)

    estimate = estimate_against_deadline(state, now, audio_seconds, factors)
    if not estimate.fits:
        return StartDecision(False, estimate.words, batch_size_for(now),
                             est_finish=estimate.finish, boundary=estimate.boundary)

    refusal, util, opportunistic, gpu_waited = _gpu_clearance(
        window_ok, now, allow_opportunistic, sustained_polls, sleep)
    if refusal is not None:
        return refusal
    waited += gpu_waited

    cpu = cpu_guard(sleep=sleep)
    waited += cpu.waited_s
    if cpu.busy:
        return StartDecision(False, cpu_busy_words(cpu), batch_size_for(now),
                             util, opportunistic, cpu_pct=cpu.pct)

    if waited:
        # ⚠️ The re-check. See the docstring: a cleared start that then waited
        # through the boundary must give up, not begin late.
        later = now + timedelta(seconds=waited)
        estimate = estimate_against_deadline(state, later, audio_seconds, factors)
        if not estimate.fits:
            return StartDecision(
                False, f"{estimate.words} (after {waited:.0f}s of guard waiting)",
                batch_size_for(now), util, opportunistic, cpu.pct,
                estimate.finish, estimate.boundary)

    where = "opportunistic: GPU sustained-free" if opportunistic else "in window"
    return StartDecision(True,
                         f"{where}; GPU at {util}%, CPU at {cpu.pct:.0f}%; "
                         f"{estimate.words}",
                         batch_size_for(now), util, opportunistic, cpu.pct,
                         estimate.finish, estimate.boundary)
