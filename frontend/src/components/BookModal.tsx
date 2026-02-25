import { useState, useEffect } from 'react';
import { getBookById } from '../services/api';
import type { Book } from '../types/Book';
import './BookModal.css';

interface BookModalProps {
  bookId: number;
  onClose: () => void;
}

export function BookModal({ bookId, onClose }: BookModalProps) {
  const [book, setBook] = useState<Book | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [imageError, setImageError] = useState<boolean>(false);

  useEffect(() => {
    const fetchBook = async () => {
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
  }, [bookId]);

  // Close modal on Escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [onClose]);

  // Prevent body scroll when modal is open
  useEffect(() => {
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = '';
    };
  }, []);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) {
      onClose();
    }
  };

  const handleImageError = () => {
    setImageError(true);
  };

  if (loading) {
    return (
      <div className="book-modal-overlay" onClick={handleOverlayClick}>
        <div className="book-modal-panel">
          <button className="book-modal-close" onClick={onClose}>✕</button>
          <div className="book-modal-loading">
            <div className="loading-spinner"></div>
            <p>Loading book details...</p>
          </div>
        </div>
      </div>
    );
  }

  if (error || !book) {
    return (
      <div className="book-modal-overlay" onClick={handleOverlayClick}>
        <div className="book-modal-panel">
          <button className="book-modal-close" onClick={onClose}>✕</button>
          <div className="book-modal-error">
            <p className="error-message">⚠️ {error || 'Book not found'}</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="book-modal-overlay" onClick={handleOverlayClick}>
      <div className="book-modal-panel">
        <button className="book-modal-close" onClick={onClose}>✕</button>
        
        <div>
          {imageError ? (
            <div className="book-modal-cover-fallback">
              <span className="fallback-icon">📚</span>
              <span className="fallback-text">No Cover Available</span>
            </div>
          ) : (
            <img
              src={book.cover_url}
              alt={`Cover of ${book.title}`}
              className="book-modal-cover"
              onError={handleImageError}
            />
          )}
        </div>

        <div className="book-modal-info">
          <h2 className="book-modal-title">{book.title}</h2>

          <div className="book-modal-metadata">
            <div className="metadata-label">Series:</div>
            <div className="metadata-value">{book.series || '-'}</div>

            <div className="metadata-label">#:</div>
            <div className="metadata-value">{book.series_index_display || book.series_index || '-'}</div>

            <div className="metadata-label">Author:</div>
            <div className="metadata-value">{book.author}</div>

            <div className="metadata-label">Narrator:</div>
            <div className="metadata-value">{book.narrator}</div>

            <div className="metadata-label">Year:</div>
            <div className="metadata-value">{book.year}</div>

            <div className="metadata-label">Genre:</div>
            <div className="metadata-value">{book.genre}</div>

            <div className="metadata-label">Duration:</div>
            <div className="metadata-value">{book.duration_hhmm || book.duration}</div>
          </div>

          {(book.desc || book.description) && (
            <div className="book-modal-description">
              {book.desc || book.description}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
