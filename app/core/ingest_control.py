# app/core/ingest_control.py
# When the nightly ingester is allowed to start work: the window, the machine
# guards, the owner's remote pause control, and the deadline.
#
# SIX INDEPENDENT GATES, ALL OF WHICH MUST SAY YES BEFORE A BOOK STARTS.
# They are separate on purpose - each answers a different question, and
# collapsing them would make a refusal unattributable:
#
#   1. the WINDOW   - is this a time we promised to work?      (owner: 12am-8am)
#   2. the GPU GUARD- is the graphics card free enough?        (owner: GPU <50%)
#   3. the CONTROL  - has the owner paused us, or is this hour
#                     inside a standing recurring blocker?     (owner: dashboard)
#   4. the CPU GUARD- is the processor free enough?            (owner: 2026-08-18)
#   5. the DEADLINE - will this book FINISH before the next
#                     boundary, or would it run past it?       (owner: 2026-08-18)
#   6. the PRESENCE - is the owner USING the machine right now,
#      GUARD          i.e. is one of his named processes up?   (owner: 2026-09-01)
#
# Gate 6 was added 2026-09-01 after a real incident: the 00:00 window opened,
# batch-16 transcription started, and the owner was playing World of Warcraft on
# the same PC. Every one of gates 1-5 said yes and all five were RIGHT to - the
# hour was ours, the card was idle between frames, the book fitted. See the
# `exempt_processes` block below for why no utilisation threshold could ever
# have caught it and why process PRESENCE is the check that cannot be fooled.
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


# --------------------------------------------------------------------------
# What a manual pause MEANS (owner ask 2026-08-23, verbatim: "when i manually
# pause the pipeline it says nothing can override it. I want it to ask me if i
# want to stop all work until unpaused or if scheduled window is fine to
# continue.")
#
# Until now `paused=true` was step 2 of control_blocks_start() and it was
# UNCONDITIONAL — the dashboard even said so in words ("It stays paused until
# someone presses Resume"). The owner wants that to be a question with two
# answers, asked afresh at every pause (his decision: nothing is saved as a
# preference), so the pause document now carries what the pause MEANT.
#
#   "all"          stop everything until unpaused — the historical behaviour.
#   "manual_only"  stop interactive/by-hand work; the nightly scheduled window
#                  proceeds as if nothing were paused.
#
# ⚠️ ABSENT OR UNRECOGNISED == "all", AND THAT IS THE WHOLE SAFETY STORY. Every
# pause document written before today has no `pause_mode` field, and every one
# of them meant "stop everything". A reader that defaulted to `manual_only` —
# or that treated a typo'd value as permissive — would silently reinterpret
# pauses the owner set days ago into permission to run. Fail CLOSED, always:
# the only value that unlocks the window is the exact string "manual_only".
PAUSE_MODE_ALL = "all"
PAUSE_MODE_MANUAL_ONLY = "manual_only"
PAUSE_MODES = (PAUSE_MODE_ALL, PAUSE_MODE_MANUAL_ONLY)


def normalise_pause_mode(raw) -> str:
    """Whatever the dashboard wrote -> one of PAUSE_MODES, failing closed.

    ⚠️ Same defensive posture as `clean_id_list`: this field is written by a
    DIFFERENT repo's Worker, so its shape is somebody else's promise. A
    number, a None, a mis-spelling and a missing field all mean "all" — the
    strict reading — because the alternative is a pause that quietly stops
    binding.
    """
    return PAUSE_MODE_MANUAL_ONLY if raw == PAUSE_MODE_MANUAL_ONLY else PAUSE_MODE_ALL


def pause_mode_words(mode: str) -> str:
    """The mode in the owner's own words, for logs and refusals."""
    return ("stop all work until unpaused" if mode == PAUSE_MODE_ALL
            else "let the scheduled window continue")


