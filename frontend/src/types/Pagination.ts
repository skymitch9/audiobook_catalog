/**
 * Pagination configuration types for the catalog.
 * 
 * These types define the structure for managing pagination state,
 * including current page, page size, and total items.
 */

/**
 * Page size type - can be a number or 'all' to show all items.
 */
export type PageSize = number | 'all';

/**
 * Pagination configuration interface.
 * 
 * @interface PaginationConfig
 * @property {number} currentPage - The current page number (1-indexed)
 * @property {PageSize} pageSize - Number of items per page or 'all'
 * @property {number} totalItems - Total number of items in the collection
 */
export interface PaginationConfig {
  currentPage: number;
  pageSize: PageSize;
  totalItems: number;
}
