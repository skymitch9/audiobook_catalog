"""Back up the transcripts to the gated R2 bucket, and spot-check one.

The transcripts are the GPU-hours artifact — the owner's *"we lose this data we
lose it all"*. This is the one-shot backfill for everything already on disk; the
nightly path is `pack_one()` in `app/tools/ingest_books.py`, which uploads each
transcript as a soft-fail step right after its pack.

See `app/core/ingest_transcripts.py` for the design — the gated-bucket rule, the
wrangler-OAuth transport, why the `.txt` is not uploaded, and the honest limit of
the ledger's idempotence.

USAGE
-----
    python -m scripts.upload_transcripts_r2 --dry-run   # what would go, nothing sent
    python -m scripts.upload_transcripts_r2             # upload what is missing
    python -m scripts.upload_transcripts_r2 --verify i_m_glad_my_mom_died
                                                        # round-trip ONE object

⚠️ `--verify` DOWNLOADS THE OBJECT. That is the point of it — it is the only
check that reads R2 rather than the local ledger — but it means verifying the
whole corpus would re-transfer the whole corpus. Sample; do not sweep.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.ingest_transcripts import (
    LEDGER_PATH, backfill, gzip_bytes, load_ledger, sha256, transcript_key,
    transcripts_on_disk, verify_round_trip,
)
from app.core.ingest_queue import TRANSCRIPTS_DIR


def _mb(n: int) -> str:
    return f"{n / 1048576:.2f} MB"


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be uploaded; send nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-upload even when the ledger says the bytes match")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--verify", metavar="STEM",
                    help="download ONE stored object, gunzip it and byte-compare")
    ap.add_argument("--dir", type=Path, default=TRANSCRIPTS_DIR)
    args = ap.parse_args(argv)

    if args.verify:
        path = args.dir / f"{args.verify}.json"
        if not path.exists():
            print(f"[transcripts] no such transcript: {path}")
            return 2
        res = verify_round_trip(path)
        for k, v in res.items():
            print(f"  {k}: {v}")
        # ⚠️ The exit code carries the verdict so a caller cannot read "ok:
        # False" as success just because the command printed something.
        return 0 if res.get("ok") else 1

    paths = transcripts_on_disk(args.dir)
    if not paths:
        print(f"[transcripts] nothing under {args.dir}")
        return 0

    if args.dry_run:
        ledger = load_ledger()
        todo = pending = 0
        for path in paths:
            digest = sha256(gzip_bytes(path))
            rec = ledger.get(path.stem)
            if not args.force and isinstance(rec, dict) and rec.get("sha256") == digest:
                pending += 1
                continue
            todo += 1
            print(f"  + {transcript_key(path.stem)}  ({_mb(path.stat().st_size)} raw)")
        print(f"[transcripts] DRY RUN — {todo} to upload, {pending} already recorded "
              f"({len(paths)} on disk). Ledger: {LEDGER_PATH}")
        return 0

    def report(res):
        if res["status"] == "uploaded":
            print(f"  + {res['key']}  {_mb(res['gz_bytes'])} gz  {res['sha256'][:12]}")
        elif res["status"] == "failed":
            print(f"  ! {res['key']}  FAILED: {res.get('error')}")

    out = backfill(args.dir, force=args.force, limit=args.limit, on_result=report)
    print(f"[transcripts] {len(out['uploaded'])} uploaded, "
          f"{len(out['skipped'])} already there, {len(out['failed'])} failed "
          f"({out['total']} on disk)")
    # ⚠️ A failure must be visible in the exit code. This script is a BACKUP
    # step; a silent partial backup is the failure mode that matters.
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
