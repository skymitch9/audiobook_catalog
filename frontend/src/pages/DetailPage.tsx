import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import type { Book } from '../types/Book';
import { getBookById } from '../services/api';
import './DetailPage.css';

/**
 * DetailPage Component
 * 
 * Display complete information for a single audiobook.
 * 
 * Features:
 * - Fetches book data by ID from URL parameter
 * - Displays all book properties (title, author, narrator, series, year, genre, duration, description)
 * - Shows larger cover image
 * - Loading indicator during fetch
 * - Error message on API failure or book not found
 * - Back button navigation to catalog
 * - Flexbox layout for responsive design
 * 
 * Requirements: 3.1, 3.2, 3.3, 3.5, 5.3, 5.4, 6.4
 * 
 * @returns Rendered DetailPage component
 */
function DetailPage() {
  // Extract book ID from URL parameters
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  // State management
  const [book, setBook] = useState<Book | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [imageError, setImageError] = useState<boolean>(false);

  /**
   * Fetch book data when component mounts or ID changes
   * 
   * Effect runs when the book ID from URL changes.
   * Fetches book data from API and handles loading/error states.
   * 
   * Requirements: 3.2, 5.4
   */
  useEffect(() => {
    const fetchBook = async () => {
      // Validate ID parameter
      if (!id) {
        setError('No book ID provided');
        setLoading(false);
        return;
      }

      const bookId = parseInt(id, 10);
      if (isNaN(bookId)) {
        setError('Invalid book ID');
        setLoading(false);
        return;
      }

      try {
        setLoading(true);
        setError(null);
        const data = await getBookById(bookId);
        setBook(data);
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Failed to load book details';
        setError(errorMessage);
      } finally {
        setLoading(false);
      }
    };

    fetchBook();
  }, [id]);

  /**
   * Handle back button click
   * 
   * Navigates back to the catalog page.
   * 
   * Requirements: 3.5
   */
  const handleBack = () => {
    navigate('/');
  };

  /**
   * Handle image load errors
   * 
   * Sets error state to display fallback image.
   */
  const handleImageError = () => {
    setImageError(true);
  };
  /**
   * Render loading indicator
   * 
   * Requirements: 5.4
   */
  if (loading) {
    return (
      <div className="detail-page">
        <div className="detail-loading">
          <div className="loading-spinner"></div>
          <p>Loading book details...</p>
        </div>
      </div>
    );
  }

  /**
   * Render error message
   * 
   * Requirements: 5.3
   */
  if (error || !book) {
    return (
      <div className="detail-page">
        <div className="detail-error">
          <p className="error-message">⚠️ {error || 'Book not found'}</p>
          <button className="back-button" onClick={handleBack}>
            ← Back to Catalog
          </button>
        </div>
      </div>
    );
  }

  /**
   * Render book details
   * 
   * Requirements: 3.3, 3.4
   */
  return (
    <div className="detail-page">
      <button className="back-button" onClick={handleBack}>
        ← Back to Catalog
      </button>

      <div className="detail-content">
        {/* Cover image section */}
        <div className="detail-cover">
          {imageError ? (
            <div className="detail-cover-fallback">
              <span className="fallback-icon">📚</span>
              <span className="fallback-text">No Cover Available</span>
            </div>
          ) : (
            <img
              src={book.cover_url}
              alt={`Cover of ${book.title}`}
              onError={handleImageError}
            />
          )}
        </div>

        {/* Book information section */}
        <div className="detail-info">
          <h1 className="detail-title">{book.title}</h1>

          <div className="detail-metadata">
            <div className="metadata-item">
              <span className="metadata-label">Series:</span>
              <span className="metadata-value">{book.series || '-'}</span>
            </div>

            <div className="metadata-item">
              <span className="metadata-label">#:</span>
              <span className="metadata-value">{book.series_index_display || book.series_index || '-'}</span>
            </div>

            <div className="metadata-item">
              <span className="metadata-label">Author:</span>
              <span className="metadata-value">{book.author}</span>
            </div>

            <div className="metadata-item">
              <span className="metadata-label">Narrator:</span>
              <span className="metadata-value">{book.narrator}</span>
            </div>

            <div className="metadata-item">
              <span className="metadata-label">Year:</span>
              <span className="metadata-value">{book.year}</span>
            </div>

            <div className="metadata-item">
              <span className="metadata-label">Genre:</span>
              <span className="metadata-value">{book.genre}</span>
            </div>

            <div className="metadata-item">
              <span className="metadata-label">Duration:</span>
              <span className="metadata-value">{book.duration_hhmm || book.duration}</span>
            </div>
          </div>

          {book.desc && (
            <div className="detail-description">
              <p>{book.desc}</p>
            </div>
          )}
          
          {!book.desc && book.description && (
            <div className="detail-description">
              <p>{book.description}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default DetailPage;
