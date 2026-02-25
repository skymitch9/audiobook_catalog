/**
 * SearchBar Component Usage Example
 * 
 * This file demonstrates how to use the SearchBar component.
 * It is not part of the application but serves as documentation.
 */

import { useState } from 'react';
import SearchBar from './SearchBar';

function SearchBarExample() {
  const [searchResults, setSearchResults] = useState<string>('');

  const handleSearch = (query: string) => {
    console.log('Search query:', query);
    setSearchResults(query ? `Searching for: "${query}"` : 'Showing all books');
  };

  return (
    <div style={{ padding: '2rem' }}>
      <h2>SearchBar Component Example</h2>
      
      <SearchBar 
        onSearch={handleSearch}
        placeholder="Search audiobooks..."
      />
      
      <div style={{ marginTop: '1rem', color: '#666' }}>
        {searchResults}
      </div>

      <div style={{ marginTop: '2rem', fontSize: '0.875rem', color: '#999' }}>
        <h3>Features:</h3>
        <ul>
          <li>Type in the search box - the search is debounced by 300ms</li>
          <li>Clear button appears when there is text</li>
          <li>Click the ✕ button to clear the search</li>
          <li>The onSearch callback is called with the query string</li>
        </ul>
      </div>
    </div>
  );
}

export default SearchBarExample;
