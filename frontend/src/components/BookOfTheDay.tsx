import { useState, useEffect } from 'react';
import './BookOfTheDay.css';

interface Book {
  id: number;
  title: string;
  author: string;
  narrator?: string;
  series?: string;
  series_index_display?: string;
  duration_hhmm?: string;
  cover_url?: string;
}

interface BookOfTheDayProps {
  books: Book[];
}

export function BookOfTheDay({ books }: BookOfTheDayProps) {
  const [displayedBook, setDisplayedBook] = useState<Book | null>(null);

  // Filter for eligible books (standalone or first in series)
  const getEligibleBooks = () => {
    return books.filter(book => {
      const series = book.series || '';
      const index = book.series_index_display || '';
      
      // Include if: no series (standalone) OR series index is 1, One, I, or First
      if (!series) return true;
      if (!index) return false;
      
      const idxLower = index.toLowerCase().trim();
      return idxLower === '1' || idxLower === 'one' || idxLower === 'i' || 
             idxLower === 'first' || idxLower === '1.0' || idxLower === '01';
    });
  };

  // Get book of the day (date-based seed for consistent daily selection)
  const getBookOfDay = () => {
    const eligibleBooks = getEligibleBooks();
    if (eligibleBooks.length === 0) return null;

    const today = new Date();
    const daysSinceEpoch = Math.floor(today.getTime() / (1000 * 60 * 60 * 24));
    const seed = daysSinceEpoch % eligibleBooks.length;
    
    return eligibleBooks[seed];
  };

  // Get random book
  const getRandomBook = () => {
    const eligibleBooks = getEligibleBooks();
    if (eligibleBooks.length === 0) return null;
    
    const randomIndex = Math.floor(Math.random() * eligibleBooks.length);
    return eligibleBooks[randomIndex];
  };

  // Initialize with book of the day
  useEffect(() => {
    const dailyBook = getBookOfDay();
    setDisplayedBook(dailyBook);
  }, [books]);

  const handleRandomClick = () => {
    const randomBook = getRandomBook();
    setDisplayedBook(randomBook);
  };

  if (!displayedBook) {
    return (
      <div className="book-of-day">
        <div className="book-of-day-header">
          <h2>📚 Book of the Day</h2>
        </div>
        <div className="book-of-day-content">
          <div className="book-of-day-loading">No eligible books found</div>
        </div>
      </div>
    );
  }

  const seriesInfo = displayedBook.series
    ? `📖 ${displayedBook.series}${displayedBook.series_index_display ? ` #${displayedBook.series_index_display}` : ''}`
    : '📖 Standalone';

  return (
    <div className="book-of-day">
      <div className="book-of-day-header">
        <h2>📚 Book of the Day</h2>
        <button onClick={handleRandomClick} className="random-book-btn">
          🎲 Random Book
        </button>
      </div>
      <div className="book-of-day-content">
        {displayedBook.cover_url && (
          <div className="book-of-day-cover">
            <img src={displayedBook.cover_url} alt={`${displayedBook.title} cover`} />
          </div>
        )}
        <div className="book-of-day-info">
          <div className="book-of-day-title">{displayedBook.title}</div>
          <div className="book-of-day-author">by {displayedBook.author}</div>
          <div className="book-of-day-meta">{seriesInfo}</div>
          {displayedBook.narrator && (
            <div className="book-of-day-meta">🎙️ {displayedBook.narrator}</div>
          )}
          {displayedBook.duration_hhmm && (
            <div className="book-of-day-meta">⏱️ {displayedBook.duration_hhmm}</div>
          )}
        </div>
      </div>
    </div>
  );
}
