# app/core/ingest_pack.py
# Build one chunk pack per book, gzip it, and PUT it into the gated bucket.
#
# ⚠️ THE TRANSPORT IS WRANGLER, AND THAT IS A MEASUREMENT, NOT A PREFERENCE.
# Measured 2026-08-18 with the estate R2 API token from `.env`:
#
#     estate-audio       PUT ok
#     estate-ebooks      PUT ok
#     ebooks-gated       AccessDenied      <-- the bucket packs live in
#     audiobook-covers   AccessDenied
#
# So the token cannot write packs. Both existing publishers into this bucket
# (`scripts/publish_ebooks_manifest.py`, `scripts/publish_audio_manifest.py`)
# already use wrangler's own OAuth for exactly this reason - `docs/info/
# audio-ingest.md` section 7 records that it "worked even while the token was
# still blocked". This module reads that transport rather than re-deriving one.
# A round trip was exercised end to end 2026-08-18: PUT, GET byte-exact,
# DELETE, confirmed gone.
#
# ⚠️ IF A FUTURE SESSION SEES AccessDenied HERE IT IS THE TOKEN'S SCOPE, NEVER
# THIS CODE - and the fix is not to widen the token but to keep using wrangler,
# because a token that can write the gated bucket is a token that can leak it.
#
# IDEMPOTENCE: HASH THE CONTENT, NOT THE ARTIFACT.
# The docs build shipped a sha-skip over the gzipped bundle, which carries a
# fresh `generated_at` every run and therefore changed every run - re-PUTting
# 1.2 MB forever while printing "no change" (design section 7.5). The digest here
# covers {book_id, source, ingester_version, chunk texts} and nothing that
# moves on its own.

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from app.core.book_chunker import CHUNK_CHARS, CHUNK_OVERLAP, ChapterRef, Chunk
from app.core.book_text import ExtractedBook, build_alias_map

# ⚠️ Stamped into every pack. Bump when ANY of these change: the chunk size or
# overlap, the EPUB spine-ordering rule, the transcript chapter anchoring, or
# the pack's field grammar. A pack and a reading position may only be used
# together when their versions agree (design section 4.3).
INGESTER_VERSION = 1

BUCKET = os.getenv("EBOOKS_GATED_BUCKET", "ebooks-gated")
PACK_PREFIX = "text/"
INDEX_KEY = "text/_index.json.gz"

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Design section 7.5. Nothing on the shelf approaches the WARN line (measured
# max 3.2 MB); the REFUSE line is what stops a runaway transcript silently
# becoming a 40 MB object in a gated bucket.
WARN_TEXT_BYTES = 5_000_000
REFUSE_TEXT_BYTES = 20_000_000


class PackRefused(RuntimeError):
    """A pack that must not be uploaded. Carries the worded reason."""


# --------------------------------------------------------------------------
# wrangler (the publish_ebooks_manifest.py idiom, verbatim - one way to call it)
# --------------------------------------------------------------------------

def _wrangler_cmd() -> List[str]:
    local = PROJECT_ROOT / "node_modules" / ".bin" / ("wrangler.cmd" if os.name == "nt" else "wrangler")
    if local.exists():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit("npx not found on PATH. Install Node.js, or `npm i -D wrangler` in this repo.")
    return [npx, "--yes", "wrangler"]


def r2_put(key: str, path: Path, content_type: str = "application/gzip",
           timeout: float = 300.0) -> None:
    """PUT a pack as OPAQUE GZIP BYTES.

    ⚠️ NO `--content-encoding: gzip`, AND THAT IS A MEASURED DECISION.
    The first upload set it, and the result was that a client GET transparently
    inflated the object: 246,033 bytes were stored and 802,920 came back, with
    nothing in the response saying a transform had happened. That ambiguity is
    poison for a serving layer - the Workers R2 binding hands back the STORED
    bytes while an HTTP client hands back INFLATED ones, so the same key needs
    two different readers depending on who asks, and whichever one a future
    session writes first will look correct until the other path is exercised.

    Stored opaque, every reader agrees: one R2 GET, one explicit gunzip, exactly
    as the design's section 3.1 describes.
    """
    cmd = _wrangler_cmd() + [
        "r2", "object", "put", f"{BUCKET}/{key}",
        "--file", str(path),
        "--content-type", content_type,
        "--remote",
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wrangler put {key} failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '')[-800:]}"
        )


