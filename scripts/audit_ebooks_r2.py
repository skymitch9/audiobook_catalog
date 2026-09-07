"""Audit the private `estate-ebooks` R2 bucket against our record of it.

⚠️ WHY THIS EXISTS: until now nothing had ever READ the bucket. `site/
ebook_files_manifest.json` is `scripts/upload_ebooks_r2.py`'s record of its own
PUTs — every count quoted anywhere in the docs ("168 objects in the bucket")
traces back to that file, which is the uploader's memory of what it believes it
did, not a measurement of what is there. Viewer phase 0a listed this as its one
open step, and `docs/access/RECOVERY.md` quotes a bucket figure it never took.

The gap stopped being cosmetic on 2026-09-05, when `audiobook-worker`'s
`GET /api/ebook/:anchor/file` shipped: those objects are what the live reader
streams bytes from, so an object the record claims and the bucket lacks is a
404 in somebody's reader, and no local check can see it.

⚠️ wrangler still has no `r2 object list` (checked against 4.123.0), which is
why this goes through R2's S3-compatible endpoint with `boto3` instead. It
needs the same three variables `scripts/upload_ebooks_r2.py`'s multipart path
already uses, and reads them exactly the same way — `load_dotenv()` then
`os.environ`. Nothing here prints, logs or writes a credential value.

    R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY

READ-ONLY, DELIBERATELY AND PERMANENTLY. It issues `ListObjectsV2` and nothing
else — no PUT, no DELETE, no repair. An audit that can also fix things is an
audit nobody can run when they are unsure, and "the tool that reads the bucket"
is exactly the tool you want to be able to run when you are unsure. Anything it
finds is fixed by re-running `scripts/upload_ebooks_r2.py`, which owns writes.

Usage:
    python scripts/audit_ebooks_r2.py             # human table
    python scripts/audit_ebooks_r2.py --json      # machine-readable
    python scripts/audit_ebooks_r2.py --limit 20  # cap the per-section listing

Exit code: 0 = the record and the bucket agree; 1 = at least one object the
record claims is NOT in the bucket (the case that 404s a reader); 2 = the
bucket could not be listed at all, which is NOT a clean bill of health and must
never be reported as one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:  # the repo's own pattern — see scripts/drive_audit.py, health_check.py
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover - env may already be exported
    pass

RECORD_PATH = PROJECT_ROOT / "site" / "ebook_files_manifest.json"
BUCKET = os.getenv("EBOOKS_R2_BUCKET", "estate-ebooks")
S3_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_UNREACHABLE = 2


# ---------------------------------------------------------------------------
# pure helpers — pinned by tests/test_audit_ebooks_r2.py
# ---------------------------------------------------------------------------
def diff_keys(record: Dict[str, dict], bucket: Dict[str, dict]) -> dict:
    """Compare our record of the bucket against the bucket.

    Three answers, and they are NOT interchangeable — each has a different
    cause and a different fix, so they are never summed into one "drift" count:

      · `missing`  — the record claims it, the bucket does not have it. ⚠️ THE
                     ONE THAT HURTS: the reader resolves anchor → path from the
                     gated manifest and asks for this key, so a missing object
                     is a 404 in somebody's book. Fix: re-run the uploader.
      · `orphans`  — in the bucket, not in the record. Harmless and expected:
                     a book re-filed under a new path leaves its old object
                     behind, nothing links to it (the anchor moved with the
                     path), and R2 bills against a 10 GB-month free tier.
      · `size_mismatch` — both sides have the key and disagree on the byte
                     count. Rare and worth a hard look: it means a PUT landed
                     on top of a different file, or a truncated upload was
                     recorded as complete.

    ⚠️ A record entry with NO size is reported as `unknown`, never compared as
    zero. Absent is not zero — the same rule the processing board's lane counts
    follow, and here a zero-compare would invent a mismatch on every entry
    written before the size field existed.
    """
    rec_keys = set(record)
    buc_keys = set(bucket)

    size_mismatch: List[dict] = []
    for key in sorted(rec_keys & buc_keys):
        want = record[key].get("size")
        got = bucket[key].get("size")
        if not isinstance(want, int) or not isinstance(got, int):
            continue
        if want != got:
            size_mismatch.append({"key": key, "record_bytes": want, "bucket_bytes": got})

    return {
        "missing": sorted(rec_keys - buc_keys),
        "orphans": sorted(buc_keys - rec_keys),
        "size_mismatch": size_mismatch,
        "agreed": len(rec_keys & buc_keys),
    }


def total_bytes(entries: Dict[str, dict]) -> Optional[int]:
    """Sum of `size`, or None when ANY entry does not carry one.

    ⚠️ Deliberately all-or-nothing. A total summed over the entries that
    happened to have a size is a smaller number wearing a complete one's
    clothes, and it would be compared against the bucket's real total.
    """
    out = 0
    for v in entries.values():
        size = v.get("size")
        if not isinstance(size, int):
            return None
        out += size
    return out


def human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    step = 1024.0
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < step or unit == "TiB":
            return f"{val:,.1f} {unit}" if unit != "B" else f"{int(val):,} B"
        val /= step
    return f"{n} B"


def missing_env() -> Tuple[str, ...]:
    return tuple(name for name in S3_ENV if not os.environ.get(name))


# ---------------------------------------------------------------------------
def load_record(path: Path = RECORD_PATH) -> Dict[str, dict]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    files = payload.get("files")
    if not isinstance(files, dict):
        raise ValueError(f"{path} has no `files` map — it is not an uploader record")
    return {k: v for k, v in files.items() if isinstance(v, dict)}


def list_bucket(bucket: str = BUCKET) -> Dict[str, dict]:
    """Every object in the bucket, keyed on its object key.

    Paginated with `list_objects_v2`'s continuation token rather than a single
    call: R2 caps a page at 1,000 keys and a truncated page read as the whole
    bucket would report every key past the first thousand as missing — a
    silent, confident, wrong answer, which is the failure mode this whole
    script exists to end.
    """
    import boto3  # imported here so --help works without it

    account = os.environ["R2_ACCOUNT_ID"]
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    out: Dict[str, dict] = {}
    token = None
    pages = 0
    while True:
        kwargs = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        pages += 1
        for obj in resp.get("Contents") or []:
            out[obj["Key"]] = {
                "size": obj.get("Size"),
                "etag": str(obj.get("ETag") or "").strip('"'),
                "last_modified": obj["LastModified"].isoformat() if obj.get("LastModified") else None,
            }
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:  # pragma: no cover - defensive; a truncated page with no token
            raise RuntimeError(
                f"{bucket}: the listing said it was truncated but gave no continuation token "
                f"after {pages} page(s) — refusing to report a partial listing as complete"
            )
    return out


def _print_section(title: str, keys: List[str], limit: int, detail=None) -> None:
    if not keys:
        return
    print(f"\n{title} ({len(keys)}):")
    for key in keys[:limit]:
        print(f"  {key}{detail(key) if detail else ''}")
    if len(keys) > limit:
        print(f"  … and {len(keys) - limit} more (raise --limit, or use --json)")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bucket", default=BUCKET, help=f"bucket to list (default: {BUCKET})")
    ap.add_argument("--record", type=Path, default=RECORD_PATH, help="uploader record to compare")
    ap.add_argument("--limit", type=int, default=25, help="max keys printed per section")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    absent = missing_env()
    if absent:
        # ⚠️ NOT a clean result. Naming the variables is the whole point — a
        # bare "could not list" sends the next person to the dashboard.
        print(
            f"[BLOCKED] cannot list {args.bucket}: {', '.join(absent)} not set. They are the same "
            "three scripts/upload_ebooks_r2.py's multipart path uses; put them in .env or export "
            "them, then re-run. Nothing was measured.",
            file=sys.stderr,
        )
        return EXIT_UNREACHABLE

    try:
        record = load_record(args.record)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[BLOCKED] {args.record}: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        bucket = list_bucket(args.bucket)
    except ImportError:
        print("[BLOCKED] boto3 is not installed (`pip install boto3`). Nothing was measured.", file=sys.stderr)
        return EXIT_UNREACHABLE
    except Exception as exc:  # noqa: BLE001 - the reason is the useful part
        # ⚠️ The exception TEXT, never a stack trace and never a shrug: a 403
        # here means the token is not scoped to this bucket, a 404 means the
        # bucket name is wrong, and those have completely different fixes.
        print(f"[BLOCKED] listing {args.bucket} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    diff = diff_keys(record, bucket)
    rec_bytes = total_bytes(record)
    buc_bytes = total_bytes(bucket)

    if args.json:
        print(
            json.dumps(
                {
                    "bucket": args.bucket,
                    "record_path": str(args.record),
                    "record_count": len(record),
                    "record_bytes": rec_bytes,
                    "bucket_count": len(bucket),
                    "bucket_bytes": buc_bytes,
                    **diff,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"Bucket   : {args.bucket}  (listed live, {len(bucket)} object(s), {human_bytes(buc_bytes)})")
        print(f"Record   : {args.record.name}  ({len(record)} entry(ies), {human_bytes(rec_bytes)})")
        print(f"Agreed   : {diff['agreed']} key(s) present on both sides")
        _print_section(
            "🔴 IN THE RECORD, NOT IN THE BUCKET — each of these 404s in the reader",
            diff["missing"],
            args.limit,
        )
        _print_section(
            "ORPHANS — in the bucket, not in the record (harmless; a re-filed book leaves one behind)",
            diff["orphans"],
            args.limit,
            detail=lambda k: f"  [{human_bytes(bucket[k].get('size'))}]",
        )
        if diff["size_mismatch"]:
            print(f"\n⚠️ SIZE DISAGREEMENT ({len(diff['size_mismatch'])}):")
            for row in diff["size_mismatch"][: args.limit]:
                print(
                    f"  {row['key']}  record {row['record_bytes']:,} B vs bucket "
                    f"{row['bucket_bytes']:,} B"
                )
        if not diff["missing"] and not diff["orphans"] and not diff["size_mismatch"]:
            print("\n[OK] the bucket and the record agree on every key and every byte count.")

    return EXIT_DRIFT if diff["missing"] else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
