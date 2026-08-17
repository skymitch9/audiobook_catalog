# pdf.js — VENDORED, PINNED, DO NOT EDIT

| | |
|---|---|
| Package | `pdfjs-dist` |
| **Version** | **5.4.149** — pinned deliberately; see "How to update" below |
| Upstream | Mozilla pdf.js — <https://github.com/mozilla/pdf.js> |
| Licence | **Apache License 2.0** — the full text is in [`LICENSE`](LICENSE), copied verbatim from the package |
| Vendored | 2026-08-17, viewer phase 1b |
| Source of the copy | `npm pack pdfjs-dist@5.4.149`, files taken unmodified from the tarball |

## Why it is here and not on a CDN

⚠️ **The estate's script posture is `'self'` plus gstatic for the Firebase
SDK, and nothing else** (viewer design §4.4). A CDN `<script src>` is a third
party that can change the bytes running against a page holding a Firebase ID
token. `/read`'s `Content-Security-Policy` in `site/_headers` names no CDN,
so a runtime CDN fetch would not merely be against policy — it would be
blocked, and the reader would simply fail to load.

## What is here, and what is deliberately not

| Path | Why |
|---|---|
| `build/pdf.min.js` | the API module the reader imports |
| `build/pdf.worker.min.js` | the worker thread; `workerSrc` points at it |
| `cmaps/` (169 files) | CJK character maps. ⚠️ Needed for PDFs whose CJK fonts are **not** embedded — the shelf has Japanese light-novel PDFs (`Kumo Kagyu/Goblin Slayer, Vol. 1.pdf`). Without these such a page renders blank or as boxes, which looks like a corrupt file |
| `standard_fonts/` (16 files) | the base-14 font substitutes. Without them, a PDF that references Helvetica/Times without embedding it renders with no text at all |
| `LICENSE` | Apache-2.0, as the licence requires when redistributing |
| ~~`build/*.map`~~ | omitted: 12 MB of source maps nobody debugs in production |
| ~~`build/pdf.mjs` (unminified)~~ | omitted for the same reason. ⚠️ It is the readable copy — re-download the tarball to read the source rather than hunting in the minified file |
| ~~`web/`~~ | omitted: pdf.js's own full viewer UI. This estate ships its own reader shell (`app/web/templates/read.html`), in the shelf's paper-and-ink idiom |
| ~~`build/pdf.sandbox.*`~~ | omitted: the AcroForm JavaScript sandbox. These are books, not forms, and it is a scripting engine nobody asked for |

## The one API guarantee this design leans on — VERIFIED against these bytes

`getDocument({ httpHeaders })` is what makes "a bearer on every range request,
never a credential in a URL" affordable rather than aspirational (viewer design
§3.3). It was listed as unverified; it is verified now, by reading the shipped
source rather than by trusting the docs:

```js
// pdfjs-dist@5.4.149, build/pdf.mjs:11158
function createHeaders(isHttp, httpHeaders) {
  const headers = new Headers();
  if (!isHttp || !httpHeaders || typeof httpHeaders !== "object") return headers;
  for (const key in httpHeaders) { ... headers.append(key, val); }
  return headers;
}
```

`src.httpHeaders` is read at `pdf.mjs:12571` and reaches both transports. ⚠️ Note
the `isHttp` guard: headers are applied to `http(s)` URLs only, which ours is.

⚠️ **If a future version drops `httpHeaders`, the whole no-credential-in-a-URL
decision needs revisiting** — viewer design §3.3 names the fallback (a
short-lived, book-scoped read lease). Check for it before bumping.

## How to update

1. `npm pack pdfjs-dist@<new version>` and unpack it.
2. **Grep the new `build/pdf.mjs` for `httpHeaders`** and confirm it still
   reaches a real `Headers` object. If it does not, STOP and read §3.3.
3. Copy the same file set as the table above — no more, no less.
4. Update the version in this file, in `site/_headers`' comment, and in
   `app/web/templates/read.html`'s header.
5. `python -m pytest tests/test_pdfjs_vendor.py` — it fails when the version
   claimed here and the version in the page disagree, or when a required file
   is missing.
6. Exercise a real PDF in the `/dev/` lane before promoting. A pdf.js bump has
   no test that can tell you a page renders.
