import { useState, useEffect } from 'react';

/**
 * Custom hook for debouncing rapidly changing values.
 * Returns a debounced value that only updates after the specified delay
 * has elapsed since the last value change.
 * 
 * @template T - The type of the value to debounce
 * @param value - The value to debounce
 * @param delay - The delay in milliseconds before updating the debounced value
 * @returns The debounced value
 * 
 * @example
 * const [searchQuery, setSearchQuery] = useState('');
 * const debouncedQuery = useDebounce(searchQuery, 300);
 * 
 * // debouncedQuery will only update 300ms after the user stops typing
 */
export function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);

  useEffect(() => {
    // Set up a timeout to update the debounced value after the delay
    const timeoutId = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    // Clean up the timeout if value changes before delay elapses
    // or if the component unmounts
    return () => {
      clearTimeout(timeoutId);
    };
  }, [value, delay]); // Re-run effect when value or delay changes

  return debouncedValue;
}
