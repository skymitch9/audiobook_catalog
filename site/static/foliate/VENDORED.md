# foliate-js — VENDORED, PINNED TO A COMMIT, DO NOT EDIT

| | |
|---|---|
| Package | `foliate-js` (no npm release; the repo IS the distribution) |
| **Pinned at** | **`78914aef4466eb960965702401634c2cb348e9b1`** — committed 2026-05-01, *"Use original hrefs for external links and add isExternal in fb2.js (#129)"* |
| Upstream | John Factotum — <https://github.com/johnfactotum/foliate-js> |
| Licence | **MIT** — full text in [`LICENSE`](LICENSE), copied verbatim from the tree |
| Vendored | 2026-08-17, viewer phase 2 |
| Source of the copy | `https://codeload.github.com/johnfactotum/foliate-js/tar.gz/78914aef…`, files taken unmodified |

⚠️ **A COMMIT, not `@main`.** The 2026-08-17 EPUB streaming probe measured
`@main` and said so in its own "what was not measured" list, because `@main` is
a moving target and numbers taken against it belong to nothing. This is that
tech-debt item closed: `78914ae` **was** `main` on the probe date (its parent
commit is from 2026-05-01 and nothing landed between), so the measured
behaviour and the shipped behaviour are the same bytes.

## Why it is here and not on a CDN

`/read`'s `Content-Security-Policy` (`site/_headers`) is `script-src 'self'`
plus gstatic for the Firebase SDK and names no CDN, so a runtime CDN import
would not merely be against policy — it would be blocked, and the reader would
fail to load. Same posture as the vendored pdf.js beside this.

## ⚠️ What is here, what is NOT, and the one omission that is load-bearing

| Path | Why |
|---|---|
| `view.js` | the `<foliate-view>` custom element — the reader shell |
| `epub.js` | the EPUB model. ⚠️ **This is what the reader constructs directly**, `new EPUB(loader).init()`, so it can pass its OWN loader |
| `paginator.js` | reflowable pagination (`<foliate-paginator>`) |
| `fixed-layout.js` | pre-paginated books — the shelf has comic-style fixed-layout EPUBs (`whitesand.epub` is one) |
| `epubcfi.js`, `progress.js`, `overlayer.js`, `text-walker.js` | `view.js`'s static imports |
| `search.js` | `view.js`'s `search()` dynamic import |
| `LICENSE` | MIT, as redistribution requires |
| ~~`vendor/zip.js`~~ | ⚠️ **DELIBERATELY ABSENT — see below** |
| ~~`vendor/fflate.js`, `mobi.js`, `fb2.js`, `comic-book.js`, `pdf.js`~~ | other formats. This shelf has EPUB and PDF, and PDF is pdf.js's job |
| ~~`tts.js`, `dict.js`, `opds.js`, `quote-image.js`, `footnotes.js`, `ui/`~~ | features this reader does not offer |
| ~~`reader.html`, `reader.js`, `tests/`, `rollup/`~~ | foliate's own demo app and build tooling |

### ⚠️ `vendor/zip.js` is omitted ON PURPOSE, and it is a MECHANICAL GUARD

`view.js`'s `makeBook(file)` — the normal way to open a book with this library —
builds `new ZipReader(new BlobReader(file))` over a **whole in-memory Blob**.
On the shelf's largest book that is 412,436,591 bytes pulled through a gated
Worker before a word renders. The entire point of viewer phase 2 is that the
reader does **not** do that (`site/epub-loader.js`), and the measured difference
is four orders of magnitude on bytes and two on heap.

A comment asking future agents not to call `makeBook` is advice. Omitting the
module it needs is a guard: `makeZipLoader`'s `await import('./vendor/zip.js')`
cannot resolve, so **the whole-file path cannot run at all.** If someone
genuinely needs it one day, vendoring the file back is a deliberate act with a
reason attached — which is exactly the bar it should have to clear.

`site/__tests__/epub-loader-wiring.test.js` and `tests/test_reader_page.py` both
fail if the file reappears or if `reader.js` reaches for `makeBook`.

## How the reader uses it

```js
// site/epub-loader.js — the deliberate injection point
const book = await new EPUB({ loadText, loadBlob, getSize }).init();  // OUR loader
// site/reader.js
const view = document.createElement('foliate-view');
await view.open(book);   // `View.open` passes an already-built book straight through
```

⚠️ **`View.open()` re-enters `makeBook` if it is handed a string, a Blob, or a
directory entry** (`view.js`, `open()`'s first three lines). It must only ever
be given an already-opened book object. That is the single line where the
whole-file path could come back.

## The loader contract, which upstream does not document

Read off `epub.js`'s constructor rather than a README, because there is no
README entry for it:

```
loadText(name)        -> Promise<string|null>   (null when the entry is absent)
loadBlob(name, type)  -> Promise<Blob|null>
getSize(name)         -> number                 (0 when absent)
```

⚠️ **`null` for a missing entry, not a throw.** `epub.js` probes for optional
files (`META-INF/com.apple.ibooks.display-options.xml`, calibre bookmarks) and
treats a throw as a broken book.

## CSP notes that cost real time

⚠️ **foliate rewrites an EPUB's own stylesheets, fonts and images to `blob:`
URLs, and `'self'` DOES NOT COVER `blob:`.** `img-src`/`frame-src` already had
it from phase 1; `style-src` and `font-src` did **not**, and had to be added.
Measured both ways on a real book: without `blob:` in `style-src` the linked
sheet yields **zero rules** and the body renders in Times New Roman; with it,
84 rules and the book's own Palatino. ⚠️ The failure is silent, looks like a
badly-made book rather than a blocked request, and **the page's own
`securitypolicyviolation` listener never hears it** — the section is a `blob:`
iframe inside a **closed** shadow root, so the violation fires on that document.
Full note in `site/_headers`.

## How to update

1. Pick a commit, read its diff against `78914ae` — especially `view.js`'s
   `makeBook`/`makeZipLoader` and `epub.js`'s constructor, which are the two
   places this integration touches.
2. Re-download that commit's tarball; copy the files in the table above,
   unmodified. **Do not copy `vendor/`.**
3. Update the pinned SHA in this file AND in `tests/test_reader_page.py`
   (`FOLIATE_COMMIT`) — the test is what stops this doc from lying.
4. Re-run the range measurement. A change to `epub.js`'s loader use is the one
   that would silently reinstate a whole-file read, and only a request count
   catches it.
