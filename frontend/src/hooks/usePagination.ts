/**
 * Custom hook for pagination logic.
 * 
 * This hook calculates pagination details including displayed items,
 * total pages, and start/end indices for the current page.
 * 
 * @template T - The type of items being paginated
 * @param {T[]} items - The complete array of items to paginate
 * @param {number | 'all'} pageSize - Number of items per page or 'all' to show all items
 * @param {number} currentPage - The current page number (1-indexed)
 * @returns {Object} Pagination details
 * @returns {T[]} displayedItems - Items for the current page
 * @returns {number} totalPages - Total number of pages
 * @returns {number} startIndex - Start index for current page (0-indexed)
 * @returns {number} endIndex - End index for current page (0-indexed, inclusive)
 * 
 * Requirements: 5.1-5.8
 */
export function usePagination<T>(
  items: T[],
  pageSize: number | 'all',
  currentPage: number
) {
  // Handle "all" page size option
  if (pageSize === 'all') {
    return {
      displayedItems: items,
      totalPages: 1,
      startIndex: 0,
      endIndex: items.length - 1,
    };
  }

  // Calculate total pages
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));

  // Clamp current page to valid range
  const safePage = Math.max(1, Math.min(currentPage, totalPages));

  // Calculate start and end indices (0-indexed)
  const startIndex = (safePage - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize - 1, items.length - 1);

  // Extract displayed items for current page
  const displayedItems = items.slice(startIndex, endIndex + 1);

  return {
    displayedItems,
    totalPages,
    startIndex,
    endIndex,
  };
}
