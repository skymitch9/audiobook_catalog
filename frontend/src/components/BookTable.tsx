import type { Book } from '../types/Book';
import type { SortConfig, SortField } from '../types/Sort';
import './BookTable.css';

interface BookTableProps {
  books: Book[];
  sortConfig: SortConfig;
  onSort: (field: SortField) => void;
  onBookClick?: (bookId: number) => void;
}

export function BookTable({ books, sortConfig, onSort, onBookClick }: BookTableProps) {
  const getSortIndicator = (field: SortField): string => {
    if (sortConfig.field !== field) return '';
    return sortConfig.direction === 'asc' ? ' ↑' : ' ↓';
  };

  const formatSeries = (book: Book): string => {
    if (!book.series) return '-';
    if (book.series_index) return `${book.series} #${book.series_index}`;
    return book.series;
  };

  const handleRowClick = (bookId: number) => {
    if (onBookClick) {
      onBookClick(bookId);
    }
  };

  const handleCoverClick = (e: React.MouseEvent, bookId: number) => {
    e.stopPropagation();
    if (onBookClick) {
      onBookClick(bookId);
    }
  };

  return (
    <div className="book-table-container">
      <table className="book-table">
        <thead>
          <tr>
            <th>Cover</th>
            <th className="sortable" onClick={() => onSort('title')}>Title{getSortIndicator('title')}</th>
            <th className="sortable" onClick={() => onSort('author')}>Author{getSortIndicator('author')}</th>
            <th>Narrator</th>
            <th className="sortable" onClick={() => onSort('series')}>Series{getSortIndicator('series')}</th>
            <th>Genre</th>
            <th className="sortable" onClick={() => onSort('year')}>Year{getSortIndicator('year')}</th>
            <th className="sortable" onClick={() => onSort('duration')}>Duration{getSortIndicator('duration')}</th>
          </tr>
        </thead>
        <tbody>
          {books.map((book) => (
            <tr key={book.id} onClick={() => handleRowClick(book.id)} style={{ cursor: 'pointer' }}>
              <td onClick={(e) => handleCoverClick(e, book.id)}>
                <img src={book.cover_url} alt={book.title} loading="lazy" />
              </td>
              <td>{book.title}</td>
              <td>{book.author}</td>
              <td>{book.narrator}</td>
              <td>{formatSeries(book)}</td>
              <td>{book.genre}</td>
              <td>{book.year}</td>
              <td>{book.duration}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {books.length === 0 && <div className="book-table-empty">No books found</div>}
    </div>
  );
}