@dataclass
class ControlState:
    """The contract the GABI dashboard writes and this processor reads.

    Firestore: `ingestion_control/state` (prod) and `ingestion_control_dev/state`
    (/dev/ lane). Fields, all optional, absent == permissive:

        paused           bool        hard stop; no new book starts
        pause_mode       "all"|"manual_only"   what the pause MEANS (see the
                                     PAUSE_MODES block above). Governs `paused`
                                     and `paused_until`; ABSENT == "all".
        paused_until     ISO8601     no new starts before this instant
        pause_until_gpu_free bool    ⚠️ SOFT pause: `paused_until` is a CEILING,
                                     not a promise - the processor clears it
                                     early the moment the GPU reads sustained-
                                     free and no exempt process is running
        dont_check_until ISO8601     do not even EVALUATE the guard before this
        pause_windows    [{from,until}]  scheduled quiet hours, ISO8601 with tz
        recurring_windows [{days,from,until}]  STANDING weekly quiet hours in
                                     Phoenix wall clock; absolute while in force
        exempt_processes [imageName] ⚠️ do-not-disturb: any of these running
                                     means the machine is IN USE and nothing
                                     new starts, in the window or out of it
        requeue          [bookId]    ⚠️ CONSUMED: failed books to put back to
                                     pending at the next run start, then cleared
        priority_front   [bookId|str] books/series to move to the HEAD of the
                                     queue; NOT consumed - it is a standing
                                     preference until the dashboard clears it
        updated_by       string      who wrote it (uid or "processor")
        updated_at       ISO8601     when

    ⚠️ `requeue` and `priority_front` are the two list fields and they behave
    DIFFERENTLY ON PURPOSE. A requeue is an EVENT - "put these back" - and an
    event that is not consumed fires forever, so the processor removes exactly
    the ids it acted on. A priority is a STATE - "these matter most" - and a
    state that self-clears would silently stop mattering the moment a run
    happened to see it. Collapsing the two behaviours either resurrects books
    the owner already dealt with, or drops a priority he never withdrew.

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
    # ⚠️ Defaults to "all" so a ControlState built by hand (tests, the no-
    # credential path) carries the strict meaning, exactly like a pause
    # document written before this field existed.
    pause_mode: str = PAUSE_MODE_ALL
    paused_until: Optional[str] = None
    # ⚠️ DEFAULTS FALSE AND IS COERCED TO EXACTLY True/False IN __post_init__.
    # A soft pause is the only pause this processor may end by itself, so the
    # field that says "you may end it" must never be true by accident: a string
    # "false", a 0, a None or a missing field all mean NO, and only the literal
    # boolean True unlocks the release path. Fail closed here means the pause
    # merely lasts to its ceiling, which is the harmless direction (§5 of the
    # design: this is the one new field whose old-reader behaviour fails CLOSED).
    pause_until_gpu_free: bool = False
    dont_check_until: Optional[str] = None
    pause_windows: List[dict] = None
    recurring_windows: List[dict] = None
    exempt_processes: List[str] = None
    requeue: List[str] = None
    priority_front: List[str] = None
    # ⚠️ The RAW `requeue` entries this reader refused, kept so the consumer can
    # sweep them out of the document. MEASURED 2026-08-18 during the first live
    # round trip: `clear_requeue` uses ArrayRemove, which deletes only the values
    # it names, so an entry the reader dropped survived the clear and warned
    # again on EVERY control read - and the control is read before every book,
    # so one malformed entry becomes a thousand log lines a night. Carried as
    # the raw values (not the cleaned ones) because ArrayRemove has to match
    # what Firestore actually holds.
    requeue_rejected: List[object] = None
    # ⚠️ SAME LESSON, TWO MORE LISTS (2026-09-01). A malformed requeue id that
    # nobody can see is a book that does not come back; a malformed RECURRING
    # BLOCKER that nobody can see is the GPU running through hours the owner
    # blocked, and a malformed EXEMPT PROCESS is a start beside his game. Both
    # are worse than the requeue case, so both carry their raw rejects out for
    # the consumer to surface on the board and sweep from the document.
    recurring_rejected: List[object] = None
    exempt_rejected: List[object] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    readable: bool = True
    error: Optional[str] = None

    def __post_init__(self):
        # Normalised here rather than only in read_control(), so EVERY route
        # into this dataclass fails closed — including a test or a caller that
        # passes pause_mode="manual-only" (hyphen) and would otherwise get a
        # permissive pause out of a typo.
        self.pause_mode = normalise_pause_mode(self.pause_mode)
        # ⚠️ `is True`, NOT `bool(...)`. The dashboard is a different repo's
        # Worker and Firestore will hand back whatever it wrote: the string
        # "false" is truthy, so `bool("false")` would turn a field that says NO
        # into permission for this processor to end the owner's pause by itself.
        # Only the literal boolean unlocks it.
        self.pause_until_gpu_free = self.pause_until_gpu_free is True
        if self.pause_windows is None:
            self.pause_windows = []
        # ⚠️ CLEANED HERE AND NOT ONLY IN `read_control`, unlike `requeue` and
        # `priority_front` - and the asymmetry is deliberate. Those two are
        # inert lists of names; these two are GUARDS. A hand-built ControlState
        # (a test, `--ignore-control`, a future caller) carrying a half-typed
        # blocker must not be able to reach the evaluator at all, because a
        # blocker the evaluator cannot understand is an hour the owner blocked
        # and the machine ran anyway.
        kept, bad = clean_recurring_windows(self.recurring_windows,
                                            MAX_RECURRING_WINDOWS)
        self.recurring_windows = kept
        self.recurring_rejected = list(self.recurring_rejected or []) + bad
        kept, bad = clean_id_list(self.exempt_processes, MAX_EXEMPT_PROCESSES)
        self.exempt_processes = kept
        self.exempt_rejected = list(self.exempt_rejected or []) + bad
        if self.requeue is None:
            self.requeue = []
        if self.requeue_rejected is None:
            self.requeue_rejected = []
        if self.priority_front is None:
            self.priority_front = []


# ⚠️ Bounds on the two list fields, and they are LOW on purpose. These lists
# are typed by a person clicking rows on a dashboard, not generated - a
# thousand-entry `requeue` is a bug or an abuse, never an intention, and the
# cost of honouring one is a run that spends its whole start on bookkeeping.
# The excess is DROPPED WITH A LOG LINE rather than silently truncated.
MAX_REQUEUE = 200
MAX_PRIORITY_FRONT = 200

# ⚠️ TWENTY, AND FAR LOWER THAN THE 200 ABOVE, ON PURPOSE. A requeue list is a
# person clicking failed rows and can legitimately be long; a set of standing
# weekly quiet hours is a handful of rows typed once, and a set of
# do-not-disturb process names is the two or three games he actually plays.
# Anything past 20 of either is a bug or a paste accident upstream, and
# honouring it would mean 20 string comparisons per book against a list nobody
# meant. The excess is DROPPED WITH A LOG LINE and surfaced, never truncated
# silently - see `recurring_rejected` / `exempt_rejected`.
MAX_RECURRING_WINDOWS = 20
MAX_EXEMPT_PROCESSES = 20

# One control-list entry's sane maximum. A book id is a slug and a series name
# is a few words; anything longer is not a name this processor can match.
MAX_CONTROL_ENTRY_CHARS = 200


def clean_id_list(raw, limit: int) -> Tuple[List[str], List[str]]:
    """`(kept, rejected)` from whatever the dashboard actually wrote.

    ⚠️ DEFENSIVE IN THE SAME POSTURE AS `read_control()` ITSELF: this reads a
    document a *different repo's* Worker writes, so every assumption about its
    shape is somebody else's promise. A non-list, a list of numbers, a `None`
    entry, a 4 KB string - each becomes a dropped entry and a reported reason,
    never a crash and never a silently mangled id.

    Order is preserved and duplicates are dropped keeping the FIRST occurrence,
    because for `priority_front` the order IS the instruction.
    """
    if not isinstance(raw, (list, tuple)):
        return [], []
    kept: List[str] = []
    rejected: list = []
    seen = set()
    for entry in raw:
        # ⚠️ The RAW value is kept, not a repr. `clear_requeue` removes these
        # from the document with ArrayRemove, which matches on the stored value
        # - a prettified string would match nothing and the entry would survive
        # every sweep, warning once per book forever.
        if not isinstance(entry, str):
            rejected.append(entry)
            continue
        text = entry.strip()
        if not text or len(text) > MAX_CONTROL_ENTRY_CHARS:
            rejected.append(entry)
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        if len(kept) >= limit:
            rejected.append(entry)
            continue
        kept.append(text)
    return kept, rejected


# --------------------------------------------------------------------------
# 3b. recurring blockers - standing weekly quiet hours
# --------------------------------------------------------------------------
#
# Owner ask 2026-08-31, verbatim: *"then i can set blocker times that are
# reoccuring. for instance MTW 630-1015 I want ingestion paused."*
#
#     recurring_windows: [{days: [1,2,3], from: "18:30", until: "22:15"}]
#
# ⚠️ THESE ARE NOT `pause_windows`, AND THE DIFFERENCE IS THE WHOLE POINT.
# A `pause_window` is one dated interval with a full ISO instant at each end; it
# happens once and then it is over. A recurring blocker has NO date - it is a
# weekday set and two wall-clock times, and it means the same thing every week
# until the owner deletes the row. That is why the times here are bare "HH:MM"
# read as PHOENIX WALL CLOCK: a stored instant would drift a week later, and an
# offset-carrying string would invite exactly the timezone bug §5 of the
# contract doc exists to prevent (Arizona is a fixed UTC-7, no DST, so a wall
# clock is unambiguous here and would not be anywhere else).
#
# ⚠️ ABSOLUTE WHILE IN FORCE. Like `pause_windows` and for the same reason: a
# blocker IS a schedule the owner set, so nothing overrides it - not
# `pause_mode="manual_only"`, not a free GPU, not a soft pause's release, and
# not the nightly 12am-8am window either. A blocker that overlaps the window
# eats the overlap, and that consequence was put to the owner and accepted
# (design §4 Q2, 2026-08-31: *"pm and your rule is fine"*).

# "HH:MM" or "H:MM", 00:00-23:59. Deliberately NOT a general time parser: this
# field is written by a dashboard's two number inputs, and anything that does
# not look exactly like a clock reading is a mistake, not a dialect.
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse_hhmm(value) -> Optional[int]:
    """`"18:30"` -> 1110 minutes past Phoenix midnight, or None if it is junk.

    ⚠️ None means UNUSABLE, never midnight. A blocker whose `from` failed to
    parse must be DROPPED, not silently re-based to 00:00 - that would move a
    6:30 PM blocker to the top of the day and block the nightly window instead.
    """
    if not isinstance(value, str):
        return None
    m = _HHMM_RE.match(value.strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _clean_days(raw) -> Optional[List[int]]:
    """ISO weekday ints 1-7 (Monday=1), de-duplicated and sorted, or None.

    ⚠️ `isinstance(True, int)` is True in Python, so booleans are excluded
    explicitly - `days: [True, True]` would otherwise become "Monday" out of
    two values that were never a day at all.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    days = set()
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int):
            return None
        if not 1 <= entry <= 7:
            return None
        days.add(entry)
    return sorted(days) if days else None


