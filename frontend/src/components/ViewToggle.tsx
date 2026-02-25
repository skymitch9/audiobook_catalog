import { useView } from '../contexts/ViewContext';
import './ViewToggle.css';

export function ViewToggle() {
  const { viewMode, setViewMode } = useView();

  return (
    <div className="view-toggle">
      <button
        className={`view-toggle-btn ${viewMode === 'grid' ? 'active' : ''}`}
        onClick={() => setViewMode('grid')}
        aria-label="Grid view"
        aria-pressed={viewMode === 'grid'}
        title="Grid view"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <rect x="3" y="3" width="7" height="7" />
          <rect x="14" y="3" width="7" height="7" />
          <rect x="14" y="14" width="7" height="7" />
          <rect x="3" y="14" width="7" height="7" />
        </svg>
      </button>
      <button
        className={`view-toggle-btn ${viewMode === 'table' ? 'active' : ''}`}
        onClick={() => setViewMode('table')}
        aria-label="Table view"
        aria-pressed={viewMode === 'table'}
        title="Table view"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <line x1="8" y1="6" x2="21" y2="6" />
          <line x1="8" y1="12" x2="21" y2="12" />
          <line x1="8" y1="18" x2="21" y2="18" />
          <line x1="3" y1="6" x2="3.01" y2="6" />
          <line x1="3" y1="12" x2="3.01" y2="12" />
          <line x1="3" y1="18" x2="3.01" y2="18" />
        </svg>
      </button>
    </div>
  );
}
