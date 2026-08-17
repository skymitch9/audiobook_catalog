# @zip.js/zip.js — VENDORED, PINNED, DO NOT EDIT

| | |
|---|---|
| Package | `@zip.js/zip.js` |
| **Version** | **2.7.45** — pinned deliberately; see "Why this version" |
| Upstream | Gildas Lormeau — <https://github.com/gildas-lormeau/zip.js> |
| Licence | **BSD-3-Clause** — full text in [`LICENSE`](LICENSE), copied verbatim from the package |
| Vendored | 2026-08-17, viewer phase 2 |
| Source of the copy | `npm pack @zip.js/zip.js@2.7.45`; the package's **`lib/` tree**, contents unmodified |
| Entry point | **`zip-no-worker-inflate.js`** |

## Why this version, and not the latest

2.7.45 is the version the 2026-08-17 EPUB streaming probe measured — the run
that opened the 393 MiB White Sand Omnibus in 15 ranges and 76.9 KiB and
decided this whole phase. Pinning to the measured version means the shipped
behaviour and the evidence for it are the same code. 2.8.51 was current on the
vendoring date; bumping to it is a deliberate act that must re-run the range
measurement, not a maintenance chore.

## ⚠️ Two deviations from "copied verbatim", both forced, both documented

### 1. The `lib/` directory level was dropped

The package ships these files under `lib/`. `.gitignore` line 137 carries the
standard Python `lib/` rule, which matched **every one of the 44 files** and
would have excluded the entire library from git — present on disk, absent from
the deploy, renderer 404s, book never opens, nothing anywhere saying why.

⚠️ **This is the SAME failure pdf.js hit at phase 1b**, where the Python
`build/` rule silently dropped both renderer files out of
`git add site/static/pdfjs`. It was caught that time by reading the commit.
Twice is a pattern: for a vendored dependency, *"present in my working tree"*
and *"will reach the deployment"* are different facts and only the second one
matters.

The contents are byte-identical and every internal import is relative
(`./core/...`), so removing the parent directory changes nothing at runtime.
The alternative — a `.gitignore` negation — was rejected because git cannot
re-include files inside an excluded **directory** without first re-including
the directory, which makes the negation a two-line incantation that the next
person deletes as noise.

`tests/test_reader_page.py` asserts these files are **TRACKED IN GIT**, not
merely on disk.

### 2. `zip-no-worker-inflate.js` is the entry, and the rest of the tree rides along

Only that entry and what it imports is ever loaded. The other entries
(`zip-full.js`, `zip-fs.js`, the `z-worker-*` bootstraps) are copied because
copying the tree whole is a smaller, more auditable claim than curating it, and
because an unimported ES module costs nothing.

⚠️ **`-no-worker-`** is chosen, not accidental: this build ships **no worker
script**, so there is nothing for a `worker-src` policy to argue with and no
blob-URL worker to allow. `site/epub-loader.js` also calls
`configure({ useWebWorkers: false })`, exactly as foliate's own loader does.
⚠️ **`-inflate`** is read-only — no deflate codec, ~28 KB smaller, and it is
structurally incapable of writing a ZIP, which is the right shape for a reader.

## What this reader uses from it

| Export | Used for |
|---|---|
| `Reader` | **subclassed** by `GatedRangeReader` in `site/epub-loader.js` — the injection point for HTTP byte ranges |
| `ZipReader` | reads the central directory and the entries |
| `TextWriter` / `BlobWriter` | the two output shapes foliate's EPUB loader needs |
| `configure` | `{ useWebWorkers: false }` |

⚠️ **`HttpRangeReader` is exported and is deliberately NOT used.** The design
requires a bearer token on every request, fetched fresh per range so a reading
session outlives a one-hour token; zip.js's own HTTP readers take a fixed
header list at construction. `Reader` is subclassed instead — about thirty
lines, and the range arithmetic and auth attachment become unit-testable in
Node with a counting fake `fetch` (`site/__tests__/epub-range.test.js`).

## How to update

1. `npm pack @zip.js/zip.js@<version>`, copy `package/lib/*` **with the `lib/`
   level removed** (see above) and `package/LICENSE`.
2. Update the version in this file AND in `tests/test_reader_page.py`
   (`ZIPJS_VERSION`).
3. Re-check that `Reader` is still exported from `core/io.js` and still calls
   `readUint8Array(index, length)` — that is the whole contract this integration
   leans on, and it is not part of any documented public API.
4. Re-run the range measurement against a large book. A version that quietly
   starts buffering is invisible except in the request count.
