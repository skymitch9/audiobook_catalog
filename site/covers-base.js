// covers-base.js - where cover images live. GENERATED, do not edit.
//
// Written by app/writers.py from app/config.py COVERS_BASE_URL, which is the
// single source of truth. Covers are served from Cloudflare R2, not from this
// site, so anything that reads catalog.csv has to resolve the relative
// `cover_href` through coverUrl() before putting it in an <img src>.
//
// site/index.html does NOT import this - its cover URLs are already absolute,
// baked in at build time by app/web/html_builder.py cover_src().

export const COVERS_BASE_URL = 'https://pub-7ab0a1938250448aa329ca218db15a68.r2.dev/';

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
  const rel = cover.startsWith('covers/') ? cover.slice('covers/'.length) : cover;
  if (!COVERS_BASE_URL) return cover;
  // Match Python's urllib.parse.quote(safe='/') exactly, so a cover has ONE
  // canonical URL whichever side emitted it. encodeURIComponent leaves
  // !'()* alone; quote() percent-encodes them.
  const enc = (s) => encodeURIComponent(s).replace(
    /[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());
  const encoded = rel.replace(/^\/+/, '').split('/').map(enc).join('/');
  return COVERS_BASE_URL.replace(/\/+$/, '') + '/' + encoded;
}
