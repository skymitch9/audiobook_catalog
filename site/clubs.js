// clubs.js — book club system, Phase 1: create/browse/join/leave clubs + members
// ES module, browser-native (no build step)
// Design: docs/BOOK_CLUBS_DESIGN.md (gitignored; lives on the dev machine)

import {
  collection, doc, getDoc, getDocs, setDoc, deleteDoc, updateDoc,
  query, where, serverTimestamp, runTransaction, arrayUnion,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { slugifyName } from './identity.js';

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
 * @param {object} db
 * @param {{name: string, description?: string, emoji?: string}} input
 * @param {{displayName: string}} session
 * @returns {Promise<{success: boolean, clubId?: string, error?: string}>}
 */
export async function createClub(db, input, session) {
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
    await setDoc(clubRef, {
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
    });
    await setDoc(doc(db, col('clubs'), clubRef.id, 'members', slug), {
      displayName: session.displayName,
      role: 'host',
      status: 'active',
      joinedAt: serverTimestamp(),
    });
    return { success: true, clubId: clubRef.id };
  } catch (e) {
    return { success: false, error: e.message };
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

/**
 * Update club details. Any member may edit name/description/emoji;
 * joinMode ('open' | 'application') is a host setting (enforced in the UI).
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
  try {
    await updateDoc(doc(db, col('clubs'), clubId), updates);
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
  }
}

/** Reject (delete) a join request. */
export async function rejectRequest(db, clubId, targetSlug) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'requests', targetSlug));
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
  }
}