def r2_get_bytes(key: str, dest: Path, timeout: float = 300.0) -> bool:
    cmd = _wrangler_cmd() + ["r2", "object", "get", f"{BUCKET}/{key}",
                             "--file", str(dest), "--remote"]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    return proc.returncode == 0 and dest.exists()


# --------------------------------------------------------------------------
# pack construction
# --------------------------------------------------------------------------

def content_digest(book_id: str, source: str, chunks: List[Chunk]) -> str:
    """⚠️ Over CONTENT ONLY - no timestamp, no artifact bytes. See the header."""
    h = hashlib.sha256()
    h.update(book_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(source.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(INGESTER_VERSION).encode("utf-8"))
    h.update(b"\x00")
    for c in chunks:
        h.update(c.text.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def build_pack(book: ExtractedBook, chunks: List[Chunk], refs: List[ChapterRef],
               extra: Optional[dict] = None) -> dict:
    text_bytes = book.text_bytes
    if text_bytes > REFUSE_TEXT_BYTES:
        raise PackRefused(
            f"{book.book_id}: {text_bytes:,} bytes of text exceeds the "
            f"{REFUSE_TEXT_BYTES:,}-byte refuse line (see ingest_pack.py header "
            f"and design section 7.5). Nothing was uploaded."
        )

    pack = {
        "book_id": book.book_id,
        "title": book.title,
        "source": book.source,
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ingester_version": INGESTER_VERSION,
        "chunk_chars": CHUNK_CHARS,
        "chunk_overlap": CHUNK_OVERLAP,
        "text_bytes": text_bytes,
        "text_sha256": content_digest(book.book_id, book.source, chunks),
        "chapters": [r.to_dict() for r in refs],
        "chunks": [c.to_dict() for c in chunks],
    }
    if book.notes:
        pack["notes"] = list(book.notes)
    # ⚠️ Per BOOK, never per series (design section 6.4). Transcripts only -
    # an EPUB's spelling is the publisher's and needs no alias map.
    if book.source == "transcript":
        pack["alias_candidates"] = build_alias_map(book)
    if extra:
        pack.update(extra)
    return pack


def write_pack_gz(pack: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{pack['book_id']}.json.gz"
    raw = json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # mtime=0 so the gzip container is byte-identical for identical content -
    # otherwise the artifact changes every run even when the content does not,
    # which is the defect the header's idempotence note exists to prevent.
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(path, "wb"), mtime=0) as gz:
        gz.write(raw)
    return path


def pack_stats(pack: dict, gz_path: Path) -> dict:
    raw = len(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    gz = gz_path.stat().st_size
    return {
        "book_id": pack["book_id"],
        "source": pack["source"],
        "chunks": len(pack["chunks"]),
        "chapters": len(pack["chapters"]),
        "text_bytes": pack["text_bytes"],
        "raw_bytes": raw,
        "gz_bytes": gz,
        "gzip_ratio": round(gz / raw, 4) if raw else None,
        "warn": pack["text_bytes"] > WARN_TEXT_BYTES,
    }


def upload_pack(gz_path: Path, book_id: str) -> str:
    key = f"{PACK_PREFIX}{book_id}.json.gz"
    r2_put(key, gz_path)
    return key


def build_index(entries: List[dict]) -> dict:
    """bookId -> summary. Also the record of what is deliberately NOT here."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ingester_version": INGESTER_VERSION,
        "chunk_chars": CHUNK_CHARS,
        "chunk_overlap": CHUNK_OVERLAP,
        "count": len(entries),
        "books": {e["book_id"]: e for e in entries},
    }
