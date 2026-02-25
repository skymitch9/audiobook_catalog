/**
 * Statistics data structure for the statistics dashboard.
 * 
 * This interface defines aggregated statistics calculated from the book collection,
 * including totals, averages, and top lists for various metadata fields.
 */

/**
 * Represents a count entry for top lists (authors, narrators, genres).
 */
export interface CountEntry {
  name: string;
  count: number;
}

/**
 * Statistics interface containing all calculated metrics for the collection.
 * 
 * @interface Statistics
 * @property {number} totalBooks - Total count of books in the collection
 * @property {number} totalDurationMinutes - Sum of all book durations in minutes
 * @property {number} totalDurationHours - Total duration converted to hours (rounded)
 * @property {number} totalDurationDays - Total duration converted to days (rounded to 2 decimals)
 * @property {number} averageDurationMinutes - Mean duration across all books
 * @property {CountEntry[]} topAuthors - Top 10 authors by book count
 * @property {CountEntry[]} topNarrators - Top 10 narrators by book count
 * @property {CountEntry[]} topGenres - Top 10 genres by book count
 */
export interface Statistics {
  totalBooks: number;
  totalDurationMinutes: number;
  totalDurationHours: number;
  totalDurationDays: number;
  averageDurationMinutes: number;
  topAuthors: CountEntry[];
  topNarrators: CountEntry[];
  topGenres: CountEntry[];
}
