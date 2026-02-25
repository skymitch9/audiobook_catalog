import { useState, useEffect, useMemo, useCallback } from 'react';
import type { Book } from '../types/Book';
import type { SortConfig, SortField } from '../types/Sort';
import type { PageSize } from '../types/Pagination';
import { getAllBooks } from '../services/api';
import { searchBooks as searchBooksService } from '../services/search';
import { useSort } from '../hooks/useSort';
import { usePagination } from '../hooks/usePagination';
import { useUrlState } from '../hooks/useUrlState';
import { useView } from '../contexts/ViewContext';
import BookCard from '../components/BookCard';
import { BookTable } from '../components/BookTable';
import { BookModal } from '../components/BookModal';
import SearchBar from '../components/SearchBar';
import { SortControls } from '../components/SortControls';
import { Pagination } from '../components/Pagination';
import { ViewToggle } from '../components/ViewToggle';
import { BookOfTheDay } from '../components/BookOfTheDay';
import './CatalogPage.css';

/**
 * CatalogPage Component
 * 
 * Main page displaying audiobooks with search, sort, pagination, and view toggle.
 * 
 * Features:
 * - Fetches all books from API on mount
 * - Enhanced search with multi-token AND logic
 * - Sorting by multiple fields (title, author, year, duration, series)
 * - Pagination with configurable page size
 * - Grid/Table view toggle
 * - URL state persistence for search, sort, and pagination
 * - Loading indicator during API requests
 * - Error message display on API failures
 * - "No books found" message for empty results
 * - Responsive layout
 * 
 * Requirements: 4.1-4.8, 5.1-5.8, 6.1-6.7, 7.1-7.8
 * 
 * @returns Rendered CatalogPage component
 */
