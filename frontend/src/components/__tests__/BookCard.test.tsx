import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import BookCard from '../BookCard';
import type { Book } from '../../types/Book';

/**
 * Unit tests for BookCard component
 * 
 * Tests cover:
 * - Rendering with complete book data
 * - Rendering with missing series information
 * - Click handler functionality
 * - Image fallback on error
 * 
 * Requirements: 1.3, 1.5, 6.2
 */

describe('BookCard Component', () => {
  // Mock book data with complete information
  const mockBookComplete: Book = {
    id: 1,
    title: 'The Great Audiobook',
    series: 'Epic Series',
    series_index: '1',
    author: 'John Doe',
    narrator: 'Jane Smith',
    year: '2024',
    genre: 'Fiction',
    duration: '5h 30m',
    duration_minutes: 330,
    cover_url: '/covers/test.jpg',
    description: 'A great audiobook',
  };

  // Mock book data without series information
  const mockBookNoSeries: Book = {
    id: 2,
    title: 'Standalone Book',
    series: '',
    series_index: '',
    author: 'Alice Johnson',
    narrator: 'Bob Williams',
    year: '2023',
    genre: 'Non-Fiction',
    duration: '3h 15m',
    duration_minutes: 195,
    cover_url: '/covers/standalone.jpg',
  };

  it('should render book title', () => {
    render(<BookCard book={mockBookComplete} />);
    expect(screen.getByText('The Great Audiobook')).toBeInTheDocument();
  });

  it('should render author information', () => {
    render(<BookCard book={mockBookComplete} />);
    expect(screen.getByText(/John Doe/)).toBeInTheDocument();
    expect(screen.getByText(/Author:/)).toBeInTheDocument();
  });

  it('should render narrator information', () => {
    render(<BookCard book={mockBookComplete} />);
    expect(screen.getByText(/Jane Smith/)).toBeInTheDocument();
    expect(screen.getByText(/Narrator:/)).toBeInTheDocument();
  });

  it('should render series information when available', () => {
    render(<BookCard book={mockBookComplete} />);
    expect(screen.getByText('Epic Series #1')).toBeInTheDocument();
  });

  it('should not render series information when not available', () => {
    render(<BookCard book={mockBookNoSeries} />);
    expect(screen.queryByText(/Epic Series/)).not.toBeInTheDocument();
  });

  it('should render cover image with correct src', () => {
    render(<BookCard book={mockBookComplete} />);
    const img = screen.getByAltText('Cover of The Great Audiobook');
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute('src', 'http://localhost:5001/covers/test.jpg');
  });

  it('should call onClick handler when card is clicked', () => {
    const handleClick = vi.fn();
    render(<BookCard book={mockBookComplete} onClick={handleClick} />);
    
    const card = screen.getByRole('button');
    fireEvent.click(card);
    
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it('should handle keyboard navigation with Enter key', () => {
    const handleClick = vi.fn();
    render(<BookCard book={mockBookComplete} onClick={handleClick} />);
    
    const card = screen.getByRole('button');
    fireEvent.keyDown(card, { key: 'Enter' });
    
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it('should handle keyboard navigation with Space key', () => {
    const handleClick = vi.fn();
    render(<BookCard book={mockBookComplete} onClick={handleClick} />);
    
    const card = screen.getByRole('button');
    fireEvent.keyDown(card, { key: ' ' });
    
    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it('should not call onClick when no handler is provided', () => {
    render(<BookCard book={mockBookComplete} />);
    const card = screen.getByRole('button');
    
    expect(() => fireEvent.click(card)).not.toThrow();
  });

  it('should display fallback when image fails to load', () => {
    render(<BookCard book={mockBookComplete} />);
    
    const img = screen.getByAltText('Cover of The Great Audiobook');
    fireEvent.error(img);
    
    // After error, fallback should be displayed
    expect(screen.getByText('No Cover')).toBeInTheDocument();
    expect(screen.getByText('📚')).toBeInTheDocument();
  });

  it('should have proper accessibility attributes', () => {
    render(<BookCard book={mockBookComplete} />);
    
    const card = screen.getByRole('button');
    expect(card).toHaveAttribute('tabIndex', '0');
    expect(card).toHaveAttribute('aria-label', 'View details for The Great Audiobook by John Doe');
  });

  it('should render all required fields for any book', () => {
    render(<BookCard book={mockBookComplete} />);
    
    // Verify all required fields are present
    expect(screen.getByText('The Great Audiobook')).toBeInTheDocument();
    expect(screen.getByText(/John Doe/)).toBeInTheDocument();
    expect(screen.getByText(/Jane Smith/)).toBeInTheDocument();
    expect(screen.getByAltText('Cover of The Great Audiobook')).toBeInTheDocument();
  });
});
