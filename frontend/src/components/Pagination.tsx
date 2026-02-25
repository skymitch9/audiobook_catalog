import type { PaginationConfig, PageSize } from '../types/Pagination';
import './Pagination.css';

interface PaginationProps {
  config: PaginationConfig;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: PageSize) => void;
}

export function Pagination({ config, onPageChange, onPageSizeChange }: PaginationProps) {
  const { currentPage, pageSize, totalItems } = config;
  const totalPages = pageSize === 'all' ? 1 : Math.ceil(totalItems / pageSize);
  const startIndex = pageSize === 'all' ? 1 : (currentPage - 1) * pageSize + 1;
  const endIndex = pageSize === 'all' ? totalItems : Math.min(currentPage * pageSize, totalItems);

  const handlePageSizeChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
    const value = event.target.value;
    const newSize: PageSize = value === 'all' ? 'all' : parseInt(value, 10);
    onPageSizeChange(newSize);
  };

  const isFirstPage = currentPage === 1;
  const isLastPage = currentPage === totalPages || pageSize === 'all';

  return (
    <div className="pagination">
      <div className="pagination-info">
        Showing {startIndex}-{endIndex} of {totalItems}
      </div>
      <div className="pagination-size">
        <label htmlFor="page-size">Per page:</label>
        <select id="page-size" value={pageSize} onChange={handlePageSizeChange}>
          <option value="25">25</option>
          <option value="50">50</option>
          <option value="100">100</option>
          <option value="all">All</option>
        </select>
      </div>
      {pageSize !== 'all' && totalPages > 1 && (
        <div className="pagination-controls">
          <button onClick={() => onPageChange(1)} disabled={isFirstPage}>«</button>
          <button onClick={() => onPageChange(Math.max(1, currentPage - 1))} disabled={isFirstPage}>‹</button>
          <span className="pagination-current">Page {currentPage} of {totalPages}</span>
          <button onClick={() => onPageChange(Math.min(totalPages, currentPage + 1))} disabled={isLastPage}>›</button>
          <button onClick={() => onPageChange(totalPages)} disabled={isLastPage}>»</button>
        </div>
      )}
    </div>
  );
}