function CatalogPage() {
  // State management
  const [books, setBooks] = useState<Book[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedBookId, setSelectedBookId] = useState<number | null>(null);

  // URL state for search, sort, and pagination (Requirements 5.7, 7.6)
  const [searchQuery, setSearchQuery] = useUrlState<string>('search', '');
  const [sortField, setSortField] = useUrlState<SortField>('sort', 'title');
  const [sortDirection, setSortDirection] = useUrlState<'asc' | 'desc'>('direction', 'asc');
  const [currentPage, setCurrentPage] = useUrlState<number>('page', 1);
  const [pageSize, setPageSize] = useUrlState<PageSize>('pageSize', 25);

  // View mode from context (Requirement 6.1)
  const { viewMode } = useView();

  /**
   * Fetch all books on component mount
   * 
   * Effect runs once when component mounts to load initial book data.
   * Sets loading state during fetch and handles errors appropriately.
   * 
   * Requirements: 1.1, 5.4
   */
  useEffect(() => {
    const fetchBooks = async () => {
      try {
        setLoading(true);
        setError(null);
        const data = await getAllBooks();
        setBooks(data);
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Failed to load books';
        setError(errorMessage);
      } finally {
        setLoading(false);
      }
    };

    fetchBooks();
  }, []);

  /**
   * Apply search filter to books (Requirements 7.1, 7.2, 7.3, 7.4)
   */
  const filteredBooks = useMemo(() => {
    return searchBooksService(books, searchQuery);
  }, [books, searchQuery]);

  /**
   * Sort configuration
   */
  const sortConfig: SortConfig = useMemo(() => ({
    field: sortField,
    direction: sortDirection,
  }), [sortField, sortDirection]);

  /**
   * Apply sort to filtered books (Requirement 4.8)
   */
  const sortedBooks = useSort(filteredBooks, sortConfig);

  /**
   * Apply pagination to sorted books (Requirement 5.6)
   */
  const { displayedBooks } = useMemo(() => {
    const paginationResult = usePagination(sortedBooks, pageSize, currentPage);
    return {
      displayedBooks: paginationResult.displayedItems,
    };
  }, [sortedBooks, pageSize, currentPage]);

  /**
   * Handle search query changes (Requirement 7.5)
   */
  const handleSearch = useCallback((query: string) => {
    setSearchQuery(query);
    setCurrentPage(1); // Reset to first page on new search
  }, [setSearchQuery, setCurrentPage]);

  /**
   * Handle sort changes (Requirements 4.1-4.7)
   */
  const handleSortChange = useCallback((newConfig: SortConfig) => {
    // If same field, toggle direction; otherwise, set new field with asc direction
    if (newConfig.field === sortField) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(newConfig.field);
      setSortDirection(newConfig.direction);
    }
  }, [sortField, sortDirection, setSortField, setSortDirection]);

  /**
   * Handle sort field click from table headers (Requirement 6.5)
   */
  const handleTableSort = useCallback((field: SortField) => {
    if (field === sortField) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDirection('asc');
    }
  }, [sortField, sortDirection, setSortField, setSortDirection]);

  /**
   * Handle page changes (Requirement 5.2)
   */
  const handlePageChange = useCallback((page: number) => {
    setCurrentPage(page);
  }, [setCurrentPage]);

  /**
   * Handle page size changes (Requirement 5.1)
   */
  const handlePageSizeChange = useCallback((size: PageSize) => {
    setPageSize(size);
    setCurrentPage(1); // Reset to first page when changing page size
  }, [setPageSize, setCurrentPage]);

  /**
   * Handle book click to open modal
   */
  const handleBookClick = useCallback((bookId: number) => {
    setSelectedBookId(bookId);
  }, []);

  /**
   * Handle modal close
   */
  const handleCloseModal = useCallback(() => {
    setSelectedBookId(null);
  }, []);

  /**
   * Render loading indicator
   */
  if (loading) {
    return (
      <div className="catalog-page">
        <div className="catalog-loading">
          <div className="loading-spinner"></div>
          <p>Loading audiobooks...</p>
        </div>
      </div>
    );
  }

  /**
   * Render error message
   * 
   * Requirements: 5.3
   */
  if (error) {
    return (
      <div className="catalog-page">
        <SearchBar onSearch={handleSearch} initialValue={searchQuery} />
        <div className="catalog-error">
          <p className="error-message">⚠️ {error}</p>
          <button 
            className="retry-button"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  /**
   * Render empty results message (Requirement 7.8)
   */
  if (filteredBooks.length === 0) {
    return (
      <div className="catalog-page">
        <div className="catalog-controls">
          <SearchBar onSearch={handleSearch} initialValue={searchQuery} />
          <ViewToggle />
        </div>
        <div className="catalog-empty">
          <p className="empty-message">
            {searchQuery.trim() !== '' 
              ? `No books found for "${searchQuery}". Try different keywords or clear your search.`
              : 'No books available'}
          </p>
        </div>
      </div>
    );
  }

  /**
   * Pagination configuration
   */
  const paginationConfig = {
    currentPage,
    pageSize,
    totalItems: filteredBooks.length,
  };

  /**
   * Render catalog with books (Requirements 4.8, 5.6, 6.2, 6.3, 6.7)
   */
  return (
    <div className="catalog-page">
      <div className="catalog-controls">
        <SearchBar onSearch={handleSearch} initialValue={searchQuery} />
        <div className="catalog-controls-right">
          <SortControls config={sortConfig} onSortChange={handleSortChange} />
          <ViewToggle />
        </div>
      </div>

      {/* Book of the Day */}
      <BookOfTheDay books={books} />

      {/* Conditionally render grid or table based on view mode (Requirement 6.2, 6.3) */}
      {viewMode === 'grid' ? (
        <div className="catalog-grid">
          {displayedBooks.map((book) => (
            <BookCard
              key={book.id}
              book={book}
              onClick={() => handleBookClick(book.id)}
            />
          ))}
        </div>
      ) : (
        <BookTable
          books={displayedBooks}
          sortConfig={sortConfig}
          onSort={handleTableSort}
          onBookClick={handleBookClick}
        />
      )}

      {/* Pagination controls (Requirement 5.1-5.8) */}
      <Pagination
        config={paginationConfig}
        onPageChange={handlePageChange}
        onPageSizeChange={handlePageSizeChange}
      />

      {/* Book detail modal */}
      {selectedBookId !== null && (
        <BookModal bookId={selectedBookId} onClose={handleCloseModal} />
      )}
    </div>
  );
}

export default CatalogPage;
