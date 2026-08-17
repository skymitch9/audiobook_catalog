/**
 * epub-loader.js — foliate-js, opened over HTTP byte ranges (viewer phase 2).
 *
 * ⚠️ THE ONE THING TO UNDERSTAND BEFORE EDITING THIS FILE.
 *
 * foliate-js ships a perfectly good `makeBook(file)` in `view.js`, and calling
 * it is WRONG here. It builds `new ZipReader(new BlobReader(file))` over a
 * whole in-memory Blob, so it needs the entire archive on the client before it
 * renders a word. On the shelf's largest book that is 412,436,591 bytes over a
 * household uplink, through a gated Worker, on someone's phone. This file
 * exists to hand foliate's `EPUB` class a DIFFERENT loader — one backed by
 * `epub-range.js`'s range-only transport — and then hand the finished book
 * straight to `<foliate-view>`, whose `open()` passes an already-built book
 * through untouched.
 *
 *   makeBook(file)          ->  BlobReader  ->  whole file.     NEVER USED HERE.
 *   new EPUB(rangeLoader)   ->  our reader  ->  ~77 KiB.        This.
 *
 * The vendored drop makes that mechanical rather than advisory: foliate's
 * `vendor/zip.js` is NOT vendored (see site/static/foliate/VENDORED.md), so
 * `makeZipLoader`'s dynamic import cannot resolve and the whole-file path
 * cannot run even if a future edit reaches for it.
 *
 * ## The loader contract, which is tiny and undocumented upstream
 *
 * `new EPUB({ loadText, loadBlob, getSize }).init()` is all foliate's EPUB
 * needs — read from `epub.js`'s constructor, not from a README:
 *
 *   loadText(name)        -> Promise<string|null>   (null when absent)
 *   loadBlob(name, type)  -> Promise<Blob|null>
 *   getSize(name)         -> number                 (0 when absent)
 *
 * `entries` is in foliate's own loader for the CBZ/FB2 paths and is unused by
 * EPUB; it is returned anyway because it costs nothing and it is what the
 * acceptance measurement counts.
 *
 * ## What this costs, measured
 *
 * See the header of `epub-range.js` and
 * `library_catalog/docs/info/epub-streaming-findings-2026-08-17.md`.
 */

import { createRangeSource } from './epub-range.js';

/** The pinned vendored drops. Relative, so the /dev/ lane loads /dev/ copies. */
const ZIPJS = './static/zipjs/zip-no-worker-inflate.js';
const FOLIATE_EPUB = './static/foliate/epub.js';
const FOLIATE_VIEW = './static/foliate/view.js';

/**
 * Load `<foliate-view>` and register the custom element.
 *
 * ⚠️ Dynamic, not a static import, so a reader opening a PDF never pays for the
 * EPUB stack — and so a failure to load it is catchable and can be WORDED
 * rather than blanking the page.
 */
export function loadFoliateView() {
  return import(FOLIATE_VIEW);
}

/**
 * Open one EPUB over byte ranges.
 *
 * @param {object} o
 * @param {string} o.url            the gated byte stream for this anchor
 * @param {() => Promise<string|null>} o.getAuthHeader  called PER REQUEST
 * @param {typeof fetch} [o.fetchImpl]                  injected for tests
 * @returns {Promise<{book: object, stats: object, entries: Array}>}
 */
/**
 * Turn an already-initialised range source into foliate's loader object.
 *
 * ⚠️ Split out and EXPORTED so it can be exercised in Node against a real ZIP
 * with a counting `fetch` (site/__tests__/epub-loader.test.js). The thing worth
 * testing here is not that a book opens — it is HOW MANY BYTES opening it
 * costs, and that number is invisible in any test that only checks the result.
 *
 * `zip` is the vendored zip.js module, passed in rather than imported, for the
 * same reason.
 */
export function createGatedZipLoader({ zip, source }) {
  /**
   * zip.js's `Reader` contract is `{ size, init(), readUint8Array(i, n) }` —
   * SUBCLASSED rather than duck-typed so the base class's `readable` getter
   * comes along. zip.js uses it to stream an entry's data, in 64 KiB chunks,
   * THROUGH `readUint8Array` — i.e. through ranges as well.
   */
  class GatedRangeReader extends zip.Reader {
    constructor() {
      super();
      this.size = source.size;
    }
    init() {
      // The network probe already happened in `source.init()`; this only marks
      // the stream initialised so zip.js does not wait on anything.
      super.init();
    }
    readUint8Array(index, length) {
      return source.read(index, length);
    }
  }

  return async () => {
    const zipReader = new zip.ZipReader(new GatedRangeReader());
    const entries = await zipReader.getEntries();
    const map = new Map(entries.map((entry) => [entry.filename, entry]));

    // A faithful copy of foliate `view.js`'s `makeZipLoader` shape — the same
    // null-when-absent semantics, because `epub.js` relies on them (a missing
    // `META-INF/com.apple.ibooks.display-options.xml` is normal, not an error).
    const load = (f) => (name, ...args) => (map.has(name) ? f(map.get(name), ...args) : null);
    const loadText = load((entry) => entry.getData(new zip.TextWriter()));
    const loadBlob = load((entry, type) => entry.getData(new zip.BlobWriter(type)));
    const getSize = (name) => map.get(name)?.uncompressedSize ?? 0;
    return { entries, loadText, loadBlob, getSize };
  };
}

export async function openEpubOverRanges({ url, getAuthHeader, fetchImpl }) {
  const zip = await import(ZIPJS);

  // ⚠️ No web workers. The `-no-worker-inflate` build ships no worker script at
  // all, so leaving this on would have zip.js look for one that is not there;
  // and a blob-URL worker is a CSP surface this page does not need. foliate's
  // own loader makes the same call for the same reason.
  zip.configure({ useWebWorkers: false });

  const source = createRangeSource({ url, getAuthHeader, fetchImpl });
  await source.init();

  const loader = await createGatedZipLoader({ zip, source })();

  const { EPUB } = await import(FOLIATE_EPUB);
  const book = await new EPUB(loader).init();

  return { book, stats: source.stats, entries: loader.entries };
}
