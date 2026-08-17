/**
 * The EPUB range transport — viewer phase 2.
 *
 * ⚠️ WHAT THIS FILE IS ACTUALLY DEFENDING. The entire value of viewer phase 2
 * is a number: the 393 MiB White Sand Omnibus opens in 18 requests totalling
 * 664 KB instead of one request totalling 412,436,591 B. Nothing about the
 * reader LOOKS different when that regresses — the book still opens, still
 * paginates, still turns pages. It is just four orders of magnitude more
 * expensive, on a household uplink, on someone's phone.
 *
 * So these tests COUNT. The mutation they exist to fail is "swap the range
 * reader for a whole-file fetch", in any of its forms, and a test that only
 * asserted the book opened would pass every one of them.
 */
import { describe, it, expect } from 'vitest';
import {
  createRangeSource,
  rangeHeaderFor,
  totalFromContentRange,
  buildRangeHeaders,
  RangeUnsupportedError,
  ObjectChangedError,
  HttpStatusError,
} from '../epub-range.js';

/* ── a fake origin that behaves like the phase-1a byte stream ───────────── */

/**
 * @param bytes the whole "object"
 * @param opts.ignoreRange  answer 200 + the whole file, as phase 1a does for a
 *                          malformed or multi-span Range (its documented
 *                          behaviour, and the reason RangeUnsupportedError
 *                          exists)
 * @param opts.requireAuth  401 unless a bearer arrives
 */
