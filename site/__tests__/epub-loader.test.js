/**
 * The range loader against a REAL ZIP — viewer phase 2.
 *
 * `epub-range.test.js` proves the transport in isolation. This proves the join:
 * that the vendored zip.js accepts our `Reader` subclass, that it really does
 * read a ZIP's central directory from the END of the archive rather than
 * downloading it, and that pulling one small entry out of a multi-megabyte
 * archive costs kilobytes.
 *
 * ⚠️ The archive here is built at test time and is DELIBERATELY MOSTLY
 * INCOMPRESSIBLE PADDING, because a test whose "large" entry deflates to
 * nothing proves nothing about range reading.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import { createRangeSource } from '../epub-range.js';
import { createGatedZipLoader } from '../epub-loader.js';

// ⚠️ The WRITER build, for the test only. The reader ships
// `zip-no-worker-inflate.js`, which has no deflate codec and cannot write a ZIP
// at all — which is the right shape for a reader and the wrong one for a
// fixture factory.
import * as zipRW from '../static/zipjs/zip-no-worker.js';
import * as zipShipped from '../static/zipjs/zip-no-worker-inflate.js';

const PAD = 4 * 1024 * 1024;
let archive;

beforeAll(async () => {
  zipRW.configure({ useWebWorkers: false });
  const out = new zipRW.Uint8ArrayWriter();
  const w = new zipRW.ZipWriter(out);
  await w.add('mimetype', new zipRW.TextReader('application/epub+zip'), { level: 0 });
  await w.add('META-INF/container.xml', new zipRW.TextReader('<container/>'));
  // Incompressible padding: a pseudo-random byte stream, stored not deflated.
  const pad = new Uint8Array(PAD);
  let x = 123456789;
  for (let i = 0; i < PAD; i++) { x = (x * 1103515245 + 12345) & 0x7fffffff; pad[i] = x & 0xff; }
  await w.add('OEBPS/big.bin', new zipRW.Uint8ArrayReader(pad), { level: 0 });
  await w.add('OEBPS/content.opf', new zipRW.TextReader('<package>the opf</package>'));
  archive = await w.close();
});

/** A counting origin honouring single `bytes=A-B` spans, like phase 1a. */
function origin(bytes) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    const headers = init?.headers ?? {};
    calls.push(headers.Range);
    const m = /^bytes=(\d+)-(\d+)$/.exec(headers.Range ?? '');
    if (!m) {
      return new Response(bytes, { status: 200, headers: { 'Content-Length': String(bytes.length) } });
    }
    const start = Number(m[1]);
    const end = Math.min(Number(m[2]), bytes.length - 1);
    const slice = bytes.slice(start, end + 1);
    return new Response(slice, {
      status: 206,
      headers: {
        'Content-Range': `bytes ${start}-${end}/${bytes.length}`,
        'Content-Length': String(slice.length),
        ETag: '"fixture"',
      },
    });
  };
  return { fetchImpl, calls };
}

async function openLoader(bytes) {
  const { fetchImpl, calls } = origin(bytes);
  const source = createRangeSource({ url: '/f', getAuthHeader: async () => 'Bearer t', fetchImpl });
  await source.init();
  const loader = await createGatedZipLoader({ zip: zipShipped, source })();
  return { loader, source, calls };
}

describe('the vendored zip.js, driven over ranges', () => {
  it('the fixture really is big and really is incompressible', () => {
    expect(archive.length).toBeGreaterThan(PAD);
  });

  it('⚠️ lists the entries without reading the archive', async () => {
    const { loader, source } = await openLoader(archive);
    expect(loader.entries.map((e) => e.filename)).toEqual([
      'mimetype', 'META-INF/container.xml', 'OEBPS/big.bin', 'OEBPS/content.opf',
    ]);
    // THE NUMBER. Opening a 4 MB archive costs single-digit KB, because a ZIP's
    // central directory lives at the END and this reads it there.
    expect(source.stats.bytes).toBeLessThan(4096);
    expect(source.stats.wholeFileRequests).toBe(0);
  });

  it('⚠️ reads the small entries and STILL never touches the 4 MB one', async () => {
    const { loader, source } = await openLoader(archive);
    expect(await loader.loadText('META-INF/container.xml')).toBe('<container/>');
    expect(await loader.loadText('OEBPS/content.opf')).toBe('<package>the opf</package>');
    expect(loader.getSize('OEBPS/big.bin')).toBe(PAD);

    // This is the assertion the whole phase rests on: the reader learned the
    // archive's shape, read two entries and the size of a third, and the bytes
    // it fetched are a rounding error against the file.
    expect(source.stats.bytes).toBeLessThan(8192);
    expect(source.stats.bytes / archive.length).toBeLessThan(0.002);
    expect(source.stats.wholeFileRequests).toBe(0);
  });

  it('the classic ZIP-from-the-end pattern is visible in the requests', async () => {
    const { calls } = await openLoader(archive);
    expect(calls[0]).toBe('bytes=0-0');            // size probe
    const starts = calls.slice(1, 3).map((r) => Number(/bytes=(\d+)-/.exec(r)[1]));
    // The next two reads are the end-of-central-directory record and the
    // central directory itself, both near the tail.
    for (const s of starts) expect(s).toBeGreaterThan(archive.length * 0.9);
  });

  it('answers null for an absent entry rather than throwing', async () => {
    // ⚠️ foliate's epub.js PROBES for optional files (Apple display options,
    // calibre bookmarks) and treats a throw as a broken book.
    const { loader } = await openLoader(archive);
    expect(await loader.loadText('META-INF/com.apple.ibooks.display-options.xml')).toBeNull();
    expect(await loader.loadBlob('nope.png', 'image/png')).toBeNull();
    expect(loader.getSize('nope.png')).toBe(0);
  });

  it('reads the big entry correctly WHEN ASKED — range reading is not truncation', async () => {
    const { loader, source } = await openLoader(archive);
    const blob = await loader.loadBlob('OEBPS/big.bin', 'application/octet-stream');
    expect(blob.size).toBe(PAD);
    // And now, of course, it did pay for it. The point was never that bytes are
    // free — it is that you pay for what you read.
    expect(source.stats.bytes).toBeGreaterThan(PAD);
  });

  it('⚠️ the SHIPPED bundle cannot write a ZIP, and that is deliberate', () => {
    // `-inflate` is read-only. If someone swaps the entry point for a full
    // build "for consistency", this goes red and they have to say why.
    expect(zipShipped.ZipWriter).toBeUndefined();
    expect(zipShipped.Reader).toBeTypeOf('function');
    expect(zipShipped.ZipReader).toBeTypeOf('function');
    expect(zipShipped.TextWriter).toBeTypeOf('function');
    expect(zipShipped.BlobWriter).toBeTypeOf('function');
  });
});