def clean_recurring_windows(raw, limit: int = None) -> Tuple[List[dict], list]:
    """`(kept, rejected)` from whatever the dashboard actually wrote.

    ⚠️ SAME DEFENSIVE POSTURE AS `clean_id_list`, AND A SHARPER CONSEQUENCE.
    This document is written by a DIFFERENT repo's Worker, so its shape is
    somebody else's promise; a non-list, a missing key, a "6:30pm" string or a
    day numbered 0 each becomes a dropped entry and a REPORTED one, never a
    crash and never a half-understood blocker. The sharpness: a dropped requeue
    id is a book that waits a day, but a dropped BLOCKER is the GPU running
    through hours the owner reserved - which is why the rejects leave here as
    the RAW values, so the consumer can show them on the board and sweep them
    out of the document rather than re-warning once per book forever.

    Kept entries are NORMALISED to `{days: [int], from: "HH:MM", until: "HH:MM"}`
    with zero-padded times, so the evaluator and the words never re-parse.
    """
    limit = MAX_RECURRING_WINDOWS if limit is None else limit
    if not isinstance(raw, (list, tuple)):
        return [], []
    kept: List[dict] = []
    rejected: list = []
    for entry in raw:
        if not isinstance(entry, dict):
            rejected.append(entry)
            continue
        days = _clean_days(entry.get("days"))
        start = parse_hhmm(entry.get("from"))
        end = parse_hhmm(entry.get("until"))
        # ⚠️ `start == end` IS REJECTED, and it is the one judgement call here.
        # It is either a zero-length window or a 24-hour one and there is no way
        # to tell which was meant; guessing "24 hours" would block a whole day
        # off a typo, and guessing "zero" would silently do nothing. Dropping it
        # surfaces the row on the board where the owner can fix it.
        if days is None or start is None or end is None or start == end:
            rejected.append(entry)
            continue
        if len(kept) >= limit:
            rejected.append(entry)
            continue
        kept.append({"days": days,
                     "from": f"{start // 60:02d}:{start % 60:02d}",
                     "until": f"{end // 60:02d}:{end % 60:02d}"})
    return kept, rejected


def _clock_words(minutes: int) -> str:
    """1110 -> "6:30 PM". The owner reads a card, not a 24-hour timestamp."""
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix}"


