// fb-env.js — data lane switch for the two-lane Pages deploy
// ES module, browser-native (no build step)
//
// The dev lane reads and writes *_dev Firestore collections, so experiments
// never touch prod data. A page is on the dev lane when it is:
//   - served under /dev/ (the Pages dev lane, built from main), or
//   - served from localhost / 127.0.0.1 (local development).
// The prod lane (Pages site root, built from the prod branch) resolves to the
// unsuffixed names and behaves exactly as before.

const DEV_HOSTNAMES = ['localhost', '127.0.0.1'];

/**
 * Decide whether a location is on the dev data lane.
 * @param {{pathname: string, hostname: string}} loc - window.location or equivalent
 * @returns {boolean}
 */
export function detectDevLane(loc) {
  if (!loc) return false;
  return loc.pathname.includes('/dev/') || DEV_HOSTNAMES.includes(loc.hostname);
}

export const IS_DEV_LANE =
  typeof window !== 'undefined' && detectDevLane(window.location);

export const COLLECTION_SUFFIX = IS_DEV_LANE ? '_dev' : '';

/**
 * Resolve a Firestore collection name for the current lane.
 * @param {string} name - base collection name, e.g. 'reviews'
 * @returns {string} 'reviews' on prod, 'reviews_dev' on the dev lane
 */
export function col(name) {
  return name + COLLECTION_SUFFIX;
}