function fakeOrigin(bytes, opts = {}) {
  const calls = [];
  const etag = opts.etag ?? '"v1"';
  const fetchImpl = async (url, init) => {
    const headers = init?.headers ?? {};
    calls.push({ url, headers });
    if (opts.requireAuth && !headers.Authorization) {
      return new Response(JSON.stringify({ detail: 'Sign in to read this.' }), {
        status: 401, headers: { 'Content-Type': 'application/json' },
      });
    }
    if (opts.status && opts.status !== 206) {
      return new Response(JSON.stringify({ detail: opts.detail ?? null }), {
        status: opts.status, headers: { 'Content-Type': 'application/json' },
      });
    }
    const m = /^bytes=(\d+)-(\d+)$/.exec(headers.Range ?? '');
    if (!m || opts.ignoreRange) {
      return new Response(bytes, {
        status: 200,
        headers: { 'Content-Length': String(bytes.length), ETag: etag },
      });
    }
    const start = Number(m[1]);
    const end = Math.min(Number(m[2]), bytes.length - 1);
    if (start >= bytes.length) {
      return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${bytes.length}` } });
    }
    const slice = bytes.slice(start, end + 1);
    return new Response(slice, {
      status: 206,
      headers: {
        'Content-Range': `bytes ${start}-${end}/${bytes.length}`,
        'Content-Length': String(slice.length),
        ETag: opts.etagPerCall ? opts.etagPerCall(calls.length) : etag,
      },
    });
  };
  return { fetchImpl, calls };
}

const body = new Uint8Array(1024).map((_, i) => i % 251);

/* ── the wire format ────────────────────────────────────────────────────── */

describe('rangeHeaderFor', () => {
  it('is inclusive on both ends', () => {
    expect(rangeHeaderFor(0, 1)).toBe('bytes=0-0');
    expect(rangeHeaderFor(0, 1024)).toBe('bytes=0-1023');
    expect(rangeHeaderFor(412436569, 22)).toBe('bytes=412436569-412436590');
  });

  it('⚠️ REFUSES a zero length rather than emitting `bytes=A-(A-1)`', () => {
    // A malformed Range is IGNORED by the endpoint, which answers 200 with the
    // whole file. So an off-by-one here is not a 400 — it is a 393 MiB
    // download. It must never reach the network.
    expect(() => rangeHeaderFor(10, 0)).toThrow(RangeError);
    expect(() => rangeHeaderFor(10, -5)).toThrow(RangeError);
    expect(() => rangeHeaderFor(-1, 10)).toThrow(RangeError);
    expect(() => rangeHeaderFor(1.5, 10)).toThrow(RangeError);
  });
});

describe('totalFromContentRange', () => {
  it('reads the total off a 206', () => {
    expect(totalFromContentRange('bytes 0-0/412436591')).toBe(412436591);
  });
  it('reads the total off a 416, whose form is different', () => {
    expect(totalFromContentRange('bytes */412436591')).toBe(412436591);
  });
  it('answers null for anything it does not understand', () => {
    for (const v of [null, undefined, '', 'bytes 0-0/*', 'items 0-0/10', '0-0/10']) {
      expect(totalFromContentRange(v)).toBeNull();
    }
  });
});

/* ── auth ───────────────────────────────────────────────────────────────── */

describe('auth attachment', () => {
  it('puts the bearer in a HEADER, never in the URL', async () => {
    const { fetchImpl, calls } = fakeOrigin(body, { requireAuth: true });
    const src = createRangeSource({
      url: 'https://audiobook-api.heygabi.ai/api/ebook/b-abc/file',
      getAuthHeader: async () => 'Bearer tok-1',
      fetchImpl,
    });
    await src.init();
    await src.read(0, 16);
    expect(calls.length).toBe(2);
    for (const c of calls) {
      expect(c.headers.Authorization).toBe('Bearer tok-1');
      // ⚠️ A credential in a URL survives in history, referrers, screenshots
      // and any log that records request lines, and cannot be revoked.
      expect(c.url).not.toMatch(/tok-1|token=|auth=/);
    }
  });

  it('⚠️ carries the bearer on the SIZE PROBE too, not only on data reads', async () => {
    // The probe is a real ranged GET against a gated object. An unauthenticated
    // one is a 401, and a reader that only authenticated its data reads would
    // fail at open with a misleading message.
    const { fetchImpl, calls } = fakeOrigin(body, { requireAuth: true });
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    await src.init();
    expect(calls[0].headers.Authorization).toBe('Bearer t');
    expect(calls[0].headers.Range).toBe('bytes=0-0');
  });

  it('⚠️ re-asks for the token on EVERY request, so an hour-long read survives', async () => {
    // pdf.js captures headers once at getDocument and cannot do this; the EPUB
    // path can, and that is why phase 1b's "token expiry mid-session is
    // unhandled" does not apply here. Capturing the token once would pass every
    // other test in this file.
    let n = 0;
    const { fetchImpl, calls } = fakeOrigin(body);
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => `Bearer t${++n}`, fetchImpl });
    await src.init();
    await src.read(0, 8);
    await src.read(8, 8);
    expect(calls.map((c) => c.headers.Authorization)).toEqual(['Bearer t1', 'Bearer t2', 'Bearer t3']);
  });

  it('passes a missing token through rather than faking one', async () => {
    const { fetchImpl } = fakeOrigin(body, { requireAuth: true });
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => null, fetchImpl });
    // The endpoint's own worded 401 reaches the reader, which is the honest
    // outcome; inventing a header would turn it into "something went wrong".
    await expect(src.init()).rejects.toMatchObject({ name: 'HttpStatusError', status: 401 });
  });

  it('buildRangeHeaders omits Authorization entirely when there is none', () => {
    expect(buildRangeHeaders(0, 1, null)).toEqual({ Range: 'bytes=0-0' });
    expect(buildRangeHeaders(0, 1, 'Bearer x')).toEqual({ Range: 'bytes=0-0', Authorization: 'Bearer x' });
  });
});

/* ── ⚠️ THE COUNTING TESTS — the mutation guard ─────────────────────────── */

describe('⚠️ it range-reads, and CANNOT whole-file', () => {
  it('reads only what it asks for, and counts it', async () => {
    const big = new Uint8Array(5_000_000);
    const { fetchImpl, calls } = fakeOrigin(big);
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    expect(await src.init()).toBe(5_000_000);
    await src.read(4_999_000, 1000);
    await src.read(0, 512);

    // THE ASSERTION THAT MATTERS: a tiny fraction of a large object.
    expect(src.stats.requests).toBe(3);
    expect(src.stats.bytes).toBe(1 + 1000 + 512);
    expect(src.stats.bytes).toBeLessThan(big.length / 1000);
    expect(src.stats.wholeFileRequests).toBe(0);
    // Every single request carried a Range. Not one bare GET.
    expect(calls.every((c) => /^bytes=\d+-\d+$/.test(c.headers.Range))).toBe(true);
  });

  it('⚠️ THROWS on a 200 instead of reading the body — the whole-file tripwire', async () => {
    // This is the mutation: an origin (or a proxy, or a malformed Range) that
    // answers the whole file. The reader must refuse it, because reading it is
    // exactly the 412 MB download this phase exists to prevent.
    const big = new Uint8Array(5_000_000);
    const { fetchImpl } = fakeOrigin(big, { ignoreRange: true });
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    await expect(src.init()).rejects.toBeInstanceOf(RangeUnsupportedError);
    expect(src.stats.wholeFileRequests).toBe(1);
    // ⚠️ and it counted ZERO bytes: the body was cancelled, not consumed.
    expect(src.stats.bytes).toBe(0);
  });

  it('⚠️ refuses to proceed when the origin will not say how big the object is', async () => {
    const { fetchImpl } = fakeOrigin(body, { etag: '"v1"' });
    const src = createRangeSource({
      url: '/f',
      getAuthHeader: async () => 'Bearer t',
      // A 206 with no Content-Range: nothing to size the central directory from.
      fetchImpl: async (...a) => {
        const res = await fetchImpl(...a);
        const h = new Headers(res.headers);
        h.delete('Content-Range');
        return new Response(await res.arrayBuffer(), { status: res.status, headers: h });
      },
    });
    await expect(src.init()).rejects.toBeInstanceOf(RangeUnsupportedError);
  });

  it('probes exactly once — a second init costs no request', async () => {
    const { fetchImpl, calls } = fakeOrigin(body);
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    await src.init();
    await src.init();
    expect(calls.length).toBe(1);
    expect(src.size).toBe(body.length);
  });
});

/* ── the file changing underneath a half-read archive ───────────────────── */

describe('ETag consistency', () => {
  it('⚠️ stops when the object is replaced mid-read, rather than inflating garbage', async () => {
    // A ZIP central directory is read ONCE. If the object changes, every offset
    // in it points somewhere else and inflate produces nonsense rather than an
    // error — a corrupt page with no explanation anywhere.
    const { fetchImpl } = fakeOrigin(body, { etagPerCall: (n) => (n <= 1 ? '"v1"' : '"v2"') });
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    await src.init();
    await expect(src.read(0, 8)).rejects.toBeInstanceOf(ObjectChangedError);
  });

  it('is untroubled by an origin that sends no ETag at all', async () => {
    const { fetchImpl } = fakeOrigin(body, { etagPerCall: () => undefined });
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    await src.init();
    await expect(src.read(0, 8)).resolves.toHaveLength(8);
  });
});

/* ── refusals keep their status, so the reader can word them ────────────── */

describe('refusals', () => {
  for (const [status, name] of [[401, 'lapsed sign-in'], [403, 'no grant'], [404, 'not on the shelf'], [429, 'paced'], [503, 'misconfigured']]) {
    it(`carries a ${status} (${name}) through with its status and the endpoint's own sentence`, async () => {
      const { fetchImpl } = fakeOrigin(body, { status, detail: `worded ${status}` });
      const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
      await expect(src.init()).rejects.toMatchObject({
        name: 'HttpStatusError', status, detail: `worded ${status}`,
      });
    });
  }

  it('treats a short answer as a failure rather than silently truncating', async () => {
    const { fetchImpl } = fakeOrigin(body);
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    await src.init();
    // The last byte is at 1023; asking for 8 from 1020 can only yield 4.
    await expect(src.read(1020, 8)).rejects.toBeInstanceOf(HttpStatusError);
  });

  it('reads the exact bytes asked for, at the right offsets', async () => {
    const { fetchImpl } = fakeOrigin(body);
    const src = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
    await src.init();
    expect([...(await src.read(100, 4))]).toEqual([...body.slice(100, 104)]);
    expect([...(await src.read(0, 3))]).toEqual([...body.slice(0, 3)]);
  });
});