def recurring_window_words(window: dict) -> str:
    """A blocker in the owner's own terms: "Mon Tue Wed 6:30 PM-10:15 PM Phoenix".

    ⚠️ The refusal has to NAME the window, not just say one bit. There can be
    several rows and they are invisible from the machine; "blocked by a
    recurring blocker" would leave him reading the log wondering which row he
    has to delete to run tonight.
    """
    days = " ".join(_WEEKDAY_NAMES[d - 1] for d in window.get("days") or [])
    start = parse_hhmm(window.get("from"))
    end = parse_hhmm(window.get("until"))
    if start is None or end is None:
        return days or "?"
    return f"{days} {_clock_words(start)}-{_clock_words(end)} Phoenix"


def recurring_window_in_force(windows, now: Optional[datetime] = None) -> Optional[dict]:
    """The first standing blocker covering `now`, or None. Phoenix wall clock.

    ⚠️ A WINDOW WITH `from` > `until` CROSSES MIDNIGHT, AND IT BELONGS TO THE
    DAY IT STARTS. `{days: [5], from: "22:00", until: "02:00"}` is Friday 10pm
    to Saturday 2am - one blocker, named for Friday. The alternative reading
    (the day applies to both ends) would make that row mean "Friday 10pm to
    Friday 2am", i.e. nothing, or 22 hours, depending on which end you believe.
    So Saturday 01:00 is tested against FRIDAY's membership, which is what
    `yesterday` is doing below - the single line of this function that is not
    obvious, and the one the tests pin from both sides of midnight.
    """
    now = now or phoenix_now()
    # ⚠️ PINNED TO PHOENIX HERE AND NOT LEFT TO THE CALLER. "18:30" is a WALL
    # CLOCK reading, so the only question this function may ever ask is "what
    # time is it in Phoenix" - a caller that handed us a UTC instant would
    # otherwise shift every blocker seven hours and, worse, shift the WEEKDAY
    # for anything after 5pm. A naive value is read as Phoenix for the same
    # reason `parse_iso` does: one household, one timezone, and reading it as
    # UTC would start the blocker seven hours early.
    now = now.replace(tzinfo=PHOENIX) if now.tzinfo is None else now.astimezone(PHOENIX)
    minute_of_day = now.hour * 60 + now.minute
    today = now.isoweekday()                 # Monday = 1, matching the field
    yesterday = today - 1 or 7
    for window in windows or []:
        start = parse_hhmm(window.get("from"))
        end = parse_hhmm(window.get("until"))
        days = window.get("days") or []
        if start is None or end is None or not days:
            continue
        if start < end:
            if today in days and start <= minute_of_day < end:
                return window
        else:
            # Crosses midnight: the tail of the named day, or the head of the
            # day after it.
            if today in days and minute_of_day >= start:
                return window
            if yesterday in days and minute_of_day < end:
                return window
    return None


# --------------------------------------------------------------------------
# 3c. do-not-disturb processes - "is the owner USING this machine?"
# --------------------------------------------------------------------------
#
# Owner, 2026-09-01, after the midnight window started batch-16 transcription
# beside his game: *"I was playing wow at midnight and the ingestion didnt
# pause. is there an alternate check I can add to make sure World of Warcraft
# is an exemption."*
#
# ⚠️ NO UTILISATION THRESHOLD COULD EVER HAVE CAUGHT THIS, which is why the
# answer is a new KIND of check rather than a stricter number. This module's own
# GPU guard already says it: a game paused on a menu still owns the card the
# moment it unpauses, and a loading screen reads 3%. Inside the window the GPU
# test is deliberately a single lenient poll ("at 2am the window IS the
# guarantee") - and 2026-09-01 falsified that guarantee. Process PRESENCE is the
# one signal an idle frame cannot fake.
#
# ⚠️ THIS GATES STARTS ONLY, like every other gate in this module. A book
# already transcribing when the game launches runs to the end and packs; killing
# it would waste the GPU-hours already spent and leave a WAV on disk.

# `tasklist` is on every Windows install, needs no package and no admin rights,
# and costs ~100 ms - which is why it is used rather than psutil (absent on the
# interpreter the nightly task runs; see the CPU guard's banner) or a WMI query.
_TASKLIST_NAME_RE = re.compile(r'^"([^"]*)"')

# ⚠️ THE SENTINEL IS NOT A PROCESS NAME AND MUST NEVER BE COMPARED AS ONE.
# `machine_in_use()` has THREE answers, not two: a name (in use), None (free),
# and "we could not tell" - and the third must not collapse into either of the
# first two. Returning None for an unreadable listing would start books beside
# the owner's game the day `tasklist` breaks; returning a plausible-looking name
# would put a lie in the log. It is an object with a readable repr so a stray
# print says what it is.
TASKLIST_UNREADABLE = "<process listing unreadable>"


def parse_tasklist_names(raw: str) -> List[str]:
    """Image names from `tasklist /FO CSV /NH` output.

    The image name is the FIRST quoted field of every row; the rest (PID,
    session, memory) is not looked at. A quoted-field regex rather than
    `split(",")` because window titles and memory figures carry commas inside
    their quotes on some locales, and slicing on the comma would cut a row in
    half and invent a process name out of the fragment.
    """
    names: List[str] = []
    for line in (raw or "").splitlines():
        m = _TASKLIST_NAME_RE.match(line.strip())
        if m and m.group(1).strip():
            names.append(m.group(1).strip())
    return names


def running_process_names(timeout: float = 20.0) -> Optional[List[str]]:
    """Every running image name, or None when the listing could not be read.

    ⚠️ None is NOT "nothing is running", exactly as the GPU parser's None is not
    "the card is idle". The caller turns it into IN USE.
    """
    try:
        proc = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    names = parse_tasklist_names(proc.stdout)
    return names or None


