/**
 * Book interface representing an audiobook with all its metadata.
 * 
 * This interface defines the structure for audiobook data retrieved from the backend API.
 * It includes all required fields for displaying book information in the catalog and detail views.
 * 
 * @interface Book
 * @property {number} id - Unique identifier for the book
 * @property {string} title - Book title
 * @property {string} series - Series name (empty string if not part of series)
 * @property {string} series_index - Position in series (empty string if not applicable)
 * @property {string} series_index_display - Display version of series index
 * @property {string} author - Book author name
 * @property {string} narrator - Audiobook narrator name
 * @property {string} year - Publication year
 * @property {string} genre - Book genre/category
 * @property {string} duration - Human-readable duration (e.g., "5h 30m")
 * @property {string} duration_hhmm - Duration in HH:MM format from CSV
 * @property {number} duration_minutes - Duration in minutes as integer
 * @property {string} cover_url - Relative path to cover image
 * @property {string} [desc] - Optional detailed description from CSV
 * @property {string} [description] - Optional detailed description
 * @property {string} [drive_url] - Optional Google Drive link for listening/downloading
 */
export interface Book {
  id: number;
  title: string;
  series: string;
  series_index: string;
  series_index_display?: string;
  author: string;
  narrator: string;
  year: string;
  genre: string;
  duration: string;
  duration_hhmm?: string;
  duration_minutes: number;
  cover_url: string;
  desc?: string;
  description?: string;
  drive_url?: string;
}
