/**
 * Text normalization utilities for search functionality.
 * 
 * These functions handle text normalization including accent removal
 * and case normalization to enable flexible search matching.
 */

/**
 * Removes accents and diacritical marks from text.
 * 
 * Uses Unicode normalization (NFD) to decompose characters into base characters
 * and combining marks, then removes the combining marks.
 * 
 * @param text - The text to normalize
 * @returns Text with accents removed (e.g., "café" → "cafe", "niño" → "nino")
 * 
 * @example
 * removeAccents("café") // "cafe"
 * removeAccents("niño") // "nino"
 * removeAccents("Zürich") // "Zurich"
 */
export function removeAccents(text: string): string {
  return text
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '');
}

/**
 * Normalizes text for search by converting to lowercase, removing accents, and trimming.
 * 
 * This function combines multiple normalization steps to create a consistent
 * format for search matching, making searches case-insensitive and accent-insensitive.
 * 
 * @param text - The text to normalize
 * @returns Normalized text (lowercase, no accents, trimmed)
 * 
 * @example
 * normalizeSearchText("  Café Münchën  ") // "cafe munchen"
 * normalizeSearchText("HELLO WORLD") // "hello world"
 */
export function normalizeSearchText(text: string): string {
  return removeAccents(text.toLowerCase().trim());
}
