import { useMemo } from 'react';
import type { SortConfig } from '../types/Sort';

/**
 * Custom hook for sorting logic.
 * 
 * This hook sorts an array of items based on the specified field and direction.
 * It handles different data types (string, number) and maintains a stable sort.
 * 
 * @template T - The type of items being sorted (must have sortable fields)
 * @param {T[]} items - The array of items to sort
 * @param {SortConfig} config - Sort configuration (field and direction)
 * @returns {T[]} Sorted array of items
 * 
 * Requirements: 4.1-4.8
 */
export function useSort<T extends Record<string, any>>(
  items: T[],
  config: SortConfig
): T[] {
  return useMemo(() => {
    // Create a shallow copy to avoid mutating the original array
    const sortedItems = [...items];

    // Sort the items based on the field and direction
    sortedItems.sort((a, b) => {
      const field = config.field as keyof T;
      let aValue = a[field];
      let bValue = b[field];

      // Handle different field types
      let comparison = 0;

      if (config.field === 'duration') {
        // For duration, use duration_minutes for numeric comparison
        const aDuration = a['duration_minutes' as keyof T] as number;
        const bDuration = b['duration_minutes' as keyof T] as number;
        comparison = aDuration - bDuration;
      } else if (config.field === 'year') {
        // For year, convert to number for proper numeric comparison
        const aYear = parseInt(String(aValue), 10) || 0;
        const bYear = parseInt(String(bValue), 10) || 0;
        comparison = aYear - bYear;
      } else {
        // For string fields (title, author, series), use localeCompare
        const aString = String(aValue || '').toLowerCase();
        const bString = String(bValue || '').toLowerCase();
        comparison = aString.localeCompare(bString);
      }

      // Apply sort direction
      return config.direction === 'asc' ? comparison : -comparison;
    });

    return sortedItems;
  }, [items, config.field, config.direction]);
}
