// fb-env.js — data lane switch for the two-lane Pages deploy
// ES module, browser-native (no build step)
//
// Pages served under /dev/ (the dev lane, built from main) read and write
// *_dev Firestore collections, so dev experiments never touch prod data.
// The prod lane (site root, built from the prod branch) resolves to the
// unsuffixed names and behaves exactly as before.

export const IS_DEV_LANE =
  typeof window !== 'undefined' && window.location.pathname.includes('/dev/');

export const COLLECTION_SUFFIX = IS_DEV_LANE ? '_dev' : '';

/**
 * Resolve a Firestore collection name for the current lane.
 * @param {string} name - base collection name, e.g. 'reviews'
 * @returns {string} 'reviews' on prod, 'reviews_dev' on the /dev/ lane
 */
export function col(name) {
  return name + COLLECTION_SUFFIX;
}