def machine_in_use(exempt_processes=None) -> Optional[str]:
    """The name of a do-not-disturb process that is running, or None, or the
    `TASKLIST_UNREADABLE` sentinel.

    ⚠️ AN EMPTY LIST SHORT-CIRCUITS AND SPAWNS NOTHING. This is asked once per
    book, and the overwhelmingly common case is a list nobody has filled in -
    138 EPUB starts a night must not each pay a subprocess to be told there was
    nothing to compare against. It is also why the empty case cannot be folded
    into the loop below: `running_process_names()` failing on a machine with no
    exempt list would otherwise report the whole night IN USE for a list that
    would have matched nothing.

    Matching is case-insensitive and EXACT on the image name: `Wow.exe` matches
    `WOW.EXE` (Windows filenames are case-insensitive, so the same binary can be
    listed either way) but never `wowlauncher.exe`. A substring match would let
    "wow" block on `PowerToys.exe`, and a guard that fires on the wrong process
    is a guard the owner deletes.
    """
    wanted = {str(name).strip().casefold()
              for name in (exempt_processes or []) if str(name).strip()}
    if not wanted:
        return None
    names = running_process_names()
    if names is None:
        return TASKLIST_UNREADABLE
    for name in names:
        if name.casefold() in wanted:
            return name
    return None


def machine_in_use_words(found: str) -> str:
    """The worded refusal. Never a bare boolean and never a bare process name."""
    if found == TASKLIST_UNREADABLE:
        return ("the running-process listing is unreadable (tasklist failed) - "
                "treating the machine as IN USE; no new starts. Clear the "
                "do-not-disturb list on the dashboard to escape this")
    return f"{found} is running - the machine is in use; no new starts"


def process_blocks_start(state: "ControlState") -> Optional[str]:
    """A worded reason the owner's presence refuses this start, or None.

    ⚠️ LOGGED WHEN IT BITES, because this is the one gate whose cause is
    invisible from the outside: nothing about "no books packed last night" says
    "a game was open", and without the line the owner re-reports the incident
    the fix was written for.
    """
    found = machine_in_use(state.exempt_processes)
    if found is None:
        return None
    words = machine_in_use_words(found)
    print(f"[ingest-control] {words}")
    return words


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


def _who_paused(state: ControlState) -> str:
    """Who set the pause, for the refusal line. `updated_by` is written by the
    dashboard as `estate-ops:<email>`; the processor writes "processor"."""
    who = (state.updated_by or "").strip()
    return f" (set by {who})" if who else ""


def control_blocks_start(state: ControlState, now: Optional[datetime] = None,
                         window_ok: bool = False) -> Optional[str]:
    """A worded reason a start is blocked, or None to proceed.

    Never a bare boolean: every refusal this estate makes has to say what
    happened and what would clear it, and these strings are what the log and the
    dashboard show.

    ⚠️ `window_ok` IS THE SCHEDULED/MANUAL DISTINCTION, AND ITS DEFAULT IS THE
    SAFE ONE (owner ask 2026-08-23 — see the PAUSE_MODES block). A pause set to
    `manual_only` yields ONLY to a start that is genuinely inside the nightly
    12am-8am window; `window_ok=False` — which is what every caller that does
    not pass it gets, including `--now`'s `_control_or_guard` — is a
    manual/interactive start and is refused under BOTH modes. That is the
    owner's second answer read literally: *"if scheduled window is fine to
    continue"* grants the WINDOW permission, never a person at a keyboard.

    ⚠️ THE MODE GOVERNS `paused` AND `paused_until` ONLY — NEVER `pause_windows`.
    A pause window IS a scheduled block (the owner's quiet hours), so letting
    "the scheduled window may continue" override one would make pause windows
    mean nothing at all. Nor does it touch `dont_check_until`, which is a
    spend-nothing instruction rather than a pause, or the unreadable-control
    branch, which must keep failing closed no matter what any field says.
    """
    now = now or phoenix_now()
    if not state.readable:
        return f"ingestion control unreadable ({state.error or 'unknown'}) - treating as PAUSED"

    # The manual pause and its timed twin both mean whatever `pause_mode` says
    # they mean. `window_ok` decides whether this particular start is the one
    # the owner exempted.
    mode = normalise_pause_mode(state.pause_mode)
    scheduled_exempt = mode == PAUSE_MODE_MANUAL_ONLY and window_ok
    who = _who_paused(state)

    if state.paused and not scheduled_exempt:
        if mode == PAUSE_MODE_MANUAL_ONLY:
            return ("paused by the dashboard - the scheduled 12am-8am window may "
                    f"continue, but this is a manual start{who}; clear the pause "
                    "there to run by hand")
        return (f"paused by the dashboard (paused=true, mode={mode}: "
                f"{pause_mode_words(mode)}){who}; clear it there to resume")

    until = parse_iso(state.paused_until)
    if until and now < until and not scheduled_exempt:
        if mode == PAUSE_MODE_MANUAL_ONLY:
            return (f"paused until {until.isoformat()} - the scheduled 12am-8am "
                    f"window may continue, but this is a manual start{who}")
        return (f"paused until {until.isoformat()} (dashboard paused_until, "
                f"mode={mode}: {pause_mode_words(mode)}){who}")

    # ⚠️ THE TWO KINDS OF SCHEDULED BLOCK, EVALUATED TOGETHER AND BOTH ABSOLUTE.
    # A one-shot `pause_window` is a dated interval; a `recurring_window` is a
    # standing weekly one. Neither consults `pause_mode`, `window_ok` or any
    # machine reading, for the same reason: a window IS a schedule the owner
    # set, and letting anything override one would make windows mean nothing.
    for window in state.pause_windows or []:
        start = parse_iso(window.get("from"))
        end = parse_iso(window.get("until"))
        if start and end and start <= now < end:
            return f"inside a scheduled pause window {start.isoformat()} -> {end.isoformat()}"

    recurring = recurring_window_in_force(state.recurring_windows, now)
    if recurring is not None:
        return (f"inside a recurring blocker ({recurring_window_words(recurring)}) - "
                "blockers are absolute; delete the row on the dashboard to run")
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
    requeue, requeue_bad = clean_id_list(data.get("requeue"), MAX_REQUEUE)
    priority, priority_bad = clean_id_list(data.get("priority_front"), MAX_PRIORITY_FRONT)
    # ⚠️ Reported, not swallowed. A dropped entry means the dashboard and this
    # processor disagree about a book the owner asked for by name, and the only
    # thing worse than dropping it is dropping it quietly.
    # ⚠️ The two guard lists are cleaned by `ControlState.__post_init__`, not
    # here, so that EVERY route into the dataclass gets the same treatment —
    # see the comment there. They are handed over raw and their rejects are
    # read back off the state below, which is why the warning loop moved after
    # the construction.
    state = ControlState(
        paused=bool(data.get("paused", False)),
        # Absent on every document written before 2026-08-23, and absent means
        # "all" — see normalise_pause_mode.
        pause_mode=normalise_pause_mode(data.get("pause_mode")),
        paused_until=data.get("paused_until"),
        pause_until_gpu_free=data.get("pause_until_gpu_free"),
        dont_check_until=data.get("dont_check_until"),
        pause_windows=list(data.get("pause_windows") or []),
        recurring_windows=data.get("recurring_windows"),
        exempt_processes=data.get("exempt_processes"),
        requeue=requeue,
        requeue_rejected=requeue_bad,
        priority_front=priority,
        updated_by=data.get("updated_by"),
        updated_at=data.get("updated_at"),
        readable=True,
    )
    for label, bad in (("requeue", requeue_bad), ("priority_front", priority_bad),
                       ("recurring_windows", state.recurring_rejected),
                       ("exempt_processes", state.exempt_rejected)):
        if bad:
            print(f"[ingest-control] WARN: {len(bad)} unusable {label} "
                  f"entr{'y' if len(bad) == 1 else 'ies'} dropped: "
                  f"{[repr(b)[:60] for b in bad[:5]]}")
    return state


