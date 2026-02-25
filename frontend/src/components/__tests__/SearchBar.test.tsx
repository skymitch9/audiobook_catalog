import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import SearchBar from '../SearchBar';

/**
 * SearchBar Component Tests
 * 
 * Basic smoke test to verify the component renders correctly.
 * Full unit tests will be implemented in task 3.6.
 */
describe('SearchBar Component', () => {
  it('renders search input with placeholder', () => {
    const mockOnSearch = vi.fn();
    
    render(<SearchBar onSearch={mockOnSearch} />);
    
    const input = screen.getByPlaceholderText('Search audiobooks...');
    expect(input).toBeInTheDocument();
  });

  it('renders with custom placeholder', () => {
    const mockOnSearch = vi.fn();
    const customPlaceholder = 'Find your book...';
    
    render(<SearchBar onSearch={mockOnSearch} placeholder={customPlaceholder} />);
    
    const input = screen.getByPlaceholderText(customPlaceholder);
    expect(input).toBeInTheDocument();
  });
});
