// fb-env.js — data lane switch for the two-lane Pages deploy
// ES module, browser-native (no build step)
//
// The dev lane reads and writes *_dev Firestore collections, so experiments
// never touch prod data. A page is on the dev lane when it is:
//   - served under /dev/ (the Pages dev lane, built from main), or
//   - served from localhost / 127.0.0.1 (local development).
// The prod lane (Pages site root, built from the prod branch) resolves to the
// unsuffixed names and behaves exactly as before.

/**
 * The ONE Firebase web config for this project.
 *
 * ⚠️ Every page that calls initializeApp() must import this rather than
 * inlining the object. Before 2026-08-16 the config was pasted inline in ten
 * places; the SSO flip that moved authDomain to auth.heygabi.ai had to edit
 * each one by hand and MISSED app/tools/generate_stats.py, which still emits
 * the pre-flip "audiobook-catalog.firebaseapp.com". That is the exact failure
 * this export exists to prevent — change the value here, once.
 *
 * These values are public by design (they identify the project to Firebase and
 * ship in the page source); access control lives in firestore.rules, not here.
 *
 * ⚠️ authDomain is auth.heygabi.ai — a custom domain, NOT the default
 * *.firebaseapp.com. Google sign-in depends on it. Do not "correct" it back.
 */
export const FIREBASE_CONFIG = {
  apiKey: "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y",
  authDomain: "auth.heygabi.ai",
  projectId: "audiobook-catalog",
  storageBucket: "audiobook-catalog.firebasestorage.app",
  messagingSenderId: "68492219785",
  appId: "1:68492219785:web:7cbe57dda8712377f0bd58"
};

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