def write_control(payload: dict, collection: str = None,
                  updated_by: str = "processor") -> bool:
    """Write ONLY the fields in `payload`. `True` when Firestore took it.

    ⚠️ `merge=True` IS THE MASK, AND IT IS LOAD-BEARING - never change it to a
    plain `set()`. The dashboard's own Worker writes this same document with an
    explicit `updateMask` for exactly this reason (contract §3b): a whole-
    document write from either side re-asserts fields the OTHER side owns, and
    the measured consequence there was books re-queued forever by a button
    nobody pressed. `merge=True` touches the named keys and leaves the rest of
    the document exactly as Firestore holds it, including a `requeue` entry the
    owner added between this run's read and this write.

    ⚠️ A FAILURE RETURNS FALSE, IT DOES NOT RAISE (2026-09-01). The soft-pause
    release calls this from inside `decide_start`, i.e. from inside a gate, and
    a gate that throws takes the whole night's run with it over a bookkeeping
    write. The caller's job is to fail CLOSED on a False - to stay paused this
    tick and say so - which is the clear-then-start rule the release depends on.
    """
    collection = collection or CONTROL_COLLECTION
    client = _firestore_client()
    if client is None:
        return False
    body = dict(payload)
    body["updated_by"] = updated_by
    body["updated_at"] = phoenix_now().isoformat()
    try:
        client.collection(collection).document(CONTROL_DOC).set(body, merge=True)
    except Exception as exc:
        print(f"[ingest-control] WARN: control write failed "
              f"({type(exc).__name__}: {str(exc)[:120]}); "
              f"fields not written: {sorted(payload)}")
        return False
    return True


def clear_requeue(consumed: List[str], collection: str = None) -> bool:
    """Remove exactly the ids this run acted on from `requeue`. `True` if written.

    ⚠️ ARRAY_REMOVE, NEVER `requeue: []`. The dashboard can write a new entry at
    any instant, including between this run's read and this write. Setting the
    field to an empty list would throw that entry away and the owner would press
    a button that did nothing, with nothing anywhere saying why. `ArrayRemove`
    deletes only the values named and leaves anything added since untouched.

    ⚠️ A FAILURE HERE IS DELIBERATELY NOT FATAL AND NOT RETRIED. The worst case
    is that the same ids are consumed again on the next run - which re-marks a
    pending book pending, i.e. nothing. Compare the alternative: raising here
    would let a Firestore blip stop a night's ingestion over bookkeeping.
    """
    if not consumed:
        return False
    collection = collection or CONTROL_COLLECTION
    client = _firestore_client()
    if client is None:
        return False
    try:
        from firebase_admin import firestore

        client.collection(collection).document(CONTROL_DOC).update(
            {"requeue": firestore.ArrayRemove(list(consumed))}
        )
        return True
    except Exception as exc:
        # Named, because an uncleared requeue is a control that looks stuck.
        print(f"[ingest-control] WARN: could not clear {len(consumed)} requeue "
              f"entr{'y' if len(consumed) == 1 else 'ies'} ({type(exc).__name__}: "
              f"{str(exc)[:120]}); they will be re-applied next run, which is a no-op")
        return False


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
    # ⚠️ TRUE WHEN A DIFFERENT BOOK COULD STILL PASS THIS REFUSAL.
    # Every refusal this module made before 2026-08-18 was GLOBAL - a pause, a
    # busy machine, the wrong hour - so the caller correctly stopped the whole
    # run on any of them. The deadline gate broke that assumption: "this book is
    # too long to finish by 08:00" says nothing about the 40-minute book behind
    # it. A caller that stops on an item-specific refusal throws away the rest of
    # the night for one long book.
    item_specific: bool = False


