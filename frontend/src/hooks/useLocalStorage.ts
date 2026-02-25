import { useState } from 'react';

/**
 * Custom hook for persisting state to localStorage with JSON serialization.
 * Handles parse errors gracefully by falling back to the initial value.
 * 
 * @template T - The type of the value to store
 * @param key - The localStorage key
 * @param initialValue - The initial value to use if no stored value exists or parsing fails
 * @returns A tuple of [storedValue, setValue] similar to useState
 * 
 * @example
 * const [theme, setTheme] = useLocalStorage<'light' | 'dark'>('theme', 'dark');
 */
export function useLocalStorage<T>(
  key: string,
  initialValue: T
): [T, (value: T) => void] {
  // State to store our value
  // Pass initial state function to useState so logic is only executed once
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      // Get from local storage by key
      const item = window.localStorage.getItem(key);
      // Parse stored json or if none return initialValue
      return item ? JSON.parse(item) : initialValue;
    } catch (error) {
      // If error (e.g., JSON parse error), return initial value
      console.warn(`Error reading localStorage key "${key}":`, error);
      return initialValue;
    }
  });

  // Return a wrapped version of useState's setter function that
  // persists the new value to localStorage.
  const setValue = (value: T) => {
    try {
      // Save state
      setStoredValue(value);
      // Save to local storage
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      // A more advanced implementation would handle the error case
      console.warn(`Error setting localStorage key "${key}":`, error);
    }
  };

  return [storedValue, setValue];
}
