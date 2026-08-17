// clubs.js — book club system, Phase 1: create/browse/join/leave clubs + members
// ES module, browser-native (no build step)
// Design: docs/BOOK_CLUBS_DESIGN.md (gitignored; lives on the dev machine)

import {
  collection, doc, getDoc, getDocs, setDoc, deleteDoc, updateDoc,
  query, where, serverTimestamp, runTransaction, arrayUnion,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { slugifyName } from './identity.js';
import { describeActionError } from './permission-ux.js';
import { reportGate } from './gate-shadow.js';

/**
 * Validate a club name. 3-40 chars after trimming.
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateClubName(name) {
  const trimmed = (name || '').trim();
  if (trimmed.length < 3) return { valid: false, error: 'Club name must be at least 3 characters.' };
  if (trimmed.length > 40) return { valid: false, error: 'Club name must be 40 characters or fewer.' };
  return { valid: true };
}

/**
 * Validate a club description. Optional, up to 300 chars.
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateClubDescription(description) {
  if ((description || '').length > 300) {
    return { valid: false, error: 'Description must be 300 characters or fewer.' };
  }
  return { valid: true };
}

/**
 * Create a club. The creator becomes the host and first member.
 *
 * When the creator has a LIVE Firebase session, pass their auth uid as
 * `uid`: the club is then born CLAIMED — managerUids is stamped at create,
 * so rules enforce its manager writes from day one. Without a uid (legacy
 * session) the club is created unclaimed, exactly as before the uid layer.
 *
 * @param {object} db
 * @param {{name: string, description?: string, emoji?: string}} input
 * @param {{displayName: string}} session
 * @param {string|null} [uid] the creator's Firebase Auth uid, when live
 * @returns {Promise<{success: boolean, clubId?: string, error?: string}>}
 */
export async function createClub(db, input, session, uid = null) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to create a club.' };
  }
  const nameCheck = validateClubName(input.name);
  if (!nameCheck.valid) return { success: false, error: nameCheck.error };
  const descCheck = validateClubDescription(input.description);
  if (!descCheck.valid) return { success: false, error: descCheck.error };

  const slug = slugifyName(session.displayName);
  const clubRef = doc(collection(db, col('clubs')));
  try {
    const clubDoc = {
      name: input.name.trim(),
      description: (input.description || '').trim(),
      emoji: input.emoji || '📚',
      avatarReadId: null,
      avatarCoverHref: '',
      joinMode: 'open',
      hostSlug: slug,
      hostDisplayName: session.displayName,
      memberSlugs: [slug],
      invitedSlugs: [],
      memberCount: 1,
      createdAt: serverTimestamp(),
    };
    if (uid) {
      clubDoc.managerUids = {
        [uid]: { role: 'host', displayName: session.displayName, claimedAt: Date.now() },
      };
    }
    await setDoc(clubRef, clubDoc);
    await setDoc(doc(db, col('clubs'), clubRef.id, 'members', slug), {
      displayName: session.displayName,
      role: 'host',
      status: 'active',
      joinedAt: serverTimestamp(),
    });
    return { success: true, clubId: clubRef.id };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * Fetch all clubs — every club is visible and joinable (small, trusted
 * user base; no private clubs or invite codes).
 */
export async function getAllClubs(db) {
  const snap = await getDocs(collection(db, col('clubs')));
  return snap.docs.map(d => ({ id: d.id, ...d.data() })).filter(c => !c.archived);
}

/**
 * Fetch clubs the user belongs to, plus clubs they've been invited to.
 * Invited clubs come first and carry `invited: true` so the UI can pin
 * them with accept/reject buttons.
 */
export async function getMyClubs(db, displayName) {
  const slug = slugifyName(displayName);
  const [invitedSnap, memberSnap] = await Promise.all([
    getDocs(query(collection(db, col('clubs')), where('invitedSlugs', 'array-contains', slug))),
    getDocs(query(collection(db, col('clubs')), where('memberSlugs', 'array-contains', slug))),
  ]);
  const seen = new Set();
  const out = [];
  for (const d of invitedSnap.docs) {
    seen.add(d.id);
    out.push({ id: d.id, ...d.data(), invited: true });
  }
  for (const d of memberSnap.docs) {
    if (!seen.has(d.id)) out.push({ id: d.id, ...d.data(), invited: false });
  }
  return out.filter(c => !c.archived);
}

// ==================== Club feature toggles ====================
//
// Every club feature added from 2026-08 on ships GATED behind a per-club
// toggle that club managers (host/moderator — enforced in the UI, like every
// manager action in this auth-free trust model) control from the Edit Club
// modal. The club doc carries a `features` map; each backlog feature adds ONE
// key here and one checkbox in club.html — no schema migration. A key absent
// from the club doc falls back to FEATURE_DEFAULTS.
//
// readingSchedule defaults OFF: every read has milestones by construction, so
// "on if the club uses milestones" would mean on for everyone. OFF means zero
// UI change for existing clubs until a manager opts in.
export const FEATURE_DEFAULTS = {
  readingSchedule: false,   // due dates on sections + on-track/behind chips
  // Server-side pipeline (app/club_announcements.py) posts schedule changes,
  // due-date nudges and read start/finish to the club's own webhook. OFF by
  // default so no channel hears anything until a manager opts in.
  discordAnnouncements: false,
  // Poll-closed (and future poll-posted) embeds — backlog #2c. A SEPARATE
  // opt-in from discordAnnouncements above: a club can want meeting/due
  // nudges without poll chatter. OFF by default; the engine still checks
  // discordAnnouncements FIRST as the master gate (see
  // app/club_announcements.py feature_enabled/POLL_FEATURE_KEY) — this key
  // only matters once that master toggle is already on.
  discordPollAnnouncements: false,
  // Free-form, chapter-taggable polls (backlog #3). OFF by default like every
  // other opt-in feature; see the "Club polls" section of club-reads.js for
  // the data shape and spoiler-gating logic.
  polls: false,
  // Blind ratings reveal (backlog #4): members rate a read privately, a
  // manager reveals everyone's rating + the average together. OFF by
  // default; see the "Blind ratings" section of club-reads.js for the
  // browser-unreadable subcollection design and its trust-model trade-off.
  blindRatings: false,
  // Meeting scheduler RSVP + .ics download (backlog #5): members respond
  // Going/Maybe/Can't to the club's nextMeetingAt, and anyone can download a
  // client-generated calendar file for it. OFF by default; gates BOTH the
  // RSVP row and the .ics download button (single key covers the whole
  // feature — see the "Meeting RSVP" section of club-reads.js and
  // site/ics.js for the data shape and calendar-file builder).
  meetingRsvp: false,
  // Buddy-read pace graph (backlog #6): a per-member progress-over-time line
  // chart on the read page, plus the schedule's expected-pace line when one
  // is set. OFF by default. See the "Buddy-read pace graph" section of
  // club-reads.js for the append-only progress-history shape (no new
  // subcollection, no rules change) and the pure derivation/scaling helpers
  // club-read.html turns into hand-built SVG.
  paceGraph: false,
};

/** Is a feature enabled for this club? Falls back to FEATURE_DEFAULTS. */
export function clubFeatureEnabled(club, key) {
  const map = club && club.features;
  if (map && typeof map === 'object' && key in map) return !!map[key];
  return !!FEATURE_DEFAULTS[key];
}

// ==================== Manager uid roster (enforced permissions) ====================
//
// Since 2026-08-14 the club doc may carry `managerUids`: a map of Firebase
// Auth uid -> { role: 'host'|'moderator', displayName, claimedAt }, recorded
// BESIDE the display-name roles (hostSlug, members/{slug}.role), which stay
// the presentation layer. firestore.rules enforces manager-only writes
// against this map — the FIRST rules clauses on this site that use
// request.auth. A club with no roster ("unclaimed") behaves exactly as
// before: that is the migration path for legacy clubs, not an oversight.
// Claiming is trust-on-first-use — while unclaimed, the first signed-in
// host/mod stamps their own uid — and the site admin (site_roles/{uid},
// see identity.js getSiteRole) is the repair path if the window is abused.

/**
 * Club-doc fields that rules gate behind the manager roster once a club is
 * claimed, SPLIT by tier. Three field-tiers as of 2026-08-16 (the site-ROLE
 * three-tier model — admin/moderator/club-mod — is a separate concept; see
 * the "Club manager enforcement" comment in firestore.rules):
 *   STRUCTURAL  — canManageClub (roster uid or site admin). The "club
 *     island": joinMode, features — a club mod runs these day to day.
 *   OPERATIONAL — canOperateClub (adds the site moderator).
 *   RESTRICTED  — canAdministerClub (site admin only, NOT the club roster;
 *     2026-08-16 tightening, owner-approved). discordWebhookMask + the
 *     settings/discord subdoc holding the real URL, and managerUids itself.
 *     Rationale, kept here because it is the reasoning a future editor of
 *     this list needs:
 *       - the Discord webhook is an outbound CAPABILITY (whoever sets it
 *         decides where club activity broadcasts) — not delegable to a
 *         per-club mod, same trust boundary as pipeline_requests/site
 *         admin-only deletes elsewhere in this codebase.
 *       - managerUids is the roster mods sit in; letting a mod rewrite it
 *         is peer-escalation (appointing/removing other mods) — the same
 *         class of move the estate ladder (docs/info/ROLES.md) outlaws
 *         with "grant only strictly beneath your own role".
 *     Everything else about running a club's actual reading — the book,
 *     reads, schedule/milestones, finish/abandon, the blind-ratings reveal,
 *     next meeting — stays exactly as available to a club mod as before;
 *     this tier is deliberately narrow.
 * ⚠️ MUST match clubStructuralFieldsChanged() / clubOperationalFieldsChanged()
 * / clubRestrictedFieldsChanged() in firestore.rules — the test suite pins
 * these lists as the contract.
 */
export const STRUCTURAL_CLUB_FIELDS = [
  'joinMode', 'features',
];

export const OPERATIONAL_CLUB_FIELDS = [
  'nextMeetingAt', 'nextMeetingNotes',
];

export const RESTRICTED_CLUB_FIELDS = [
  'discordWebhookMask', 'managerUids',
];

/** The union — every manager-gated club-doc field, whichever tier. */
export const MANAGED_CLUB_FIELDS = [
  ...STRUCTURAL_CLUB_FIELDS, ...OPERATIONAL_CLUB_FIELDS, ...RESTRICTED_CLUB_FIELDS,
];

/**
 * Read-doc fields rules gate the same way, split by tier:
 *   STRUCTURAL — club-level lifecycle (finish/abandon), slot, the
 *     blind-ratings reveal flip: canManageClub only.
 *   OPERATIONAL — the reading schedule: canOperateClub (site moderator too).
 * ⚠️ MUST match readStructuralFieldsChanged() / readOperationalFieldsChanged()
 * in firestore.rules.
 */
export const STRUCTURAL_READ_FIELDS = [
  'status', 'finishedAt', 'slot', 'ratingsRevealed', 'revealedAt',
];

export const OPERATIONAL_READ_FIELDS = [
  'milestones', 'scheduleUpdatedAt',
];

/** The union — every manager-gated read-doc field, whichever tier. */
export const MANAGED_READ_FIELDS = [
  ...STRUCTURAL_READ_FIELDS, ...OPERATIONAL_READ_FIELDS,
];

/** Does this club have a non-empty manager-uid roster? */
export function isClubClaimed(club) {
  const m = club && club.managerUids;
  return !!(m && typeof m === 'object' && Object.keys(m).length > 0);
}

/** Is this uid in the club's manager roster? */
export function isManagerUid(club, uid) {
  const m = club && club.managerUids;
  return !!(uid && m && typeof m === 'object'
            && Object.prototype.hasOwnProperty.call(m, uid));
}

/**
 * Mirror of the firestore.rules STRUCTURAL gate: may this uid perform
 * manager-gated writes on this club? Unclaimed clubs are open (migration
 * path); site admin is the break-glass.
 */
export function canManageClub(club, uid, siteAdmin = false) {
  return !isClubClaimed(club) || !!siteAdmin || isManagerUid(club, uid);
}

/**
 * Mirror of the firestore.rules OPERATIONAL gate (three-tier model):
 * everything canManageClub admits, plus the site MODERATOR — reading
 * schedule, polls management, next-meeting fields, membership ops and
 * content deletes, across every club. `siteRole` is the site_roles doc's
 * role string ('admin' | 'moderator') or null.
 */
export function canOperateClub(club, uid, siteRole = null) {
  return canManageClub(club, uid, siteRole === 'admin') || siteRole === 'moderator';
}

/**
 * Mirror of the firestore.rules RESTRICTED gate (2026-08-16 tightening):
 * the Discord webhook and the manager roster itself (RESTRICTED_CLUB_FIELDS)
 * — site admin ONLY, never the club's own manager roster. Unclaimed clubs
 * stay open (transition safety, same migration path as canManageClub): a
 * club with no roster at all predates the uid layer entirely and behaves as
 * it always has. Deliberately does not take a `uid` — roster membership
 * never satisfies this gate once the club is claimed.
 */
export function canAdministerClub(club, siteAdmin = false) {
  return !isClubClaimed(club) || !!siteAdmin;
}

/**
 * Stamp the caller's uid into the club's manager roster ("secure your
 * role"). On an unclaimed club this is the trust-on-first-use claim and
 * rules allow it for anyone signed in; on a claimed club, managerUids is
 * RESTRICTED (2026-08-16) — only the site admin may write it, not even an
 * existing bound manager — so anyone claiming after the first is directed
 * to ask the site admin, which the caller should surface as guidance, not
 * retry.
 */
export async function claimManagerRole(db, clubId, uid, session, role) {
  if (!uid) {
    return { success: false, error: 'Sign in with Google to secure your role.' };
  }
  const r = role === 'moderator' ? 'moderator' : 'host';
  try {
    await updateDoc(doc(db, col('clubs'), clubId), {
      ['managerUids.' + uid]: {
        role: r,
        displayName: session && session.displayName ? session.displayName : '',
        claimedAt: Date.now(),
      },
    });
    return { success: true };
  } catch (e) {
    if (e && (e.code === 'permission-denied' || /permission/i.test(e.message || ''))) {
      return {
        success: false,
        error: 'This club is already secured. Ask the site admin to add your account.',
      };
    }
    return { success: false, error: describeActionError(e) };
  } finally {
    reportGate('club.claimManager', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

// ==================== Per-club Discord webhook ====================
//
// Clubs paste their OWN webhook URL so reminders/announcements post to a
// channel they own. A webhook URL is a capability — anyone holding it can
// post to that channel — so the full URL never lives on the world-readable
// club doc and is never rendered back after save. It goes in
// clubs/{id}/settings/discord, which rules make UNREADABLE to browsers
// (write-only, same pattern as pipeline_requests); the server-side Discord
// notifier reads it via the service account, which bypasses rules. The
// public club doc keeps only a masked tail (discordWebhookMask) for display.
//
// ⚠️ RESTRICTED (2026-08-16): setting/clearing the webhook — both this
// subdoc and discordWebhookMask on the club doc — is SITE-ADMIN ONLY, not a
// club-mod action, even for a bound host/mod. See RESTRICTED_CLUB_FIELDS
// above for the rationale (outbound capability, not delegable per-club).

const DISCORD_WEBHOOK_RE =
  /^https:\/\/(discord\.com|discordapp\.com)\/api\/webhooks\/\d+\/[\w-]+$/;

/** Client-side shape check for a Discord webhook URL. */
export function isValidDiscordWebhook(url) {
  return DISCORD_WEBHOOK_RE.test((url || '').trim());
}

/** Display-safe mask: last 4 characters only (e.g. "…f3Kq"). */
export function maskWebhookUrl(url) {
  const u = (url || '').trim();
  return u ? `…${u.slice(-4)}` : '';
}

/**
 * Save the club's Discord webhook (site-admin action, enforced in the UI
 * and in firestore.rules — RESTRICTED_CLUB_FIELDS, 2026-08-16).
 * Full URL -> write-only settings subdoc; masked tail -> club doc.
 */
export async function setClubDiscordWebhook(db, clubId, url, session) {
  const trimmed = (url || '').trim();
  if (!isValidDiscordWebhook(trimmed)) {
    return { success: false, error: 'That does not look like a Discord webhook URL (https://discord.com/api/webhooks/...).' };
  }
  try {
    await setDoc(doc(db, col('clubs'), clubId, 'settings', 'discord'), {
      webhookUrl: trimmed,
      updatedBy: session && session.displayName ? session.displayName : '',
      updatedAt: serverTimestamp(),
    });
    await updateDoc(doc(db, col('clubs'), clubId), {
      discordWebhookMask: maskWebhookUrl(trimmed),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the site admin role' }) };
  } finally {
    reportGate('club.setWebhook', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/**
 * Remove the club's Discord webhook (site-admin action, enforced in the UI
 * and in firestore.rules — RESTRICTED_CLUB_FIELDS, 2026-08-16).
 */
export async function clearClubDiscordWebhook(db, clubId) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'settings', 'discord'));
    await updateDoc(doc(db, col('clubs'), clubId), { discordWebhookMask: '' });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the site admin role' }) };
  } finally {
    reportGate('club.clearWebhook', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/**
 * Update club details. Any member may edit name/description/emoji;
 * joinMode ('open' | 'application') and the features map are manager
 * settings (enforced in the UI).
 */
export async function updateClubDetails(db, clubId, input) {
  const updates = {};
  if (input.name !== undefined) {
    const check = validateClubName(input.name);
    if (!check.valid) return { success: false, error: check.error };
    updates.name = input.name.trim();
  }
  if (input.description !== undefined) {
    const check = validateClubDescription(input.description);
    if (!check.valid) return { success: false, error: check.error };
    updates.description = input.description.trim();
  }
  if (input.emoji !== undefined) updates.emoji = input.emoji.trim() || '📚';
  if (input.avatarReadId !== undefined) updates.avatarReadId = input.avatarReadId;
  if (input.avatarCoverHref !== undefined) updates.avatarCoverHref = input.avatarCoverHref;
  if (input.promptsEnabled !== undefined) updates.promptsEnabled = !!input.promptsEnabled;
  if (input.joinMode !== undefined) {
    if (!['open', 'application'].includes(input.joinMode)) {
      return { success: false, error: 'Invalid join mode.' };
    }
    updates.joinMode = input.joinMode;
  }
  // Optional next meeting: millis epoch (null clears), free-form notes.
  if (input.nextMeetingAt !== undefined) {
    if (input.nextMeetingAt !== null && !Number.isFinite(input.nextMeetingAt)) {
      return { success: false, error: 'Invalid meeting time.' };
    }
    updates.nextMeetingAt = input.nextMeetingAt;
  }
  if (input.nextMeetingNotes !== undefined) {
    const notes = (input.nextMeetingNotes || '').trim();
    if (notes.length > 500) {
      return { success: false, error: 'Meeting notes must be 500 characters or less.' };
    }
    updates.nextMeetingNotes = notes;
  }
  // Feature toggles: a full map of boolean flags (callers pass current map
  // with the toggled keys merged in). Unknown keys are dropped so a stale
  // client can't stuff arbitrary data under `features`.
  if (input.features !== undefined) {
    if (!input.features || typeof input.features !== 'object') {
      return { success: false, error: 'Invalid features map.' };
    }
    const cleaned = {};
    for (const key of Object.keys(FEATURE_DEFAULTS)) {
      if (key in input.features) cleaned[key] = !!input.features[key];
    }
    updates.features = cleaned;
  }
  try {
    await updateDoc(doc(db, col('clubs'), clubId), updates);
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role for that setting' }) };
  } finally {
    // Phase 1 shadow (fire-and-forget): one report per gated TIER the update
    // touched, in the worker's vocabulary. Member-editable fields
    // (name/description/emoji/...) report nothing — they stay browser-direct.
    if (STRUCTURAL_CLUB_FIELDS.some((f) => f in updates)) {
      reportGate('club.updateStructural', { clubId });
    }
    if (OPERATIONAL_CLUB_FIELDS.some((f) => f in updates)) {
      reportGate('club.setNextMeeting', { clubId });
    }
  }
}

/**
 * Fetch a single club by id. Returns null if it doesn't exist.
 */
export async function getClub(db, clubId) {
  const snap = await getDoc(doc(db, col('clubs'), clubId));
  return snap.exists() ? { id: snap.id, ...snap.data() } : null;
}

/**
 * Fetch a club's members.
 */
export async function getMembers(db, clubId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'members'));
  return snap.docs.map(d => ({ slug: d.id, ...d.data() }));
}

/**
 * Record that a member dismissed the "rate this book" nudge for a finished
 * read, so it stays dismissed across devices. Stored on the member doc; the
 * validClubMember rule allows extra fields (only displayName + role are
 * validated), so no rules change is needed.
 */
export async function dismissRateNudge(db, clubId, slug, readId) {
  try {
    const ref = doc(db, col('clubs'), clubId, 'members', slug);
    await updateDoc(ref, { dismissedRateNudges: arrayUnion(readId) });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * Join a club. Idempotent — joining a club you're in succeeds without change.
 * Transactional so memberCount always matches memberSlugs.
 */
export async function joinClub(db, clubId, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to join a club.' };
  }
  const slug = slugifyName(session.displayName);
  const clubRef = doc(db, col('clubs'), clubId);
  try {
    const alreadyMember = await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const data = clubSnap.data();
      const slugs = data.memberSlugs || [];
      if (slugs.includes(slug)) return true;
      tx.update(clubRef, { memberSlugs: [...slugs, slug], memberCount: slugs.length + 1 });
      return false;
    });
    if (!alreadyMember) {
      await setDoc(doc(db, col('clubs'), clubId, 'members', slug), {
        displayName: session.displayName,
        role: 'member',
        status: 'active',
        joinedAt: serverTimestamp(),
      });
    }
    return { success: true, clubId };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * Leave a club — anyone can, including the host. If the host leaves,
 * the next member alphabetically becomes host. If the last member
 * leaves, the club is archived in place (hidden from lists, fully
 * recoverable from the database).
 */
export async function leaveClub(db, clubId, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Not signed in.' };
  }
  const slug = slugifyName(session.displayName);
  const clubRef = doc(db, col('clubs'), clubId);
  try {
    let outcome = 'left';
    await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const data = clubSnap.data();
      const slugs = (data.memberSlugs || []).filter(s => s !== slug);
      const invited = (data.invitedSlugs || []).filter(s => s !== slug);
      const updates = { memberSlugs: slugs, invitedSlugs: invited, memberCount: slugs.length };

      let newHostRef = null;
      let newHostData = null;
      if (data.hostSlug === slug) {
        if (slugs.length === 0) {
          updates.archived = true;
          updates.archivedAt = serverTimestamp();
          outcome = 'archived';
        } else {
          const newHostSlug = [...slugs].sort()[0];
          newHostRef = doc(db, col('clubs'), clubId, 'members', newHostSlug);
          const snap = await tx.get(newHostRef); // reads before writes
          newHostData = snap.exists() ? snap.data() : { displayName: newHostSlug };
          updates.hostSlug = newHostSlug;
          updates.hostDisplayName = newHostData.displayName || newHostSlug;
          outcome = 'transferred';
        }
      }
      tx.update(clubRef, updates);
      if (newHostRef) {
        tx.set(newHostRef, { ...newHostData, role: 'host' });
      }
    });
    await deleteDoc(doc(db, col('clubs'), clubId, 'members', slug));
    return { success: true, outcome };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * Remove a member from a club (host/moderator action, or self-leave).
 * Refuses to remove the host.
 */
export async function removeMemberBySlug(db, clubId, targetSlug) {
  const clubRef = doc(db, col('clubs'), clubId);
  try {
    await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const data = clubSnap.data();
      if (data.hostSlug === targetSlug) {
        throw new Error('The host cannot be removed.');
      }
      const slugs = (data.memberSlugs || []).filter(s => s !== targetSlug);
      const invited = (data.invitedSlugs || []).filter(s => s !== targetSlug);
      tx.update(clubRef, { memberSlugs: slugs, invitedSlugs: invited, memberCount: slugs.length });
    });
    await deleteDoc(doc(db, col('clubs'), clubId, 'members', targetSlug));
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role' }) };
  } finally {
    reportGate('club.removeMember', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/**
 * Set a member's role ('moderator' or 'member'). Host-only action
 * (enforced in the UI); the host's own role cannot be changed.
 */
export async function setMemberRole(db, clubId, targetSlug, role) {
  if (role !== 'moderator' && role !== 'member') {
    return { success: false, error: 'Invalid role.' };
  }
  try {
    const club = await getClub(db, clubId);
    if (!club) return { success: false, error: 'Club not found.' };
    if (club.hostSlug === targetSlug) {
      return { success: false, error: "The host's role cannot be changed." };
    }
    const memberRef = doc(db, col('clubs'), clubId, 'members', targetSlug);
    const memberSnap = await getDoc(memberRef);
    if (!memberSnap.exists()) return { success: false, error: 'Member not found.' };
    await setDoc(memberRef, { ...memberSnap.data(), role });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host role' }) };
  } finally {
    reportGate('club.setMemberRole', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

// ==================== Join requests (application mode) ====================

/** Ask to join a club whose joinMode is 'application'. Idempotent. */
export async function requestToJoin(db, clubId, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to request to join.' };
  }
  try {
    const slug = slugifyName(session.displayName);
    await setDoc(doc(db, col('clubs'), clubId, 'requests', slug), {
      displayName: session.displayName,
      requestedAt: serverTimestamp(),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/** Pending join requests for a club. */
export async function getRequests(db, clubId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'requests'));
  return snap.docs.map(d => ({ slug: d.id, ...d.data() }));
}

/** Accept a join request: the requester becomes an active member. */
export async function acceptRequest(db, clubId, targetSlug) {
  try {
    const reqRef = doc(db, col('clubs'), clubId, 'requests', targetSlug);
    const reqSnap = await getDoc(reqRef);
    if (!reqSnap.exists()) return { success: false, error: 'Request not found.' };
    const displayName = reqSnap.data().displayName;

    const clubRef = doc(db, col('clubs'), clubId);
    await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const slugs = clubSnap.data().memberSlugs || [];
      if (!slugs.includes(targetSlug)) {
        tx.update(clubRef, { memberSlugs: [...slugs, targetSlug], memberCount: slugs.length + 1 });
      }
    });
    await setDoc(doc(db, col('clubs'), clubId, 'members', targetSlug), {
      displayName,
      role: 'member',
      status: 'active',
      joinedAt: serverTimestamp(),
    });
    await deleteDoc(reqRef);
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role' }) };
  } finally {
    reportGate('club.acceptRequest', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/** Reject (delete) a join request. */
export async function rejectRequest(db, clubId, targetSlug) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'requests', targetSlug));
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role' }) };
  } finally {
    reportGate('club.rejectRequest', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

// ==================== Invitations (manual add) ====================

/**
 * Manually add a user by display name. They land in an 'invited' state:
 * the club pins to the top of their My Clubs with accept/reject buttons.
 */
export async function inviteMember(db, clubId, displayName) {
  const name = (displayName || '').trim();
  if (name.length < 2) return { success: false, error: 'Enter a display name.' };
  const slug = slugifyName(name);
  const clubRef = doc(db, col('clubs'), clubId);
  try {
    await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const data = clubSnap.data();
      if ((data.memberSlugs || []).includes(slug)) throw new Error(`${name} is already a member.`);
      if ((data.invitedSlugs || []).includes(slug)) throw new Error(`${name} has already been invited.`);
      tx.update(clubRef, { invitedSlugs: [...(data.invitedSlugs || []), slug] });
      tx.set(doc(db, col('clubs'), clubId, 'members', slug), {
        displayName: name,
        role: 'member',
        status: 'invited',
        invitedAt: serverTimestamp(),
      });
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role' }) };
  } finally {
    reportGate('club.inviteMember', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/** Accept an invitation: invited -> active member. */
export async function acceptInvite(db, clubId, session) {
  if (!session || !session.displayName) return { success: false, error: 'Not signed in.' };
  const slug = slugifyName(session.displayName);
  const clubRef = doc(db, col('clubs'), clubId);
  try {
    await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const data = clubSnap.data();
      if (!(data.invitedSlugs || []).includes(slug)) throw new Error('No pending invitation.');
      const invited = data.invitedSlugs.filter(s => s !== slug);
      const slugs = (data.memberSlugs || []).includes(slug)
        ? data.memberSlugs
        : [...(data.memberSlugs || []), slug];
      tx.update(clubRef, { invitedSlugs: invited, memberSlugs: slugs, memberCount: slugs.length });
      tx.set(doc(db, col('clubs'), clubId, 'members', slug), {
        displayName: session.displayName,
        role: 'member',
        status: 'active',
        joinedAt: serverTimestamp(),
      });
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/** Decline an invitation: removed entirely. */
export async function declineInvite(db, clubId, session) {
  if (!session || !session.displayName) return { success: false, error: 'Not signed in.' };
  const slug = slugifyName(session.displayName);
  const clubRef = doc(db, col('clubs'), clubId);
  try {
    await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const data = clubSnap.data();
      tx.update(clubRef, { invitedSlugs: (data.invitedSlugs || []).filter(s => s !== slug) });
    });
    await deleteDoc(doc(db, col('clubs'), clubId, 'members', slug));
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * Delete a club and its member docs. Host-only action (enforced in the UI).
 */
export async function deleteClub(db, clubId) {
  try {
    const membersSnap = await getDocs(collection(db, col('clubs'), clubId, 'members'));
    for (const m of membersSnap.docs) {
      await deleteDoc(doc(db, col('clubs'), clubId, 'members', m.id));
    }
    await deleteDoc(doc(db, col('clubs'), clubId));
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host role' }) };
  } finally {
    reportGate('club.delete', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}
