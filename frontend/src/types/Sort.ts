/**
 * Sorting configuration types for the catalog.
 * 
 * These types define the structure for managing sort state,
 * including the field to sort by and the direction.
 */

/**
 * Valid sort fields for books.
 */
export type SortField = 'title' | 'author' | 'year' | 'duration' | 'series';

/**
 * Sort direction (ascending or descending).
 */
export type SortDirection = 'asc' | 'desc';

/**
 * Sort configuration interface.
 * 
 * @interface SortConfig
 * @property {SortField} field - The field to sort by
 * @property {SortDirection} direction - The sort direction (ascending or descending)
 */
export interface SortConfig {
  field: SortField;
  direction: SortDirection;
}
