// audio-request.js — the on-demand audiobook ingest request
// ES module, browser-native (no build step)
//
// AUDIO PLAYER PHASE 1, 2026-08-18. ⚠️ THERE IS NO PLAYER IN THIS PHASE and
// this module must not grow one. Phase 2 owns `<audio>`, the chapter-relative
// scrub bar, Media Session and the service-worker bearer seam. What ships here
// is the honest ladder that sits where the player will go:
//
//   not streamable yet — request it
//        -> requested — the library machine uploads it on its next run
//        -> ready to stream (player coming)
//
// Design of record: catalog-platform/docs/info/audio-player-design.md §12
// decision 3 (on-demand ingest, the duplicate clause, the eviction clause).
// The queue's as-built contract: docs/info/audio-ingest.md §3.
//
// ⚠️ WHY THERE IS A QUEUE AT ALL. The library is 630 GB across 1,073 files
// (MEASURED 2026-08-17). Owner: *"upon clicking the download button it adds it
// to a queue to be downloaded for everyone. so each book is on request then
// ready for everyone."* Nothing is uploaded until somebody asks, so an absent
// audiobook is a book nobody wanted yet — never a lost one.
//
// ⚠️ AND THE WAIT IS HOURS, NOT MINUTES, AND THE COPY SAYS SO. The design's
// first draft promised "usually ready within the hour". The pipeline that
// fulfils this queue runs every EIGHT HOURS (sync step 5.9), so that sentence
// would have been a promise the machine cannot keep. A person who is told
// "within the hour" and waits three concludes the button is broken.

