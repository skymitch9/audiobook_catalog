#!/usr/bin/env python3
"""Publish the estate's `docs/` corpus to the PRIVATE `estate-docs-gated` R2 bucket.

Design of record: catalog-platform `docs/info/gabi-docs-assistant-design.md`
(phase 1). Owner brief, verbatim (2026-08-17): *"let's make sure GABI can read
all of our docs and stuff so she can even help me if needed for let's say I
don't have a Claude code session open."*

WHY THIS SCRIPT LIVES IN audiobook_catalog, NOT catalog-platform
----------------------------------------------------------------
The design's §2.2 *recommended* `catalog-platform/scripts/publish-docs-
snapshot.mjs`. It ships here instead, deliberately, for two reasons that only
became load-bearing once the phases were scheduled together:

  1. ⚠️ **`audiobook_catalog/docs/` exists on this machine and nowhere else.**
     `.gitignore:7` ignores `docs/` wholesale (only `docs/deploys.log` is
     negated back), and `docs/access/CREDENTIALS.md` lives in that tree. The
     publish step therefore MUST run here — a CI-published snapshot would
     silently carry two-thirds of the estate while looking complete, which is
     the worst possible failure for a docs assistant.
  2. **STEP 9 (phase 5) is a step of THIS repo's pipeline** (`scripts/
     sync_to_drive.py`), and `publish_ebooks_manifest.py` is the exact
     precedent: a Python module the pipeline imports and calls, whose failure
     is one WARN. A Node script in a sibling repo invoked by absolute path
     would add a cross-repo path dependency to the estate's only unattended
     job for no gain.

The CONSUMER still lives in catalog-platform (`apps/auth-worker/src/
estate-docs.ts`, binding `ESTATE_DOCS` -> bucket `estate-docs-gated`). Both
halves are one contract: change the bucket or the object names here and that
Worker answers `snapshot_absent`, which looks exactly like a stalled pipeline.

WHAT IS PUBLISHED  (design §2.1/§3.1 — five layers, each covering a different failure)
-------------------------------------------------------------------------------------
  1. **Directory allowlist**, default DENY — `REPOS` below, three explicit
     (repo, docs-dir) pairs. A fourth repo joins by editing that array, never
     by walking a parent. Anything not on it is absent because it was never
     reachable, not because a filter caught it.
  2. **Extension allowlist**, default DENY — `.md` ONLY. This is what excludes,
     BY CONSTRUCTION, the non-markdown files measured in the three trees:
     two `deploys.log`, `DRIVE_AUDIT_REPORT.csv`, `drive-exceptions.json`,
     `permission-snapshot-*.json`, and the two `SHELF_*.fragment.html`. Two of
     those carry real people's permission data. That exclusion is this rule's
     FIRST job, not a side effect.
  3. **Per-file denylist** — `DENYLIST` below. ⚠️ `audiobook_catalog/docs/
     access/CREDENTIALS.md` is its first and permanent entry. Its whole role is
     to be the one place credential locations are written down; nothing that
     leaves this machine carries it, and no gate is worth betting that file on.
  4. **Fail-closed content scanner** — `scan_text()`. It **REFUSES the publish**
     on a hit. It does NOT strip, redact, or skip the offending file: a
     silently-stripped doc is a doc GABI answers from, missing the line that
     mattered, and "a validator that silently strips instead of rejecting" is a
     named estate defect. On a hit the publisher prints file, line and rule,
     exits non-zero, and **the previous snapshot keeps serving**.
  5. **Receipt** — `receipt.json`, every included path with bytes + sha256. It
     is what makes a *directory* allowlist auditable: one read names the
     complete included set, so an unintended file is visible rather than
     silently present.

⚠️ SCANNER ROLLOUT: SHADOW FIRST, and that is the default.
----------------------------------------------------------
`--scanner shadow` (the default) LOGS would-refuse findings and publishes
anyway. `--scanner enforce` refuses. The estate's standing enforcement-rollout
rule is off -> shadow (log would-deny, act on nothing) -> enforce, flipped only
on MEASURED zero false refusals — and this rule set has never been run over the
corpus, so its false-positive rate is unknown by construction. Flip with
`--scanner enforce` (or `DOCS_SCANNER_MODE=enforce`) after a week of clean
shadow output; that flip is design phase 5's other half.

Emergency hatch, deliberately awkward, honoured only in enforce mode:
`ALLOW_SUSPECT_DOCS=1`.

⚠️ THE SCANNER NEVER PRINTS OR STORES THE MATCHED TEXT — only path, line number
and rule name. A findings log that quotes the secret it found has published the
secret to a second place.

BUNDLE SHAPE  (one deliberate departure from the design's sketch)
-----------------------------------------------------------------
The design sketched `{ ..., sections: [{heading, level, start, end}], text:
"<the whole file>" }` — byte offsets into a stored whole-file string. Built
instead as **sections carrying their own text, with no whole-file copy and no
offsets** (the design invited this: *"reasoned; pin it in the publisher's
header when built"*). Two reasons:

  * Storing both the file text AND offsets means the consumer slices a string;
    storing both the text and the sections' text would double the corpus.
    Sections-only reconstructs the file by concatenation and is strictly
    smaller.
  * ⚠️ Offsets are a cross-language hazard here. Python indexes str by code
    point; a Worker indexes by UTF-16 code unit. The corpus is full of ⚠️
    (BMP, safe) but one astral emoji anywhere would silently shift every
    offset after it in the file, and the symptom would be a section that
    starts mid-word — a bug nobody would trace back to this line.

    { "schema": 1, "generated_at": ISO8601, "corpus": {...}, "git": {...},
      "files": [ { "repo", "path", "title", "bytes",
                   "sections": [ {"i", "heading", "level", "bytes", "text"} ] } ] }

Sections cut at **H2**, split further at **H3** when a section exceeds 8 KB,
and hard-split at line boundaries if still over — so the 8 KB ceiling the read
route promises is guaranteed by the PUBLISHER rather than by truncation at
serve time. A leading pre-first-heading preamble counts as a section. This is
what makes a 323 KB `DONE.md` answerable at all.

TRANSPORT  (measured 2026-08-18, both paths, against the real bucket)
---------------------------------------------------------------------
⚠️ **wrangler is the default and the S3 path is opt-in, because the estate R2
API token in `.env` DOES NOT REACH THIS BUCKET.** Measured:

    PUT estate-ebooks      -> OK
    PUT estate-docs-gated  -> AccessDenied

That token is scoped to named buckets (`estate-ebooks`, `estate-audio`), and a
new bucket is not one of them. `wrangler r2 object put` uses wrangler's own
OAuth login and needs no new credential — the design's recommended transport,
now also the measured-working one:

    wrangler r2 object put estate-docs-gated/snapshot.json.gz -> Upload complete.

`--transport s3` uses `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` /
`R2_SECRET_ACCESS_KEY` from `.env` via boto3, and becomes available the moment
an owner widens that token to include `estate-docs-gated` (dash -> R2 -> API
tokens -> edit the token's bucket list). Nothing here reads or prints a
credential value under either transport.

CONTRACT
--------
* **Idempotent by content.** sha256 of the gzipped bundle, recorded in
  `scripts/.docs_published.json` (gitignored); re-PUT only when it changes.
  `--force` re-uploads regardless. Same shape as `publish_ebooks_manifest.py`.
* **Never partial.** Every gate runs BEFORE the upload; a refused run leaves
  the previous snapshot serving, so GABI answers from a slightly stale corpus
  rather than none. The snapshot's date rides in every answer (design §6), so
  stale is VISIBLE rather than silently believed.
* **Growth tripwire** (design §5.4): WARN above 10 MB raw, REFUSE above 25 MB.
  `DONE.md` files only grow; the point is that a threshold exists and is
  mechanical, not that these numbers are measured.
* **A missing repo is a REFUSAL, not a skip.** Publishing two of three trees
  while reporting success is the exact failure §2.2 exists to prevent.

USAGE
-----
    python -m scripts.publish_docs_snapshot                 # publish if changed
    python -m scripts.publish_docs_snapshot --dry-run       # say what would happen
    python -m scripts.publish_docs_snapshot --force         # re-upload regardless
    python -m scripts.publish_docs_snapshot --scanner enforce
    python -m scripts.publish_docs_snapshot --verbose       # name every file

Exit 0 = the bucket holds the current snapshot. Exit 1 = it does not (refused
by a gate, or the upload failed) — the previous objects still stand.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent          # audiobook_catalog
_BOOKBUDDY = PROJECT_ROOT.parent                                # .../bookbuddy
_REPOS_ROOT = _BOOKBUDDY.parent                                 # .../vs-code-repos

# ---------------------------------------------------------------------------
# LAYER 1 — the DIRECTORY ALLOWLIST. Default deny. An explicit array of three
# (repo key, docs directory) pairs; a fourth repo joins by ADDING A LINE here.
# Env overrides exist only so a checkout in a different layout (or a test) can
# point at another tree — never so the set can grow implicitly.
# ---------------------------------------------------------------------------
REPOS: List[Tuple[str, Path]] = [
    ("catalog-platform",
     Path(os.getenv("DOCS_SNAPSHOT_PLATFORM_ROOT", str(_REPOS_ROOT / "catalog-platform"))) / "docs"),
    ("library_catalog",
     Path(os.getenv("DOCS_SNAPSHOT_LIBRARY_ROOT", str(_BOOKBUDDY / "library_catalog"))) / "docs"),
    ("audiobook_catalog",
     Path(os.getenv("DOCS_SNAPSHOT_AUDIOBOOK_ROOT", str(PROJECT_ROOT))) / "docs"),
]

# LAYER 2 — the EXTENSION ALLOWLIST. Default deny. `.md` and nothing else.
ALLOWED_SUFFIXES = {".md"}

# Directory names never descended into, at any depth. Not a security layer —
# layers 1 and 2 already exclude everything outside `docs/**/*.md` — but a
# docs tree that ever grows a build output or a vendored dependency should not
# quietly double the corpus.
SKIP_DIRS = {".git", "node_modules", "_build", "__pycache__", ".venv", "venv", "site-packages"}

# ---------------------------------------------------------------------------
# LAYER 3 — the PER-FILE DENYLIST, as "<repo>/<path relative to that repo's
# docs dir>", forward slashes, case-insensitive.
#
# ⚠️ CREDENTIALS.md IS FIRST AND PERMANENT. Its entire job is to be the one
# place credential LOCATIONS are written down. Nothing that leaves this machine
# carries it — not through a gate, not through a private bucket, not for GABI.
# Removing this line is not a code change; it is a decision to publish the
# estate's credential index, and there is no reason good enough.
# ---------------------------------------------------------------------------
DENYLIST: List[str] = [
    "audiobook_catalog/access/CREDENTIALS.md",
]
_DENY_SET = {d.lower() for d in DENYLIST}

# The bucket and object names apps/auth-worker reads (its wrangler.toml binds
# ESTATE_DOCS -> estate-docs-gated; src/estate-docs.ts's SNAPSHOT_KEY /
# RECEIPT_KEY are these two strings).
# ⚠️ Both halves are one contract — changing either without the other gives
# every caller a 503 `snapshot_absent` that looks exactly like a stalled
# pipeline. ⚠️ The bucket has NO public r2.dev URL and NO custom domain
# (verified 2026-08-18: "Public access via the r2.dev URL is disabled") and
# must never get one: the corpus carries operations runbooks, break-glass SQL,
# secret NAMES and household members' email addresses.
BUCKET = os.getenv("ESTATE_DOCS_BUCKET", "estate-docs-gated")
SNAPSHOT_KEY = "snapshot.json.gz"
RECEIPT_KEY = "receipt.json"

STATE_PATH = PROJECT_ROOT / "scripts" / ".docs_published.json"

# Section ceiling (design §2.3/§5.3). The publisher GUARANTEES it by splitting,
# so the read route never has to truncate a section it promised whole.
SECTION_MAX_BYTES = 8 * 1024

# Growth tripwire (design §5.4). Reasoned thresholds, not measured — the point
# is that one exists and is mechanical. Above WARN: publish, say so loudly.
# Above REFUSE: stop, with a message pointing back at the design section, at
# which point the answer is to drop the DONE.md archives from the allowlist or
# to move to a real index.
CORPUS_WARN_BYTES = 10 * 1024 * 1024
CORPUS_REFUSE_BYTES = 25 * 1024 * 1024

ALLOW_SUSPECT_ENV = "ALLOW_SUSPECT_DOCS"


# ===========================================================================
# LAYER 4 — the content scanner
#
# ⚠️ THIS IS THE SECURITY SPINE, together with the denylist. Read §3.2 of the
# design before touching it: the estate accepts a DIRECTORY allowlist (rather
# than a per-file one, which fails OPEN on omission and would leave GABI
# answering confidently from six-week-old text) precisely BECAUSE this scanner
# is here. It is not optional and it must not learn to strip.
#
# Every rule returns (line_number, rule_name) and NEVER the matched text.
# ===========================================================================

_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# Provider key prefixes for the services this estate actually uses. Prefix
# rules are the low-false-positive half of the scanner: a real `sk-ant-` or
# `ghp_` in a doc is a real leak, near enough always.
_PREFIX_RULES: List[Tuple[str, re.Pattern[str]]] = [
    ("anthropic_key",    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key",       re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("github_pat",       re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("github_fine_pat",  re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("google_api_key",   re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("aws_access_key",   re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token",      re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("gitlab_pat",       re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("digitalocean_pat", re.compile(r"\bdop_v1_[a-f0-9]{64}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("stripe_key",       re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("discord_bot_token", re.compile(r"\b[MNO][A-Za-z0-9_\-]{23,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt_like",         re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
]

# `password|passwd|secret|token|api_key` immediately followed by = or : and a
# value. The placeholder filter below is what keeps this from firing on every
# line of every access doc, which is exactly what those docs are FOR (secret
# NAMES and where they live, never values).
_ASSIGN_RE = re.compile(
    r"(?i)\b(pass(?:word|wd)|secret|token|api[_\-]?key|access[_\-]?key|client[_\-]?secret)"
    r"\s*[:=]\s*(?P<q>[\"']?)(?P<val>[^\s\"'`,;]{8,})(?P=q)"
)

# A value that looks like documentation rather than a credential. Deliberately
# generous: in SHADOW this only affects noise, and in ENFORCE a false refusal
# blocks the whole corpus — the asymmetry favours forgiveness here, because
# layers 1-3 and the prefix rules above are the ones actually holding the line.
_PLACEHOLDER_RE = re.compile(
    r"^(?:<|\$|%|\{|\.\.\.|…|x{4,}|\*{3,}|-{3,})"
    r"|(?:your[_\- ]|example|placeholder|redact|elided|not[_\- ]?set|unset|changeme|"
    r"dummy|sample|fake|todo|tbd|n/?a|none|null|true|false|the[_\- ]|same[_\- ]|"
    r"never|see[_\- ]|pipe|wrangler|env|gitignored|secret|token|value)",
    re.IGNORECASE,
)

_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_RUN_RE = re.compile(r"\b[a-fA-F0-9]{40,}\b")

# ⚠️ `/` IS IN THE BASE64 ALPHABET AND ALSO IN EVERY URL PATH, which is the
# entire cause of the first false positive the shadow run produced (measured
# 2026-08-18, 5 findings, all false): a plain MDN link,
# `…/docs/Web/API/HTMLMediaElement/playbackRate`, is a 40+ character run of
# base64-legal characters with respectable entropy. URLs are stripped from the
# line before the ENTROPY rules run, and only those — the prefix rules still
# see the whole line, because a key pasted into a query string is still a key.
_URL_RE = re.compile(r"\b(?:https?|ftp|s3)://\S+|\b(?:www\.)\S+\.[a-z]{2,}/\S*")

# An all-lowercase, digit-free value is an identifier, not a credential —
# `secret:list:friend` (an npm script name) is what produced three of the five
# false findings. Real tokens from every provider in _PREFIX_RULES carry mixed
# case or digits or both. Narrow by construction: a lowercase-only string of
# any length cannot be a token from any issuer this estate uses.
_IDENTIFIERISH_RE = re.compile(r"^[a-z][a-z:._\-/]*$")


def _shannon_entropy(s: str) -> float:
    """Bits per character. A real base64 secret sits near 5.0-6.0; English
    prose and repeated-character runs sit far below."""
    if not s:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def scan_text(text: str, path_label: str) -> List[Dict[str, object]]:
    """Return a list of findings: {"path", "line", "rule"}.

    ⚠️ NEVER returns the matched text. A findings list that quotes what it
    found has published the secret to a second place — the log — which is
    strictly worse than the snapshot it was trying to protect.

    Fenced code blocks are exempt from the ENTROPY rules only (per the
    design's own wording: "long high-entropy base64/hex runs *outside code
    fences*"). Prefix rules and the assignment rule apply everywhere, because
    a real `sk-ant-` key does not become safe by being inside ```.
    """
    findings: List[Dict[str, object]] = []
    in_fence = False

    def hit(lineno: int, rule: str) -> None:
        findings.append({"path": path_label, "line": lineno, "rule": rule})

    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue

        for rule_name, pattern in _PREFIX_RULES:
            if pattern.search(line):
                hit(lineno, rule_name)

        m = _ASSIGN_RE.search(line)
        if m:
            val = m.group("val")
            if not _PLACEHOLDER_RE.search(val) and not _IDENTIFIERISH_RE.match(val):
                hit(lineno, "assigned_secret_value")

        if in_fence:
            continue

        bare = _URL_RE.sub(" ", line)
        for run in _B64_RUN_RE.findall(bare):
            # A git sha, a sha256 and a base64 blob all look alike to a length
            # check. Entropy is what separates "the digest of a file we are
            # documenting" from "a key someone pasted".
            if _shannon_entropy(run) >= 4.5 and not _HEX_RUN_RE.fullmatch(run):
                hit(lineno, "high_entropy_base64")
                break

        for run in _HEX_RUN_RE.findall(bare):
            # ⚠️ 40-hex is a git sha and 64-hex is a sha256, and this corpus is
            # made of documents ABOUT commits and file digests. Those two exact
            # lengths are exempt unless the line also carries a secret-ish
            # keyword — a deliberate, reasoned carve-out, not an oversight. Any
            # OTHER hex length of 40+ is not a shape this estate's docs
            # legitimately produce.
            if len(run) in (40, 64) and not _ASSIGN_RE.search(line):
                continue
            hit(lineno, "long_hex_run")
            break

    return findings


# ===========================================================================
# Walking the allowlist
# ===========================================================================

def deny_key(repo: str, rel: Path) -> str:
    return f"{repo}/{rel.as_posix()}".lower()


def collect_files() -> Tuple[List[Tuple[str, Path, Path]], List[str], List[str]]:
    """Walk every allowlisted (repo, docs dir) pair.

    Returns (included, denied, non_md) where `included` is a list of
    (repo, absolute path, path relative to that repo's docs dir).

    ⚠️ A MISSING REPO IS AN EXCEPTION, NOT A SKIP. Publishing two of the three
    trees while reporting success is precisely the failure design §2.2 names
    as the worst possible one for a docs assistant, and it would be invisible:
    the bundle would look complete and GABI would answer "I don't have
    anything on that" about a third of the estate.
    """
    included: List[Tuple[str, Path, Path]] = []
    denied: List[str] = []
    non_md: List[str] = []

    for repo, docs_dir in REPOS:
        if not docs_dir.is_dir():
            raise SystemExit(
                f"REFUSED: the allowlisted docs tree for '{repo}' is not on this machine\n"
                f"  expected: {docs_dir}\n"
                "  A partial snapshot is worse than none — GABI would answer 'I don't have\n"
                "  anything on that' about a third of the estate while looking healthy.\n"
                "  Point DOCS_SNAPSHOT_PLATFORM_ROOT / _LIBRARY_ROOT / _AUDIOBOOK_ROOT at the\n"
                "  right checkouts, or fix the clone, and re-run."
            )

        for abs_path in sorted(docs_dir.rglob("*")):
            if not abs_path.is_file():
                continue
            if any(part in SKIP_DIRS for part in abs_path.relative_to(docs_dir).parts[:-1]):
                continue
            rel = abs_path.relative_to(docs_dir)
            label = f"{repo}/{rel.as_posix()}"

            if abs_path.suffix.lower() not in ALLOWED_SUFFIXES:
                non_md.append(label)          # layer 2 — by construction
                continue
            if deny_key(repo, rel) in _DENY_SET:
                denied.append(label)          # layer 3 — named, permanent
                continue
            included.append((repo, abs_path, rel))

    return included, denied, non_md


# ===========================================================================
# Sectioning
# ===========================================================================

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _split_on_level(lines: Sequence[str], level: int) -> List[Tuple[Optional[str], List[str]]]:
    """Cut `lines` at every heading of exactly `level`. A leading run before the
    first such heading comes back with heading None (the preamble)."""
    out: List[Tuple[Optional[str], List[str]]] = []
    current_heading: Optional[str] = None
    buf: List[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            m = _HEADING_RE.match(line)
            if m and len(m.group(1)) == level:
                if buf or current_heading is not None:
                    out.append((current_heading, buf))
                current_heading = m.group(2).strip()
                buf = [line]
                continue
        buf.append(line)
    if buf or current_heading is not None:
        out.append((current_heading, buf))
    return out


def _hard_split(heading: str, level: int, lines: Sequence[str]) -> List[Tuple[str, int, str]]:
    """Last resort: chop at line boundaries so the 8 KB ceiling always holds.
    A part beyond the first is labelled '<heading> (cont. N)' so a reader can
    see it is one section continued rather than a different one."""
    parts: List[Tuple[str, int, str]] = []
    buf: List[str] = []
    size = 0
    for line in lines:
        line_bytes = len(line.encode("utf-8")) + 1
        if buf and size + line_bytes > SECTION_MAX_BYTES:
            parts.append((heading, level, "\n".join(buf)))
            buf, size = [], 0
        buf.append(line)
        size += line_bytes
    if buf:
        parts.append((heading, level, "\n".join(buf)))
    if len(parts) == 1:
        return parts
    return [(h if i == 0 else f"{h} (cont. {i + 1})", lv, t) for i, (h, lv, t) in enumerate(parts)]


def split_sections(text: str, title: str) -> List[Dict[str, object]]:
    """Cut at H2; split an oversized H2 at H3; hard-split whatever is still
    over the ceiling. The preamble before the first H2 (which is where the H1
    and a doc's header block live) is its own section — that block is often the
    single most useful thing in an estate doc.
    """
    lines = text.splitlines()
    raw: List[Tuple[str, int, str]] = []

    for heading, block in _split_on_level(lines, 2):
        block_text = "\n".join(block)
        label = heading if heading is not None else (title or "(preamble)")
        level = 2 if heading is not None else 1
        if len(block_text.encode("utf-8")) <= SECTION_MAX_BYTES:
            raw.append((label, level, block_text))
            continue

        for sub_heading, sub_block in _split_on_level(block, 3):
            sub_text = "\n".join(sub_block)
            sub_label = f"{label} — {sub_heading}" if sub_heading is not None else label
            sub_level = 3 if sub_heading is not None else level
            if len(sub_text.encode("utf-8")) <= SECTION_MAX_BYTES:
                raw.append((sub_label, sub_level, sub_text))
            else:
                raw.extend(_hard_split(sub_label, sub_level, sub_block))

    if not raw:
        raw = [(title or "(empty)", 1, text)]

    return [
        {"i": i, "heading": h, "level": lv, "bytes": len(t.encode("utf-8")), "text": t}
        for i, (h, lv, t) in enumerate(raw)
    ]


def first_h1(text: str) -> str:
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    return ""


# ===========================================================================
# git provenance
# ===========================================================================

def repo_head(docs_dir: Path) -> str:
    """Short HEAD of the repo owning `docs_dir`, or 'unknown'. The receipt
    records it so a reader can tie a snapshot to a commit — for
    audiobook_catalog that is the repo's HEAD, NOT the docs' state, because
    that tree is gitignored; the receipt's own note says so."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(docs_dir.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip() or "unknown"
    except Exception:
        pass
    return "unknown"


# ===========================================================================
# Upload transports
# ===========================================================================

def _wrangler_cmd() -> List[str]:
    local = PROJECT_ROOT / "node_modules" / ".bin" / ("wrangler.cmd" if os.name == "nt" else "wrangler")
    if local.exists():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit("npx not found on PATH. Install Node.js, or `npm i -D wrangler` in this repo.")
    return [npx, "--yes", "wrangler"]


def _run(cmd: List[str], timeout: int = 300) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def upload_via_wrangler(key: str, src: Path, content_type: str) -> Tuple[bool, str]:
    """The DEFAULT transport. Auth is wrangler's own OAuth login — nothing here
    reads, stores or prints a credential. Measured working against this bucket
    2026-08-18."""
    rc, out = _run(_wrangler_cmd() + [
        "r2", "object", "put", f"{BUCKET}/{key}",
        "--file", str(src), "--content-type", content_type, "--remote",
    ])
    return rc == 0, out.strip()


def upload_via_s3(key: str, src: Path, content_type: str) -> Tuple[bool, str]:
    """Opt-in (`--transport s3`). Uses the estate R2 API token from `.env`.

    ⚠️ MEASURED 2026-08-18: that token does NOT reach `estate-docs-gated` —
    `PUT estate-ebooks` succeeded and `PUT estate-docs-gated` came back
    `AccessDenied`. It is scoped to a named bucket list. This path therefore
    fails today BY MEASUREMENT, not by bug, and the failure message says so;
    it becomes available the moment an owner adds this bucket to the token.
    """
    missing = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not os.getenv(v)]
    if missing:
        try:
            from dotenv import load_dotenv
            load_dotenv(PROJECT_ROOT / ".env")
        except Exception:
            pass
        missing = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not os.getenv(v)]
    if missing:
        return False, f"missing env: {', '.join(missing)} (they live in .env; never printed)"

    try:
        import boto3
    except ImportError:
        return False, "boto3 is not installed (`pip install boto3`)"

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    try:
        client.upload_file(str(src), BUCKET, key, ExtraArgs={"ContentType": content_type})
        return True, "uploaded (s3)"
    except Exception as e:  # noqa: BLE001 — the message is the deliverable
        return False, (
            f"{type(e).__name__}: {e}\n"
            f"      ⚠️ If this is AccessDenied, the estate R2 API token does not cover "
            f"'{BUCKET}'.\n"
            "      OWNER STEP: dash.cloudflare.com -> R2 -> API tokens -> edit the estate token "
            "and add this bucket.\n"
            "      Until then use the default transport (wrangler), which needs no new credential."
        )


# ===========================================================================
# State (idempotence by content)
# ===========================================================================

def load_state() -> Dict[str, object]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state: Dict[str, object]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ===========================================================================
# Building
# ===========================================================================

def build_snapshot(scanner_mode: str) -> Dict[str, object]:
    """Walk, scan, section, and assemble. Pure enough to test: it touches the
    filesystem and nothing else — no network, no state file, no upload."""
    included, denied, non_md = collect_files()

    files: List[Dict[str, object]] = []
    receipt_files: List[Dict[str, object]] = []
    findings: List[Dict[str, object]] = []
    total_bytes = 0
    total_sections = 0

    for repo, abs_path, rel in included:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
        raw = text.encode("utf-8")
        label = f"{repo}/docs/{rel.as_posix()}"

        findings.extend(scan_text(text, label))

        title = first_h1(text) or rel.stem
        sections = split_sections(text, title)
        total_bytes += len(raw)
        total_sections += len(sections)

        files.append({
            "repo": repo,
            "path": label,
            "title": title,
            "bytes": len(raw),
            "sections": sections,
        })
        receipt_files.append({
            "path": label,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "sections": len(sections),
        })

    git = {repo: repo_head(docs_dir) for repo, docs_dir in REPOS}
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    bundle = {
        "schema": 1,
        "generated_at": generated_at,
        "corpus": {"files": len(files), "bytes": total_bytes, "sections": total_sections},
        "git": git,
        "files": files,
    }
    receipt = {
        "schema": 1,
        "generated_at": generated_at,
        "git": git,
        "_note": (
            "audiobook_catalog's docs/ tree is gitignored, so its sha names the REPO's HEAD, "
            "not the docs' state. This receipt is served only to devops-class callers "
            "(GET /api/estate/docs/receipt) — it names local-only document paths."
        ),
        "totals": {"files": len(files), "bytes": total_bytes, "sections": total_sections},
        "repos": [{"repo": r, "docs_dir_present": True} for r, _ in REPOS],
        "denylist": DENYLIST,
        "excluded": {"denylisted": denied, "non_markdown": non_md},
        "scanner": {
            "mode": scanner_mode,
            "finding_count": len(findings),
            # ⚠️ path + line + rule ONLY. Never the matched text.
            "findings": findings,
        },
        "files": receipt_files,
    }
    return {"bundle": bundle, "receipt": receipt, "findings": findings,
            "denied": denied, "non_md": non_md, "total_bytes": total_bytes}


def content_sha(bundle: Dict[str, object]) -> str:
    """The digest the sha-skip compares — over the CORPUS, not the artefact.

    ⚠️ MEASURED BUG, FOUND ON THE FIRST REAL RUN (2026-08-18): hashing the
    gzipped bundle re-uploaded on every single invocation, because
    `generated_at` lives inside the bundle and therefore changes every time.
    "Idempotent by content" was true of the code and false of the behaviour —
    an 8-hourly pipeline step would have PUT 1.2 MB forever and the receipt
    diff would have printed "no change" beside it, which is precisely the shape
    of a bug nobody investigates.

    So the skip key covers exactly what a reader would call the content: each
    repo's HEAD plus every file's path and text. Not the timestamp, not the
    gzip framing, not the derived counts.
    """
    material = json.dumps(
        {"git": bundle["git"], "files": bundle["files"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def gzip_bytes(obj: Dict[str, object]) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # mtime=0 so an unchanged corpus produces an identical gzip — the sha-skip
    # depends on it. Without this, every run would look "changed".
    return gzip.compress(payload, compresslevel=9, mtime=0)


def receipt_diff(previous: Optional[Dict[str, object]], receipt: Dict[str, object]) -> Tuple[List[str], List[str]]:
    """+files / -files against the last receipt. This is the drift signal the
    directory allowlist is traded against: an unintended file shows up here
    rather than sitting silently in the corpus."""
    now = {f["path"] for f in receipt["files"]}          # type: ignore[index]
    before = set((previous or {}).get("paths") or [])
    return sorted(now - before), sorted(before - now)


# ===========================================================================
# main
# ===========================================================================

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="publish_docs_snapshot",
        description="Publish the estate docs corpus to the private estate-docs-gated R2 bucket.",
    )
    ap.add_argument("--dry-run", action="store_true", help="say what would happen; upload nothing")
    ap.add_argument("--force", action="store_true", help="re-upload even when unchanged")
    ap.add_argument("--verbose", action="store_true", help="name every included file")
    ap.add_argument("--scanner", choices=("shadow", "enforce"),
                    default=os.getenv("DOCS_SCANNER_MODE", "shadow"),
                    help="shadow (default): log would-refuse and publish anyway. enforce: refuse.")
    ap.add_argument("--transport", choices=("wrangler", "s3"), default=os.getenv("DOCS_TRANSPORT", "wrangler"),
                    help="wrangler (default, needs no new credential) or s3 (.env R2 token)")
    ap.add_argument("--out", default=None, help="also write snapshot.json.gz + receipt.json here")
    args = ap.parse_args(argv)

    print("=" * 60)
    print("Estate docs snapshot -> " + BUCKET)
    print("=" * 60)

    built = build_snapshot(args.scanner)
    bundle = built["bundle"]                             # type: ignore[assignment]
    receipt = built["receipt"]                           # type: ignore[assignment]
    findings: List[Dict[str, object]] = built["findings"]     # type: ignore[assignment]
    total_bytes: int = built["total_bytes"]              # type: ignore[assignment]

    corpus = bundle["corpus"]                            # type: ignore[index]
    print(f"  Files      : {corpus['files']} markdown documents")
    print(f"  Raw bytes  : {total_bytes:,}")
    print(f"  Sections   : {corpus['sections']}")
    print(f"  Excluded   : {len(built['denied'])} denylisted, "                # type: ignore[arg-type]
          f"{len(built['non_md'])} non-markdown")
    for label in built["denied"]:                        # type: ignore[union-attr]
        print(f"    [DENY] {label}")

    # --- Growth tripwire (design §5.4) ------------------------------------
    if total_bytes > CORPUS_REFUSE_BYTES:
        print(f"\n  [REFUSED] The corpus is {total_bytes:,} bytes, over the "
              f"{CORPUS_REFUSE_BYTES:,}-byte ceiling.")
        print("  gabi-docs-assistant-design.md §5.4: at this size the answer is either to drop")
        print("  the DONE.md archives from the allowlist or to move to a real index. The")
        print("  previous snapshot keeps serving until then.")
        return 1
    if total_bytes > CORPUS_WARN_BYTES:
        print(f"\n  [WARN] The corpus is {total_bytes:,} bytes, past the "
              f"{CORPUS_WARN_BYTES:,}-byte warning line (design §5.4). Still publishing.")

    # --- Layer 4, the scanner ---------------------------------------------
    if findings:
        print(f"\n  [SCANNER:{args.scanner}] {len(findings)} would-refuse finding(s):")
        for f in findings[:50]:
            print(f"    {f['path']}:{f['line']}  rule={f['rule']}")
        if len(findings) > 50:
            print(f"    … and {len(findings) - 50} more (full list in receipt.json)")
        print("    ⚠️ Path, line and rule only — the matched text is never printed or stored.")
    else:
        print(f"\n  [SCANNER:{args.scanner}] no findings.")

    if findings and args.scanner == "enforce":
        if os.getenv(ALLOW_SUSPECT_ENV) == "1":
            print(f"  [{ALLOW_SUSPECT_ENV}=1] Publishing anyway. Fix the files and remove this.")
        else:
            print("\n  [REFUSED] The scanner is enforcing and found a suspected credential.")
            print("  Nothing was uploaded; the previous snapshot keeps serving. Fix the file")
            print(f"  (never strip it silently), or set {ALLOW_SUSPECT_ENV}=1 for one run.")
            return 1

    # --- Bundle -----------------------------------------------------------
    blob = gzip_bytes(bundle)
    digest = content_sha(bundle)
    ratio = (len(blob) / total_bytes * 100) if total_bytes else 0
    print(f"\n  Gzipped    : {len(blob):,} bytes ({ratio:.1f}% of raw)")
    print(f"  Content sha: {digest[:16]}…  (corpus only — never the timestamp; see content_sha())")

    state = load_state()
    added, removed = receipt_diff(state.get("receipt"), receipt)  # type: ignore[arg-type]
    if added or removed:
        print(f"  Receipt    : +{len(added)} file(s), -{len(removed)} file(s) since the last publish")
        for p in added[:20]:
            print(f"    + {p}")
        for p in removed[:20]:
            print(f"    - {p}")
    else:
        print("  Receipt    : no change in the included set")

    if args.verbose:
        print("\n  Included set:")
        for f in receipt["files"]:                       # type: ignore[index]
            print(f"    {f['bytes']:>8,}  {f['sections']:>3} §  {f['path']}")

    # --- Local artefacts (tests, --out) -----------------------------------
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / SNAPSHOT_KEY).write_bytes(blob)
        (out_dir / RECEIPT_KEY).write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\n  Wrote {out_dir / SNAPSHOT_KEY} and {out_dir / RECEIPT_KEY}")

    if args.dry_run:
        print("\n  [DRY RUN] Nothing uploaded.")
        return 0

    if not args.force and state.get("sha256") == digest:
        print("\n  Unchanged since the last publish — skipping the upload (--force overrides).")
        return 0

    # --- Upload. Snapshot FIRST, receipt second: a receipt describing a
    # snapshot that is not there yet is the one ordering that lies. ---------
    tmp_dir = PROJECT_ROOT / "scripts" / ".docs_snapshot_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    snap_tmp = tmp_dir / SNAPSHOT_KEY
    rec_tmp = tmp_dir / RECEIPT_KEY
    snap_tmp.write_bytes(blob)
    rec_tmp.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    upload = upload_via_s3 if args.transport == "s3" else upload_via_wrangler
    print(f"\n  Uploading via {args.transport}…")

    ok, out = upload(SNAPSHOT_KEY, snap_tmp, "application/gzip")
    if not ok:
        print(f"  [FAILED] {SNAPSHOT_KEY}: {out}")
        print("  The previous snapshot keeps serving.")
        return 1
    print(f"  [OK] {BUCKET}/{SNAPSHOT_KEY}")

    ok, out = upload(RECEIPT_KEY, rec_tmp, "application/json")
    if not ok:
        # The snapshot IS live; only its audit trail is stale. Say exactly that
        # rather than implying the corpus did not publish.
        print(f"  [WARN] {RECEIPT_KEY}: {out}")
        print("  The snapshot published; only the receipt is stale. Re-run with --force.")
    else:
        print(f"  [OK] {BUCKET}/{RECEIPT_KEY}")

    write_state({
        "sha256": digest,
        "generated_at": bundle["generated_at"],          # type: ignore[index]
        "gzip_bytes": len(blob),
        "raw_bytes": total_bytes,
        "files": corpus["files"],
        "sections": corpus["sections"],
        "scanner": {"mode": args.scanner, "finding_count": len(findings)},
        "receipt": {"paths": [f["path"] for f in receipt["files"]]},   # type: ignore[index]
    })

    print(f"\n  Published {corpus['files']} documents, {corpus['sections']} sections, "
          f"{len(blob):,} gzipped bytes.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
