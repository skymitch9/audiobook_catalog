/**
 * epub-range.js — the range-reading transport, and NOTHING else (viewer phase 2).
 *
 * ⚠️ THIS FILE IS DELIBERATELY FREE OF zip.js AND foliate-js IMPORTS, and that
 * is not tidiness either. It is the half that can be unit-tested in Node with a
 * counting fake `fetch` — see site/__tests__/epub-range.test.js — and the half
 * where the whole win of viewer phase 2 lives. `epub-loader.js` is the thin
 * browser-only wrapper that hands this to zip.js.
 *
 * ## What it is for
 *
 * An EPUB is a ZIP. The naive way to render one is to download the whole
 * archive and inflate it, which is what BOTH shipped readers do by default.
 * MEASURED 2026-08-17 on the 393 MiB White Sand Omnibus
 * (library_catalog/docs/info/epub-streaming-findings-2026-08-17.md):
 *
 *   epub.js, whole file        412,436,591 B over the wire, 1,207 MB of JS heap
 *   foliate + range reading         78,741 B over the wire,   10.4 MB of heap
 *
 * Four orders of magnitude on bytes, two on memory, for the same book. The
 * difference is entirely this file: a ZIP's central directory lives at the END
 * of the archive, so a reader that can ask for byte spans opens a 393 MiB book
 * by fetching about 77 KiB of it.
 *
 * ## ⚠️ The three rules this transport enforces MECHANICALLY
 *
 * **1. It cannot fall back to a whole-file download.** There is no code path
 * here that fetches without a `Range` header, and a `200` answer to a ranged
 * request is treated as a NAMED FAILURE (`RangeUnsupportedError`) rather than
 * as a body to read. The known trap for this phase is foliate's own `view.js`,
 * whose `makeZipLoader` builds `new ZipReader(new BlobReader(file))` over a
 * whole in-memory Blob; routing through it silently undoes everything above.
 * A transport that physically cannot whole-file is worth more than a comment
 * asking nobody to.
 *
 * **2. The bearer rides on EVERY request, including the size probe.** Same
 * decision as pdf.js's `httpHeaders` (see reader.js) and for the same reason: a
 * credential in a URL survives in history, referrers, screenshots and any log
 * that records request lines, and cannot be revoked mid-session.
 *
 * ⚠️ **And it is fetched PER REQUEST, which pdf.js cannot do.** pdf.js captures
 * `httpHeaders` once at `getDocument`, so a reading session outliving the
 * token's hour ends in a 401 — listed as unhandled in
 * catalog-platform/docs/info/ebook-viewer-phase1.md §7. Here the header comes
 * from a callback on each range, and the Firebase SDK's `getIdToken()` returns
 * a cached token until it is near expiry and refreshes it transparently after.
 * So the EPUB path does not have phase 1b's expiry gap. Do not "simplify" this
 * into a captured string.
 *
 * **3. The file must not change underneath a half-read archive.** The central
 * directory is read once; if the object is replaced mid-session every offset in
 * it is wrong, and inflate produces garbage rather than an error. The `ETag`
 * from the size probe is remembered and checked on every later span, so that
 * turns into a worded failure instead of a corrupt page.
 *
 * ## What the endpoint promises (phase 1a, as-built)
 *
 * `GET /api/ebook/:anchor/file` on audiobook-api.heygabi.ai answers
 * `Accept-Ranges: bytes`, honours ONE `bytes=A-B` span with `206` +
 * `Content-Range`, exposes `Content-Range`/`Content-Length`/`Accept-Ranges`/
 * `ETag` through CORS, and **ignores** a malformed or multi-span `Range` —
 * answering `200` with the whole file. That last row is why rule 1 exists: the
 * failure mode of a bad range here is a 393 MiB download, not an error.
 */

/** A ranged request was answered with the whole file. Never read the body. */
export class RangeUnsupportedError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RangeUnsupportedError';
  }
}

/** The object changed while it was being read. Offsets are now lies. */
export class ObjectChangedError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ObjectChangedError';
  }
}

/** An HTTP refusal, carrying the status so the reader can word it. */
export class HttpStatusError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.name = 'HttpStatusError';
    this.status = status;
    this.detail = detail || null;
  }
}

/**
 * `bytes=A-B`, inclusive on both ends — the wire form, built in exactly one
 * place so the off-by-one lives in exactly one place too.
 *
 * ⚠️ `length` of 0 has no valid representation: `bytes=A-(A-1)` is malformed,
 * and phase 1a IGNORES a malformed range and answers 200 with the whole file.
 * So a zero-length read must never reach the network.
 */
export function rangeHeaderFor(index, length) {
  if (!Number.isInteger(index) || index < 0) throw new RangeError(`range index must be a non-negative integer, got ${index}`);
  if (!Number.isInteger(length) || length <= 0) throw new RangeError(`range length must be a positive integer, got ${length}`);
  return `bytes=${index}-${index + length - 1}`;
}

