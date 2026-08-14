// covers-base.js - where cover images live. GENERATED, do not edit.
//
// Written by app/writers.py from app/config.py COVERS_BASE_URL, which is the
// single source of truth. Covers are served from Cloudflare R2, not from this
// site, so anything that reads catalog.csv has to resolve the relative
// `cover_href` through coverUrl() before putting it in an <img src>.
//
// site/index.html does NOT import this - its cover URLs are already absolute,
// baked in at build time by app/web/html_builder.py cover_src().

export const COVERS_BASE_URL = 'https://covers.heygabi.ai/';

/**
 * Resolve a catalog.csv `cover_href` to a fetchable URL.
 * "covers/A. American/Home.jpg" -> "<base>/A.%20American/Home.jpg"
 * Absolute hrefs (already-resolved, or historic values stored in Firestore)
 * pass through untouched.
 * @param {string} href
 * @returns {string} '' when there is no cover
 */
export function coverUrl(href) {
  const cover = (href || '').trim();
  if (!cover) return '';
  if (/^(https?:)?\/\//.test(cover) || cover.startsWith('data:')) return cover;
  let rel = cover.startsWith('covers/') ? cover.slice('covers/'.length) : cover;
  // Historic Firestore hrefs arrive in BOTH forms: raw ('J.R. Mathews/…') and
  // already percent-encoded ('J.R.%20Mathews/…'), written by different eras of
  // the profile code. Encoding an encoded value double-encodes it (%20 -> %2520)
  // and the CDN answers 503 — measured live 2026-08-13 as exactly half the
  // community covers failing. Canonicalise: if it LOOKS encoded, decode first;
  // a raw value that merely contains '%' fails the decode and stays as-is.
  if (/%[0-9A-Fa-f]{2}/.test(rel)) { try { rel = decodeURIComponent(rel); } catch { /* literal %, leave raw */ } }
  if (!COVERS_BASE_URL) return cover;
  // Match Python's urllib.parse.quote(safe='/') exactly, so a cover has ONE
  // canonical URL whichever side emitted it. encodeURIComponent leaves
  // !'()* alone; quote() percent-encodes them.
  const enc = (s) => encodeURIComponent(s).replace(
    /[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());
  const encoded = rel.replace(/^\/+/, '').split('/').map(enc).join('/');
  return COVERS_BASE_URL.replace(/\/+$/, '') + '/' + encoded;
}
