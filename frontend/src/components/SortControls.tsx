import type { SortConfig, SortField } from '../types/Sort';
import './SortControls.css';

interface SortControlsProps {
  config: SortConfig;
  onSortChange: (config: SortConfig) => void;
}

export function SortControls({ config, onSortChange }: SortControlsProps) {
  const handleFieldChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
    const newField = event.target.value as SortField;
    onSortChange({ ...config, field: newField });
  };

  const handleDirectionToggle = () => {
    const newDirection = config.direction === 'asc' ? 'desc' : 'asc';
    onSortChange({ ...config, direction: newDirection });
  };

  return (
    <div className="sort-controls">
      <label htmlFor="sort-field" className="sort-label">Sort by:</label>
      <select id="sort-field" className="sort-select" value={config.field} onChange={handleFieldChange}>
        <option value="title">Title</option>
        <option value="author">Author</option>
        <option value="year">Year</option>
        <option value="duration">Duration</option>
        <option value="series">Series</option>
      </select>
      <button className="sort-direction-btn" onClick={handleDirectionToggle} aria-label={`Sort ${config.direction}`} title={`Sort ${config.direction}`}>
        {config.direction === 'asc' ? '↑' : '↓'}
      </button>
    </div>
  );
}
