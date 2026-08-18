"""Third copy of the transcripts: the GATED R2 bucket, beside the packs.

WHY THIS EXISTS
---------------
Transcripts are the GPU-hours artifact. The owner, 2026-08-18: *"we lose this
data we lose it all"* — every one of them is hours of a graphics card that
cannot be got back by re-running a script, only by re-running the clock. Until
now they had exactly two copies, both on this machine's blast radius: the local
disk and the Drive mirror that follows it. Measured the day this was built:
0.21 GB across 32 files, heading for ~13 GB as the corpus completes.

This module adds the third, in `ebooks-gated` under `transcripts/<stem>.json.gz`,
which is the SAME BUCKET AND THE SAME PRIVACY CLASS AS THE PACKS.

⚠️ NEVER A PUBLIC BUCKET, AND THE REASON IS NOT PRUDERY. A transcript is the
book as text — the whole of it, in order. `audiobook-covers` has a public r2.dev
URL, so an object put there is world-readable the moment it lands. The owner's
words about this class of data: *"this is data that could lead to piracy if it
were to get out"*. `ebooks-gated` is private and sits behind the audiobook
Worker's auth, which is why the packs live there and why these do too.

⚠️ TRANSPORT IS WRANGLER OAUTH, LIKE EVERY OTHER `ebooks-gated` PUBLISHER, and
the estate R2 API token's `AccessDenied` on this bucket is a DESIGN FEATURE, not
a misconfiguration to fix. Do not widen that token to make an upload work. This
module calls `r2_put`/`r2_get_bytes` from `ingest_pack`, so there is one way to
reach the bucket rather than two.

WHAT IS UPLOADED, AND WHAT IS DELIBERATELY NOT
-----------------------------------------------
The `.json` only. The `.txt` beside it is NOT uploaded, and that is a measured
decision rather than an oversight: it is a pure rendering of the json's segments
and `render_txt()` below reproduces it. MEASURED 2026-08-18 across all 16
transcripts then on disk — **16 of 16 reconstructed BYTE-FOR-BYTE**, so the
`.txt` is 9% more bytes (~1 GB at full corpus) carrying no information the json
does not already hold. Storing a derived artifact beside its source invites the
two to drift; keeping the renderer as code cannot drift.

⚠️ `whisper-venv/` AND `work/` ARE NEVER UPLOADED. `work/` holds the 2 GB
intermediate WAV and the progress file; `whisper-venv/` is a Python environment.
Neither is an artifact and both would dwarf the thing that is. This module only
ever reads `TRANSCRIPTS_DIR/*.json`, so the exclusion is structural rather than a
list somebody has to remember to maintain.

IDEMPOTENCE, AND THE HONEST LIMIT OF IT
---------------------------------------
Uploads are content-addressed by the sha256 of the GZIPPED BYTES, recorded in a
local ledger (`transcripts_uploaded.json`) only ever written AFTER a `put`
returns success. Re-running skips anything whose local digest already matches
its ledger entry, so a backfill is safe to run repeatedly and a nightly re-upload
of 13 GB cannot happen by accident.

⚠️ THE LEDGER IS A RECORD OF WHAT WAS PUT, NOT A READING OF WHAT IS STORED, and
a reader deserves to know the difference. `wrangler r2 object` offers only
get/put/delete — there is NO head/info — so the only way to read a stored
object's digest is to download the whole object. At 13 GB that is not a check,
it is a second copy of the transfer. `verify_round_trip()` does exactly that
download-and-compare for ONE object, which is what a spot check is for; it is
not run over the corpus. So: "skipped" means *this machine uploaded these exact
bytes and recorded it*, which is a weaker claim than *R2 currently holds them*,
and the gap is closed by sampling rather than by assertion.

The gzip is written with `mtime=0`, the same as `write_pack_gz`, so identical
content produces identical bytes and the digest is stable across runs instead of
changing every night for no reason.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

from app.core.ingest_pack import r2_get_bytes, r2_put
from app.core.ingest_queue import TRAINING_ROOT, TRANSCRIPTS_DIR

TRANSCRIPT_PREFIX = "transcripts/"
LEDGER_PATH = TRAINING_ROOT / "transcripts_uploaded.json"


def transcript_key(stem: str) -> str:
    return f"{TRANSCRIPT_PREFIX}{stem}.json.gz"


def render_txt(segments: Iterable[dict]) -> str:
    """Rebuild the `.txt` from the json's segments.

    ⚠️ THIS IS THE RECOVERY PATH FOR THE FILE WE CHOOSE NOT TO UPLOAD, so the
    format string is load-bearing and must not be tidied. It is copied from
    `estate-training-data/work/_whisper_worker.py`, which is the only thing that
    writes these files, and verified byte-for-byte against all 16 transcripts on
    disk on 2026-08-18.
    """
    out = []
    for s in segments:
        start = s["start"]
        out.append("[%02d:%02d:%06.3f] %s\n" % (
            start // 3600, start % 3600 // 60, start % 60, s["text"].strip()))
    return "".join(out)


def gzip_bytes(path: Path) -> bytes:
    """Deterministic gzip of a file's bytes — `mtime=0`, same as write_pack_gz.

    Without the fixed mtime the container changes every run even when the
    content does not, which would make the digest useless for skipping and turn
    every backfill into a full re-upload.
    """
    raw = path.read_bytes()
    buf = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(raw)
    buf.seek(0)
    return buf.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_ledger(path: Path = LEDGER_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_ledger(ledger: dict, path: Path = LEDGER_PATH) -> None:
    """tmp-then-rename: a killed run never leaves half a ledger, which would
    read as 'never uploaded' and cost a re-transfer, or worse as a truncated
    JSON that load_ledger() discards wholesale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def upload_transcript(json_path: Path, ledger: Optional[dict] = None,
                      force: bool = False) -> dict:
    """Upload one transcript if its bytes are not already recorded as uploaded.

    Returns `{"stem", "status", ...}` where status is `uploaded`, `skipped` or
    `failed`. NEVER RAISES — see the soft-fail note in `ingest_books.pack_one`:
    a backup copy that can stop an ingest run is a liability, not a safety net.
    """
    stem = json_path.stem
    result = {"stem": stem, "key": transcript_key(stem)}
    try:
        data = gzip_bytes(json_path)
        digest = sha256(data)
        result["gz_bytes"] = len(data)
        result["sha256"] = digest

        recorded = (ledger or {}).get(stem) if ledger is not None else None
        if not force and isinstance(recorded, dict) and recorded.get("sha256") == digest:
            result["status"] = "skipped"
            return result

        with tempfile.TemporaryDirectory() as tmpdir:
            gz_path = Path(tmpdir) / f"{stem}.json.gz"
            gz_path.write_bytes(data)
            r2_put(result["key"], gz_path)

        if ledger is not None:
            # Written only after the put returned success, so the ledger can
            # under-claim (costing a re-upload) but never over-claim.
            ledger[stem] = {
                "sha256": digest,
                "gz_bytes": len(data),
                "source_bytes": json_path.stat().st_size,
                "key": result["key"],
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        result["status"] = "uploaded"
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result


def verify_round_trip(json_path: Path) -> dict:
    """Download the stored object, gunzip it, and byte-compare with local.

    ⚠️ THE ONLY CHECK THAT READS R2 RATHER THAN THE LEDGER. Deliberately
    one-object: at 13 GB a corpus-wide verify is a second copy of the transfer,
    not a check. Use it to sample.
    """
    stem = json_path.stem
    out = {"stem": stem, "key": transcript_key(stem)}
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "got.json.gz"
        if not r2_get_bytes(out["key"], dest):
            out["ok"] = False
            out["error"] = "object could not be fetched"
            return out
        stored = dest.read_bytes()
        out["stored_gz_bytes"] = len(stored)
        out["stored_gz_sha256"] = sha256(stored)
        try:
            inflated = gzip.decompress(stored)
        except Exception as exc:
            out["ok"] = False
            out["error"] = f"stored object did not gunzip: {exc}"
            return out
        local = json_path.read_bytes()
        out["local_bytes"] = len(local)
        out["inflated_bytes"] = len(inflated)
        out["ok"] = inflated == local
        if not out["ok"]:
            out["error"] = "inflated bytes differ from the local transcript"
    return out


def transcripts_on_disk(directory: Path = TRANSCRIPTS_DIR,
                        ledger_path: Path = LEDGER_PATH) -> list[Path]:
    """Every transcript json. `work/` and `whisper-venv/` are siblings of this
    directory and are never globbed, which is why the exclusion needs no list.

    ⚠️ THE LEDGER IS EXCLUDED BY NAME. In production it lives one level up, so
    the two never meet — but pointed at one directory (as a test or a
    reorganised tree may do) the glob would pick up the ledger, upload it as
    though it were a transcript, and thereby CHANGE the ledger, so the next run
    would upload it again forever. Caught by a test, not by review.
    """
    if not directory.exists():
        return []
    skip = {ledger_path.name, ledger_path.with_suffix(".json.tmp").name}
    return sorted(p for p in directory.glob("*.json") if p.name not in skip)


def backfill(directory: Path = TRANSCRIPTS_DIR, ledger_path: Path = LEDGER_PATH,
             force: bool = False, limit: Optional[int] = None,
             on_result=None) -> dict:
    """Upload every transcript not already recorded. Idempotent."""
    ledger = load_ledger(ledger_path)
    paths = transcripts_on_disk(directory, ledger_path)
    if limit:
        paths = paths[:limit]

    uploaded, skipped, failed = [], [], []
    for path in paths:
        res = upload_transcript(path, ledger, force=force)
        {"uploaded": uploaded, "skipped": skipped, "failed": failed}[res["status"]].append(res)
        if on_result:
            on_result(res)
        # Saved as we go: a run killed halfway keeps the copies it already made
        # instead of re-uploading them next time.
        if res["status"] == "uploaded":
            save_ledger(ledger, ledger_path)

    save_ledger(ledger, ledger_path)
    return {"uploaded": uploaded, "skipped": skipped, "failed": failed,
            "total": len(paths)}