def _decide_cpu_only(state: ControlState, window_ok: bool, now: datetime, sleep,
                     allow_opportunistic: bool = True) -> StartDecision:
    """EPUB, text-PDF and pack-only work: three of the six gates, and here is why.

    Exempt from the GPU guard - waiting for a graphics card to idle before
    parsing a zip file would be theatre. Exempt from the DEADLINE too, because
    this work is measured in seconds and "will it finish in time" has one
    answer. NOT exempt from the CPU guard: the processor is exactly what it
    competes for, and 138 EPUBs is real work on the machine a person is using.

    ⚠️ AND NOT EXEMPT FROM THE PRESENCE GUARD EITHER, WHICH IS A DELIBERATE
    CHOICE AND NOT AN OVERSIGHT (owner, 2026-09-01: *"block everything"*). It
    would be easy to argue that parsing an EPUB cannot disturb a game. But the
    CPU guard's own 75% bar is a threshold, and thresholds are exactly what the
    WoW incident proved insufficient - the machine looked idle and was not. The
    owner asked for "the machine is in use" to mean nothing new starts, so the
    lane split does not happen here. If packing beside a game ever proves
    wanted, that is a per-lane setting on the card, not a silent exception.
    ⚠️ It runs BEFORE `cpu_guard()` because tasklist is ~100 ms and a CPU
    confirm can sleep 30 s: a refusal the cheap gate can make must not pay for
    the expensive one's.

    ⚠️ AND IT HAS AN OPPORTUNISTIC PATH, WHICH IT DID NOT UNTIL 2026-08-18.
    🔴 THE BUG THAT ADDED IT, because it will read as a harmless simplification
    to whoever tidies this next: this branch consulted `may_start_new_book()`
    and NEVER LOOKED AT `allow_opportunistic` AT ALL. So a daytime
    `--opportunistic` fire refused CPU-only work with "outside the window" - and
    since the caller treated that as a global stop, ONE pack-only book at the
    head of the queue killed the entire run behind it. Measured live at 15:30
    on 2026-08-18: 1,053 books queued, 1,028 of them GPU work on an idle card
    inside the owner's authorised window, and the run packed nothing because the
    first item happened to already have its transcript on disk.
    Refusing this work outside the window was never defensible on its own terms
    either: it costs no GPU and seconds of CPU, and the GPU path has borrowed
    idle daytime since the beginning.
    """
    in_use = process_blocks_start(state)
    if in_use:
        return StartDecision(False, in_use, batch_size_for(now))
    cpu = cpu_guard(sleep=sleep)
    if cpu.busy:
        return StartDecision(False, cpu_busy_words(cpu), batch_size_for(now),
                             cpu_pct=cpu.pct)
    if window_ok:
        return StartDecision(True, f"in window; GPU-guard exempt, CPU at {cpu.pct:.0f}%",
                             batch_size_for(now), cpu_pct=cpu.pct)
    if not allow_opportunistic:
        return StartDecision(False,
                             "outside the 00:00-07:45 Phoenix window "
                             "(opportunistic runs disabled)",
                             batch_size_for(now), cpu_pct=cpu.pct)
    return StartDecision(True,
                         f"opportunistic: no GPU needed, CPU at {cpu.pct:.0f}%",
                         batch_size_for(now), cpu_pct=cpu.pct, opportunistic=True)


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


# --------------------------------------------------------------------------
# 4b. the soft pause, and the processor's own release of it
# --------------------------------------------------------------------------
#
# Owner ask 2026-08-31, verbatim: *"i want any pause thats not the 'until i
# unpause' to be unpaused by either next scheduled start or the next gpu free
# availability"*.
#
# The encoding (design §3, and it is the only shape §3 of the contract doc
# permits): `paused: false` + `paused_until: <ceiling>` + `pause_until_gpu_free:
# true`. The flag stays OFF because `paused: true` is unconditional and never
# consults a timer - a self-ending pause written flag-ON would still be paused
# at 12:01 forever. The dashboard computes the ceiling at write time (the next
# 00:00 Phoenix for the bare button), so the "next scheduled start" release
# needs no new reader behaviour at all; the GPU release below is the one new
# thing this reader does.
#
# ⚠️ CLEAR-THEN-START, NEVER START-THEN-CLEAR. If the write that clears the
# pause fails, this tick stays paused and says why. Running books while the
# card still reads "paused" is the dishonest-board state the whole surface
# exists to prevent, and it is worse than a night of lost ingestion because the
# owner's next move is to distrust the card.
#
# ⚠️ AND `dont_check_until` STILL WINS. A don't-check is a spend-nothing
# instruction and polling the GPU is spending; the release lives below it in
# `decide_start` so a deferred tick costs no nvidia-smi and no tasklist.


def soft_pause_in_force(state: ControlState, now: Optional[datetime] = None,
                        window_ok: bool = False) -> bool:
    """True when a releasable SOFT pause is the ONLY thing blocking a start.

    ⚠️ "THE ONLY THING" IS THE LOAD-BEARING WORD, AND IT IS WHY THIS ASKS
    `control_blocks_start` A SECOND QUESTION RATHER THAN RE-DERIVING ONE. A
    recurring blocker, a one-shot pause window, a hard `paused: true` or an
    unreadable control must NOT be probed at, argued with, or released: they
    are absolute, and a release path that quietly out-ranked one would make
    every other guard in this module advisory. So the test is literal - take
    the state, take the soft ceiling OUT of it, and ask the real gate whether
    anything else still refuses. Only a state that then says "go" is a state
    whose pause this processor is allowed to end.
    """
    if not state.readable or not state.pause_until_gpu_free:
        return False
    now = now or phoenix_now()
    until = parse_iso(state.paused_until)
    if not until or now >= until:
        return False
    from dataclasses import replace

    without = replace(state, paused_until=None, pause_until_gpu_free=False)
    return control_blocks_start(without, now, window_ok=window_ok) is None


