"""ONE canonical atomic JSON writer for every file this pipeline persists.

F3 (2026-09-07). Lifted verbatim from `scripts/sync_to_drive.py:187`
`_atomic_write_json` (written 2026-08-24), which now imports it from here
instead of keeping its own copy.

WHY IT IS SHARED RATHER THAN COPIED
-----------------------------------
Three writers needed the same behaviour and two of them had drifted:

| Writer | Had tmp+replace | Had fsync | Temp name |
|---|---|---|---|
| `sync_to_drive._atomic_write_json` | yes | **yes** | `mkstemp` (unique) |
| `ingest_queue.save_state` | yes | **no** | `ingest_state.tmp` (**fixed**) |
| `ingest_queue_summary.write_queue_summary` | yes | **no** | `queue_summary.json.tmp` (**fixed**) |

A near-duplicate of a function that decides how persisted state reaches the
disk is exactly the shape the estate rule on near-duplicates names: the copy
that got the durability fix was not the copy protecting `ingest_state.json`.

THE TWO GAPS THIS CLOSES, AND WHAT THEY ARE NOT
-----------------------------------------------
1. **`flush()` + `fsync()` — DURABILITY, not atomicity.** `os.replace` alone
   already makes a `SIGKILL` safe: the OS still owns the buffered data and
   writes it out. A **power cut or bluescreen** is the case it does not
   cover — the rename can reach the disk while the data blocks have not, and
   NTFS then presents a zero-length or trailing-null file. `load_state()`
   does a bare `json.load` and would raise on every future run.
2. **A unique temp name.** `path.with_suffix(".tmp")` is always the same
   name in the same directory, so two writers collide and one silently wins.
   The single-flight lock makes that unlikely *inside* a run, but `--status`,
   `--requeue-ocr` and the nightly fire are separate processes.

⚠️ The temp file is created in the TARGET'S OWN DIRECTORY on purpose: a
cross-device `os.replace` is not atomic and can raise.

⚠️ THIS FIXES THE WRITE HALF ONLY. The read half is a separate, open decision
(`docs/TODO.md` F3): `load_state()` raising on a corrupt file is arguably
correct — degrading to an empty state would re-transcribe 1,227 books — but it
should refuse loudly and name the file rather than surfacing a bare
`JSONDecodeError`. Not bundled here.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(
    path: Path,
    data: Any,
    *,
    indent: int = 1,
    ensure_ascii: bool = False,
) -> None:
    """Write `data` to `path` as JSON, atomically and durably.

    `indent`/`ensure_ascii` are passed through so each caller keeps its file
    byte-comparable to the form it wrote before this helper existed —
    `save_state` and `write_queue_summary` use `indent=1, ensure_ascii=False`;
    `sync_to_drive` uses `indent=2, ensure_ascii=False`. They are NOT a style
    choice to be normalised: `ingest_state.json` is 1,244 rows a human reads
    with `--status`, and re-indenting it would rewrite the whole file.

    Raises whatever the write raises, after removing the temp file. Callers
    that must never fail (`write_queue_summary`) catch it themselves; this
    function deliberately does not decide that for them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=indent, ensure_ascii=ensure_ascii)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup; never mask the original error.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
