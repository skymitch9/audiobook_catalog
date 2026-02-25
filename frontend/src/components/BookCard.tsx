import { useState } from 'react';
import type { Book } from '../types/Book';
import './BookCard.css';

/**
 * BookCard Component Props
 * 
 * @interface BookCardProps
 * @property {Book} book - The book object containing all book data
 * @property {() => void} [onClick] - Optional callback for click handling
 */
interface BookCardProps {
  book: Book;
  onClick?: () => void;
}

/**
 * BookCard Component
 * 
 * Reusable component displaying summary information for a single audiobook.
 * Displays cover image, title, author, narrator, and series information.
 * 
 * Features:
 * - Cover image with fallback for missing images
 * - Title, author, and narrator display
 * - Series information (if available)
 * - Clickable card for opening modal
 * - CSS Grid layout for internal structure
 * 
 * Requirements: 6.2, 1.3
 * 
 * @param {BookCardProps} props - Component props
 * @returns Rendered BookCard component
 */
function BookCard({ book, onClick }: BookCardProps) {
  const [imageError, setImageError] = useState(false);

  /**
   * Handle image load errors by setting error state
   * This triggers the fallback image display
   */
  const handleImageError = () => {
    setImageError(true);
  };

  /**
   * Handle card click
   */
  const handleClick = () => {
    if (onClick) {
      onClick();
    }
  };

  /**
   * Handle keyboard events for accessibility
   */
  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      handleClick();
    }
  };

  /**
   * Determine if series information should be displayed
   * Series is displayed if both series name and index are present
   */
  const hasSeriesInfo = book.series && book.series.trim() !== '' && 
                        book.series_index && book.series_index.trim() !== '';

  return (
    <div
      className="book-card"
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      role="button"
      tabIndex={0}
      aria-label={`View details for ${book.title} by ${book.author}`}
    >
      <div className="book-card-cover">
        {imageError ? (
          <div className="book-card-cover-fallback">
            <span className="fallback-icon">📚</span>
            <span className="fallback-text">No Cover</span>
          </div>
        ) : (
          <img
            src={book.cover_url}
            alt={`Cover of ${book.title}`}
            onError={handleImageError}
            loading="lazy"
          />
        )}
      </div>

      <div className="book-card-content">
        <h3 className="book-card-title">{book.title}</h3>
        
        {hasSeriesInfo && (
          <p className="book-card-series">
            {book.series} #{book.series_index}
          </p>
        )}
        
        <p className="book-card-author">
          <span className="label">Author:</span> {book.author}
        </p>
        
        <p className="book-card-narrator">
          <span className="label">Narrator:</span> {book.narrator}
        </p>
      </div>
    </div>
  );
}

export default BookCard;