/**
 * Read the total size out of a `Content-Range` response header.
 *
 * The two shapes that appear: `bytes 0-0/412436591` on a 206, and
 * `bytes *\/412436591` on a 416. Both carry the total, which is the only part
 * this needs. `*` for the total (a server that does not know) returns null.
 */
export function totalFromContentRange(value) {
  if (typeof value !== 'string') return null;
  const m = /^bytes\s+(?:\d+-\d+|\*)\/(\d+)$/i.exec(value.trim());
  return m ? Number(m[1]) : null;
}

/**
 * Build the request headers for one span.
 *
 * ⚠️ `auth` is whatever the caller's token getter returned THIS request. An
 * empty/absent one is passed through rather than faked: the endpoint answers a
 * worded 401 and the reader shows it, which is the honest outcome. Inventing a
 * header here would turn "your sign-in lapsed" into "something went wrong".
 */
export function buildRangeHeaders(index, length, auth) {
  const headers = { Range: rangeHeaderFor(index, length) };
  if (auth) headers.Authorization = auth;
  return headers;
}

/**
 * A counting, authenticated, range-only reader over one URL.
 *
 * Returns `{ init, read, size, stats }`:
 *   - `init()`   probes `bytes=0-0` for the total size and the ETag;
 *   - `read(i,n)` answers a `Uint8Array` of exactly n bytes from offset i;
 *   - `stats`    `{ requests, bytes, wholeFileRequests }` — the instrumentation
 *     the acceptance test reads. ⚠️ `wholeFileRequests` is a tripwire, not a
 *     counter: it can only ever be incremented by a 200 answer, which is also
 *     the moment `read` throws.
 *
 * `fetchImpl` and `getAuthHeader` are injected so the whole thing is testable
 * without a browser, a network or a token.
 */
export function createRangeSource({ url, getAuthHeader, fetchImpl }) {
  const doFetch = fetchImpl || ((...a) => fetch(...a));
  const authOf = getAuthHeader || (async () => null);
  const stats = { requests: 0, bytes: 0, wholeFileRequests: 0 };
  let size = null;
  let etag = null;

  /** One ranged GET. Every request in this module goes through here. */
  async function ranged(index, length) {
    const auth = await authOf();
    const headers = buildRangeHeaders(index, length, auth);
    stats.requests += 1;
    const res = await doFetch(url, { headers, method: 'GET' });

    if (res.status === 200) {
      // ⚠️ RULE 1. The endpoint ignores a Range it cannot parse and sends the
      // whole file; an origin or a proxy that strips Range does the same. Read
      // this body and a 393 MiB book lands in memory — the exact outcome this
      // phase exists to prevent. Cancel it and name it.
      stats.wholeFileRequests += 1;
      try { await res.body?.cancel(); } catch { /* already discarded */ }
      throw new RangeUnsupportedError(
        'The shelf answered a byte-range request with the whole file, so the reader stopped rather than download it.',
      );
    }
    if (res.status !== 206) {
      let detail = null;
      try { const b = await res.json(); detail = typeof b.detail === 'string' ? b.detail : null; } catch { /* not JSON, nothing worth quoting */ }
      throw new HttpStatusError(res.status, detail);
    }

    // ⚠️ RULE 3. A replaced object invalidates every offset already read out of
    // the central directory, and inflate answers garbage rather than an error.
    const tag = res.headers.get('ETag');
    if (etag && tag && tag !== etag) {
      throw new ObjectChangedError('This book’s file changed while it was open, so the reader stopped rather than show you the wrong pages.');
    }

    const buf = new Uint8Array(await res.arrayBuffer());
    stats.bytes += buf.byteLength;
    return { buf, res };
  }

  return {
    stats,
    get size() { return size; },

    /**
     * ⚠️ `bytes=0-0`, not `HEAD`. It is what zip.js's own `HttpRangeReader`
     * does, it is what the 2026-08-17 probe measured, and it proves range
     * support and total size in ONE request. A `HEAD` against R2 through the
     * Worker is untested (findings §7) and would prove only the second.
     */
    async init() {
      if (size !== null) return size;
      const { res } = await ranged(0, 1);
      const total = totalFromContentRange(res.headers.get('Content-Range'));
      if (total === null || !Number.isFinite(total) || total <= 0) {
        throw new RangeUnsupportedError(
          'The shelf did not say how big this book’s file is, so the reader cannot read it a piece at a time.',
        );
      }
      etag = res.headers.get('ETag') || null;
      size = total;
      return size;
    },

    /** Exactly `length` bytes from `index`. Short answers are a failure. */
    async read(index, length) {
      if (length === 0) return new Uint8Array(0); // never a request; see rangeHeaderFor
      const { buf } = await ranged(index, length);
      if (buf.byteLength !== length) {
        throw new HttpStatusError(206, `The shelf sent ${buf.byteLength} bytes where ${length} were asked for.`);
      }
      return buf;
    },
  };
}