def _release_soft_pause(state: ControlState, now: datetime, sustained_polls: int,
                        sleep) -> Tuple[bool, str]:
    """Try to end a soft pause. `(released, words)`; the words refuse or explain.

    The release condition is BOTH halves and neither alone: the GPU quiet on a
    sustained reading, AND no do-not-disturb process running. The GPU half on
    its own re-creates the incident exactly - a game sitting on a menu or
    between loading screens reads 3% twice in a row, two minutes apart, and the
    pause the owner set because he was playing would clear itself.

    ⚠️ SUSTAINED-FREE, NOT A SINGLE POLL (design Q3, owner: *"your choice"*).
    Two polls 120 s apart under 50% - deliberately the same `gpu_sustained_free`
    bar the opportunistic daytime path uses, so this module has exactly ONE
    definition of "the GPU is free". The cost is up to ~4 minutes of release
    lag, paid once per soft pause, and it buys the guarantee that a loading
    screen cannot unpause a game.

    ⚠️ ON SUCCESS THE IN-MEMORY STATE IS MUTATED TO MATCH WHAT WAS WRITTEN.
    Not cosmetic: the caller re-asks `control_blocks_start` after this returns,
    and a state still carrying the ceiling would refuse the start we just paid
    four minutes to earn - and the document would say running while this tick
    said paused, which is the same dishonest board in mirror image.
    """
    until = parse_iso(state.paused_until)
    ceiling = f"at latest {until:%H:%M}" if until else "at latest its ceiling"

    found = machine_in_use(state.exempt_processes)
    if found is not None:
        return False, (f"soft-paused - {machine_in_use_words(found)}; releases when "
                       f"the GPU is quiet and the machine is free, {ceiling}")

    util = gpu_utilisation()
    if util is None:
        return False, ("soft-paused - GPU utilisation unreadable, treating as busy; "
                       f"releases when the GPU is quiet, {ceiling}")
    if util > GPU_BUSY_PCT:
        return False, (f"soft-paused - GPU at {util}% (> {GPU_BUSY_PCT}%); releases "
                       f"when the GPU is quiet, {ceiling}")
    if not gpu_sustained_free(polls=sustained_polls, sleep=sleep):
        return False, (f"soft-paused - the GPU did not stay under {GPU_BUSY_PCT}% "
                       f"across {sustained_polls} polls {GPU_POLL_SECONDS:.0f}s apart; "
                       f"releases when the GPU is quiet, {ceiling}")

    # ⚠️ The write comes BEFORE the start, and a failed write keeps the pause.
    if not write_control({"paused_until": None, "pause_until_gpu_free": False}):
        return False, ("soft-paused - the GPU is free but clearing the pause on the "
                       "dashboard FAILED (no Firestore client, or the write was "
                       "refused); staying paused this tick rather than running while "
                       "the card still says paused")

    state.paused_until = None
    state.pause_until_gpu_free = False
    print(f"[ingest-control] soft pause released: GPU sustained-free at {util}%, "
          f"no do-not-disturb process running (ceiling was {ceiling[9:]})")
    return True, f"soft pause released - GPU sustained-free at {util}%"


def decide_start(state: ControlState, now: Optional[datetime] = None,
                 needs_gpu: bool = True, allow_opportunistic: bool = True,
                 sustained_polls: int = 2, sleep=time.sleep,
                 audio_seconds: Optional[float] = None,
                 factors: Optional[List[float]] = None) -> StartDecision:
    """Should a new book start right now? One place, six gates, worded.

    ⚠️ Order matters and is chosen so the cheapest and most authoritative gate
    runs first: the owner's don't-check beats everything (it exists precisely to
    stop us spending anything), then the pause and the blockers, then the clock,
    then the DEADLINE - which is pure arithmetic and can refuse before a single
    subprocess runs - then the PRESENCE guard (~100 ms of tasklist), then the
    GPU, and finally the CPU, which is the only gate that may sleep 30 s to
    confirm a refusal.

    ⚠️ THE ONE PLACE THAT ORDER INVERTS IS THE SOFT PAUSE, ON PURPOSE. Every
    other pause means "do not even look at the machine"; a soft pause's whole
    definition is that a machine reading ENDS it, so its release is the only
    thing in this function that polls while a pause is nominally in force. It
    still sits below `dont_check_until` (a spend-nothing instruction outranks a
    probe) and below the blockers and the hard pause (`soft_pause_in_force`
    asks the real gate whether anything else refuses, and a state where
    something else does is never probed at).

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

    # ⚠️ MOVED ABOVE THE PAUSE CHECK 2026-08-23, and the order now matters.
    # `window_ok` used to be computed after the pause gate because the pause
    # gate had no use for it; a `manual_only` pause is exactly the case where
    # it does. `may_start_new_book()` is pure clock arithmetic with no I/O, so
    # computing it first costs nothing and cannot change any other gate.
    window_ok = may_start_new_book(now)

    blocked = control_blocks_start(state, now, window_ok=window_ok)
    if blocked:
        # A soft pause is the one refusal this processor may answer rather than
        # merely report - and only when it is the ONLY refusal standing.
        if soft_pause_in_force(state, now, window_ok=window_ok):
            released, words = _release_soft_pause(state, now, sustained_polls, sleep)
            if not released:
                return StartDecision(False, words, batch_size_for(now))
            # ⚠️ Re-asked, not assumed. `_release_soft_pause` mutated the state
            # to match the document it just wrote, and the minutes it spent
            # polling can have carried us into a blocker that was not in force
            # when this tick began.
            blocked = control_blocks_start(state, now, window_ok=window_ok)
        if blocked:
            return StartDecision(False, blocked, batch_size_for(now))

    if not needs_gpu:
        return _decide_cpu_only(state, window_ok, now, sleep, allow_opportunistic)

    estimate = estimate_against_deadline(state, now, audio_seconds, factors)
    if not estimate.fits:
        return StartDecision(False, estimate.words, batch_size_for(now),
                             est_finish=estimate.finish, boundary=estimate.boundary,
                             item_specific=True)

    # ⚠️ THE LINE THAT IS THE FIX FOR 2026-09-01, and it sits HERE - above the
    # GPU clearance, below the deadline - for two reasons. Above the GPU,
    # because inside the window that clearance is a single lenient poll and the
    # incident is precisely a game the poll cannot see. Below the deadline,
    # because the deadline is free arithmetic and near a boundary every
    # remaining book refuses; paying a subprocess per refusal there would spend
    # 25 tasklists to learn what the clock already said.
    in_use = process_blocks_start(state)
    if in_use:
        return StartDecision(False, in_use, batch_size_for(now))

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