import {
  doc, getDoc, setDoc, updateDoc, arrayUnion,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { bookIdFromTitle } from './reviews.js';
import { describeActionError } from './permission-ux.js';
import { getLiveUser, getIdToken } from './identity.js';

/**
 * What is streamable right now — a PROJECTION, never the manifest.
 *
 * ⚠️ The answer carries `bookId`, `anchor`, `title`, `sizeBytes`, `since` and
 * deliberately NOT `path`. The library's file paths are the scraping surface
 * `site/audio_manifest.json` is gitignored to close; see the Worker's
 * `src/audio-status.ts`. Nothing on this page needs a path — the byte route
 * resolves `anchor -> path` server-side, which is the whole point of an anchor.
 */
const AUDIO_STATUS_URL = 'https://audiobook-api.heygabi.ai/api/audio/status';

/**
 * How long one page keeps the streamable list before re-asking.
 *
 * Sized against the ONLY thing that can change it: an 8-hourly pipeline run.
 * Re-fetching per modal open would be dozens of gated round trips to learn the
 * same answer. Five minutes means a book that lands mid-session shows up
 * without a reload, and costs at most one request per five minutes of browsing.
 */
const STATUS_TTL_MS = 5 * 60 * 1000;

/** @type {{at: number, value: object}|null} */
let _statusCache = null;
/** @type {Promise<object>|null} */
let _statusInFlight = null;

/** Tests / sign-out — drop the per-page cache. */
export function resetAudioStatusCache() {
  _statusCache = null;
  _statusInFlight = null;
}

/**
 * The streamable set for this signed-in person, or a REASON.
 *
 * ⚠️ Never throws and never guesses. The four outcomes are kept apart because
 * they need four different UIs, and collapsing them is how a person ends up
 * asking for access they already hold (the estate's standing refusal rule):
 *
 *   {ok:true,  bookIds:Set, player:'phase2'}   the audio row renders
 *   {ok:false, reason:'signed_out'}            render nothing, quietly
 *   {ok:false, reason:'no_grant', detail}      render nothing — listening is a
 *                                              grant this account lacks
 *   {ok:false, reason:'unavailable', detail}   an OUTAGE, not a verdict; the
 *                                              row says so rather than
 *                                              claiming the book is missing
 *
 * @param {object} app the initialised Firebase app
 * @returns {Promise<{ok: boolean, bookIds?: Set<string>, player?: string, reason?: string, detail?: string}>}
 */
export async function getAudioStatus(app) {
  if (_statusCache && Date.now() - _statusCache.at < STATUS_TTL_MS) return _statusCache.value;
  if (_statusInFlight) return _statusInFlight;

  _statusInFlight = (async () => {
    let value;
    try {
      // ⚠️ `identity.getIdToken(app)`, NEVER `user.getIdToken()`. identity.js's
      // session snapshot is a plain object with no such method, and phase 1b of
      // the ebook reader shipped exactly that TypeError for every signed-in
      // person. reader.js's header §5 records it; this is the same rule.
      const token = await getIdToken(app);
      if (!token) {
        value = { ok: false, reason: 'signed_out' };
      } else {
        const res = await fetch(AUDIO_STATUS_URL, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (res.ok) {
          const body = await res.json();
          const bookIds = new Set(
            (body.books || []).map((b) => b && b.bookId).filter(Boolean),
          );
          // Phase 2: also store bookId → anchor map for the player.
          const anchors = new Map();
          (body.books || []).forEach((b) => {
            if (b && b.bookId && b.anchor) anchors.set(b.bookId, b.anchor);
          });
          value = { ok: true, bookIds, anchors, player: body.player || null };
        } else if (res.status === 401) {
          value = { ok: false, reason: 'signed_out' };
        } else if (res.status === 403) {
          // The Worker's own worded sentence — never re-written here. It is the
          // one that tells the person which grant to ask for.
          const body = await res.json().catch(() => ({}));
          value = { ok: false, reason: 'no_grant', detail: body.detail || '' };
        } else {
          // ⚠️ 503 `manifest_absent` lands here and it is NOT a permission
          // fact — it is "nobody has requested an audiobook yet", which is the
          // expected answer on day one. Treated as unavailable rather than as
          // a refusal, so nothing tells anyone to go and ask for access.
          const body = await res.json().catch(() => ({}));
          value = { ok: false, reason: 'unavailable', detail: body.detail || '' };
        }
      }
    } catch (e) {
      // Offline, CORS, DNS. ⚠️ An outage is not a verdict — say "could not
      // check", never "you do not have access".
      value = { ok: false, reason: 'unavailable', detail: '' };
    }
    _statusCache = { at: Date.now(), value };
    _statusInFlight = null;
    return value;
  })();
  return _statusInFlight;
}

/** Is this book streamable right now? Convenience over {@link getAudioStatus}. */
export function isStreamable(status, bookTitle) {
  return !!(status && status.ok && status.bookIds.has(bookIdFromTitle(bookTitle)));
}

/** Get the anchor for a streamable book. Returns null if not found. */
export function getAnchorForBook(status, bookTitle) {
  if (!status || !status.ok || !status.anchors) return null;
  return status.anchors.get(bookIdFromTitle(bookTitle)) || null;
}

/**
 * The pending request pile for a book, or null.
 *
 * `read` on `audio_requests` is open by rule and LOAD-BEARING (the fulfiller
 * lists the collection with the public web API key, gated exactly like a
 * browser). What a reader learns is a book title — already public in
 * `catalog.csv` and `chapters.json` by owner decision — plus opaque uids.
 *
 * @returns {Promise<{requesters: string[], bookTitle: string}|null>}
 */
export async function getAudioRequest(db, bookTitle) {
  try {
    const snap = await getDoc(doc(db, col('audio_requests'), bookIdFromTitle(bookTitle)));
    if (!snap.exists()) return null;
    const data = snap.data() || {};
    return { requesters: Array.isArray(data.requesters) ? data.requesters : [], bookTitle: data.bookTitle || bookTitle };
  } catch (e) {
    return null; // an unreadable pile is "no pile" for UI purposes, never an error page
  }
}

/**
 * Ask for a book to be uploaded — create the pile, or JOIN it.
 *
 * ⚠️ THE DUPLICATE CLAUSE IS THE DOCUMENT ID (owner: *"my book club reads a lot
 * of the same books so 3 of us might request the same book … add a duplicate
 * clause"*). One doc per book: a second press unions one uid onto `requesters`
 * rather than opening a second row, so a book-club pile can never upload the
 * same 600 MB twice.
 *
 * ⚠️ CREATE AND JOIN ARE DIFFERENT WRITES, AND THEY MUST BE. The deployed
 * rules refuse a `create` whose `requesters` is not EXACTLY `[me]`
 * (`audioRequestIsNewPile`), and refuse an `update` that removes anyone or
 * adds anyone but me (`audioRequestJoinsPile` — `hasAll` and `hasOnly`, a pair,
 * neither redundant). So a single blind `setDoc` cannot serve both: on an
 * existing pile Firestore evaluates `setDoc` as an UPDATE, and a create-shaped
 * body would try to overwrite everyone else's uid and be denied. Read first,
 * then write the right shape.
 *
 * ⚠️ AND THE READ CAN LOSE A RACE — that is what the retry is for. Two
 * club-mates pressing within the same second both see "no pile", both attempt
 * a create, and the loser's write is evaluated as an update and refused. That
 * refusal is not a permission problem, it is a lost race, and retrying it as a
 * join is the correct and only fix. Without this, the second person's press
 * looks like a dead button — which is the exact failure the estate's refusal
 * rule forbids.
 *
 * @returns {Promise<{success: boolean, state?: 'requested', requesters?: number, error?: string}>}
 */
export async function requestAudio(db, app, bookTitle) {
  const bookId = bookIdFromTitle(bookTitle);
  if (!bookId) return { success: false, error: 'That book has no usable title, so it cannot be requested. Tell Mitch.' };

  // The ENFORCED identity, not the localStorage mirror: the rules compare
  // `requestedBy` and every entry of `requesters` against `request.auth.uid`,
  // so a session that cannot prove a uid cannot make this write at all — and
  // it is far better to say so than to let it fail as a PERMISSION_DENIED.
  const user = await getLiveUser(app).catch(() => null);
  const uid = user && user.uid ? user.uid : null;
  if (!uid) {
    return {
      success: false,
      error: 'Sign in with Google to request an audiobook — a request has to be tied to an account so we can tell you when it is ready.',
    };
  }

  const ref = doc(db, col('audio_requests'), bookId);
  const now = Date.now();

  const join = async () => {
    // ⚠️ `arrayUnion`, not a read-modify-write of the array. Two people joining
    // at once with a client-side splice would each write a list missing the
    // other, and `hasAll(old)` would refuse the loser — arrayUnion merges
    // server-side and is idempotent, so a re-press by somebody already in the
    // pile is a no-op both rule clauses accept.
    //
    // ⚠️ Only `requesters` and `updatedAt` are sent. `bookId`, `bookTitle` and
    // `requestedBy` are FROZEN by rule: the fulfiller uploads whatever
    // `bookTitle` says, so an editable title on a shared pile would let one
    // requester redirect everyone else's request at a different 600 MB file.
    await updateDoc(ref, { requesters: arrayUnion(uid), updatedAt: now });
  };

  try {
    const snap = await getDoc(ref);
    if (snap.exists()) {
      const existing = (snap.data() || {}).requesters || [];
      if (!existing.includes(uid)) await join();
      return { success: true, state: 'requested', requesters: existing.includes(uid) ? existing.length : existing.length + 1 };
    }
    await setDoc(ref, {
      bookId,
      bookTitle,
      requestedBy: uid,
      // ⚠️ EXACTLY [uid]. `audioRequestIsNewPile` asserts list EQUALITY, not
      // `hasAll([uid])` — the weaker form let one person open a pile that
      // already named a stranger, which poisoned it so the real second
      // requester's legitimate join was then refused (the live smoke test
      // caught it within ten minutes of the first rules deploy).
      requesters: [uid],
      status: 'pending',
      createdAt: now,
      updatedAt: now,
    });
    return { success: true, state: 'requested', requesters: 1 };
  } catch (e) {
    // The lost-create race — see the doc comment. One retry as a join, and if
    // THAT fails the error is real and gets worded.
    try {
      await join();
      return { success: true, state: 'requested' };
    } catch (e2) {
      return { success: false, error: describeActionError(e2) };
    }
  }
}
