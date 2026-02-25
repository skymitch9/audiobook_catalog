import { useState, useEffect, useCallback } from 'react';

/**
 * Custom hook for syncing state with URL hash parameters.
 * Supports multiple key-value pairs in the hash (e.g., #search=test&page=2&sort=title).
 * Handles URL encoding/decoding automatically.
 * 
 * @template T - The type of the value to store
 * @param key - The URL hash parameter key
 * @param initialValue - The initial value to use if no hash parameter exists
 * @returns A tuple of [value, setValue] similar to useState
 * 
 * @example
 * const [searchQuery, setSearchQuery] = useUrlState<string>('search', '');
 * const [page, setPage] = useUrlState<number>('page', 1);
 * 
 * // URL will be updated to: #search=fantasy&page=2
 */
export function useUrlState<T>(
  key: string,
  initialValue: T
): [T, (value: T) => void] {
  // Parse the current URL hash to get the initial value
  const getValueFromHash = useCallback((): T => {
    try {
      const hash = window.location.hash.slice(1); // Remove the '#' prefix
      if (!hash) return initialValue;

      const params = new URLSearchParams(hash);
      const value = params.get(key);
      
      if (value === null) return initialValue;

      // Try to parse the value based on the type of initialValue
      // URLSearchParams.get() already decodes the value, so no need to decode again
      if (typeof initialValue === 'number') {
        const parsed = Number(value);
        return (isNaN(parsed) ? initialValue : parsed) as T;
      } else if (typeof initialValue === 'boolean') {
        return (value === 'true') as T;
      } else if (typeof initialValue === 'object' && initialValue !== null) {
        // For objects, try to parse as JSON
        try {
          return JSON.parse(value) as T;
        } catch {
          return initialValue;
        }
      }
      
      // For strings and other types, return the value as-is
      return value as T;
    } catch (error) {
      console.warn(`Error parsing URL hash for key "${key}":`, error);
      return initialValue;
    }
  }, [key, initialValue]);

  const [value, setValue] = useState<T>(getValueFromHash);

  // Update the URL hash when the value changes
  const setValueAndHash = useCallback((newValue: T) => {
    try {
      setValue(newValue);

      // Parse current hash
      const hash = window.location.hash.slice(1);
      const params = new URLSearchParams(hash);

      // Update or remove the parameter
      if (newValue === initialValue || newValue === '' || newValue === null || newValue === undefined) {
        // Remove the parameter if it's the initial value or empty
        params.delete(key);
      } else {
        // Set the value - URLSearchParams will handle encoding
        let valueToSet: string;
        if (typeof newValue === 'object' && newValue !== null) {
          valueToSet = JSON.stringify(newValue);
        } else {
          valueToSet = String(newValue);
        }
        params.set(key, valueToSet);
      }

      // Update the URL hash
      const newHash = params.toString();
      window.location.hash = newHash ? `#${newHash}` : '';
    } catch (error) {
      console.warn(`Error updating URL hash for key "${key}":`, error);
    }
  }, [key, initialValue]);

  // Listen for hash changes (e.g., browser back/forward)
  useEffect(() => {
    const handleHashChange = () => {
      const newValue = getValueFromHash();
      setValue(newValue);
    };

    window.addEventListener('hashchange', handleHashChange);
    
    return () => {
      window.removeEventListener('hashchange', handleHashChange);
    };
  }, [getValueFromHash]);

  return [value, setValueAndHash];
}
