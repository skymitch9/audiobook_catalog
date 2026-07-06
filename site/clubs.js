// clubs.js — book club system, Phase 1: create/browse/join/leave clubs + members
// ES module, browser-native (no build step)
// Design: docs/BOOK_CLUBS_DESIGN.md (gitignored; lives on the dev machine)

import {
  collection, doc, getDoc, getDocs, setDoc, deleteDoc,
  query, where, serverTimestamp, runTransaction,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { slugifyName } from './identity.js';

// Base32-ish alphabet without lookalike characters (no I/L/O/U/0/1)
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTVWXYZ23456789';
export const CODE_LENGTH = 8;

/**
 * Generate an 8-char invite code from a lookalike-free alphabet.
 * @returns {string}
 */
export function generateInviteCode() {
  const bytes = new Uint8Array(CODE_LENGTH);
  crypto.getRandomValues(bytes);
  let code = '';
  for (const b of bytes) {
    code += CODE_ALPHABET[b % CODE_ALPHABET.length];
  }
  return code;
}

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
 * @param {{name: string, description?: string, emoji?: string, isPublic?: boolean}} input
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
      isPublic: input.isPublic !== false,
      inviteCode: generateInviteCode(),
      hostSlug: slug,
      hostDisplayName: session.displayName,
      memberSlugs: [slug],
      memberCount: 1,
      createdAt: serverTimestamp(),
    });
    await setDoc(doc(db, col('clubs'), clubRef.id, 'members', slug), {
      displayName: session.displayName,
      role: 'host',
      joinedAt: serverTimestamp(),
    });
    return { success: true, clubId: clubRef.id };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/**
 * Fetch all public clubs, newest first.
 */
export async function getPublicClubs(db) {
  const snap = await getDocs(query(collection(db, col('clubs')), where('isPublic', '==', true)));
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}

/**
 * Fetch clubs the user belongs to (public or private).
 */
export async function getMyClubs(db, displayName) {
  const slug = slugifyName(displayName);
  const snap = await getDocs(
    query(collection(db, col('clubs')), where('memberSlugs', 'array-contains', slug))
  );
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
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
 * Look up a club by invite code (case-insensitive). Returns null if not found.
 */
export async function findClubByInviteCode(db, code) {
  const normalized = (code || '').trim().toUpperCase();
  if (!normalized) return null;
  const snap = await getDocs(
    query(collection(db, col('clubs')), where('inviteCode', '==', normalized))
  );
  return snap.docs.length > 0 ? { id: snap.docs[0].id, ...snap.docs[0].data() } : null;
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
        joinedAt: serverTimestamp(),
      });
    }
    return { success: true, clubId };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/**
 * Leave a club. The host cannot leave — they must delete the club
 * (or a future phase adds host transfer).
 */
export async function leaveClub(db, clubId, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Not signed in.' };
  }
  const slug = slugifyName(session.displayName);
  return removeMemberBySlug(db, clubId, slug);
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
        throw new Error('The host cannot leave. Delete the club instead.');
      }
      const slugs = (data.memberSlugs || []).filter(s => s !== targetSlug);
      tx.update(clubRef, { memberSlugs: slugs, memberCount: slugs.length });
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
