import { useState, useEffect } from 'react';
import { useDebounce } from '../hooks/useDebounce';
import './SearchBar.css';

/**
 * SearchBar Component Props
 * 
 * @interface SearchBarProps
 * @property {(query: string) => void} onSearch - Callback function receiving search query
 * @property {string} [placeholder] - Optional placeholder text for the input field
 * @property {string} [initialValue] - Optional initial value for the search input
 */
interface SearchBarProps {
  onSearch: (query: string) => void;
  placeholder?: string;
  initialValue?: string;
}

/**
 * SearchBar Component
 * 
 * Input field for searching audiobooks with debouncing functionality.
 * 
 * Features:
 * - Controlled input with useState
 * - Debounced search (300ms delay) using useDebounce hook
 * - Clear button to reset search
 * - Multi-token search support (space-separated keywords)
 * - Integrates with enhanced search service
 * 
 * Requirements: 7.5, 7.6
 * 
 * @param {SearchBarProps} props - Component props
 * @returns Rendered SearchBar component
 */
function SearchBar({ onSearch, placeholder = 'Search audiobooks...', initialValue = '' }: SearchBarProps) {
  const [inputValue, setInputValue] = useState<string>(initialValue);
  
  // Use debounce hook to delay search execution (Requirement 7.5)
  const debouncedValue = useDebounce(inputValue, 300);

  /**
   * Effect hook to call onSearch when debounced value changes
   * This ensures search only executes after user stops typing for 300ms
   */
  useEffect(() => {
    onSearch(debouncedValue);
  }, [debouncedValue, onSearch]);

  /**
   * Handle input change events
   * Updates the controlled input value
   * 
   * @param {React.ChangeEvent<HTMLInputElement>} event - Input change event
   */
  const handleInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    setInputValue(event.target.value);
  };

  /**
   * Handle clear button click
   * Resets the input value to empty string
   * This triggers the debounced search with empty query
   */
  const handleClear = () => {
    setInputValue('');
  };

  /**
   * Determine if clear button should be shown
   * Only show when input has content
   */
  const showClearButton = inputValue.trim() !== '';

  return (
    <div className="search-bar">
      <div className="search-bar-container">
        <input
          type="text"
          className="search-bar-input"
          value={inputValue}
          onChange={handleInputChange}
          placeholder={placeholder}
          aria-label="Search audiobooks"
        />
        {showClearButton && (
          <button
            type="button"
            className="search-bar-clear"
            onClick={handleClear}
            aria-label="Clear search"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}

export default SearchBar;
