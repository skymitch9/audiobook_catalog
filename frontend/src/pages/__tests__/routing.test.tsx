/**
 * Routing Integration Tests
 * 
 * Tests to verify that routing and navigation work correctly between pages.
 * This is part of the checkpoint to ensure page components integrate properly.
 * 
 * Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import CatalogPage from '../CatalogPage';
import DetailPage from '../DetailPage';
import Header from '../../components/Header';
import * as api from '../../services/api';

// Mock the API module
vi.mock('../../services/api', () => ({
  getAllBooks: vi.fn(),
  getBookById: vi.fn(),
  searchBooks: vi.fn(),
  getCoverUrl: vi.fn((path: string) => `http://localhost:5001/${path}`),
}));

describe('Routing Integration', () => {
  const mockBooks = [
    {
      id: 1,
      title: 'Test Book 1',
      author: 'Author 1',
      narrator: 'Narrator 1',
      series: 'Series 1',
      series_index: '1',
      year: '2024',
      genre: 'Fiction',
      duration: '5h 30m',
      duration_minutes: 330,
      cover_url: 'covers/book1.jpg',
      description: 'Test description 1',
    },
    {
      id: 2,
      title: 'Test Book 2',
      author: 'Author 2',
      narrator: 'Narrator 2',
      series: '',
      series_index: '',
      year: '2023',
      genre: 'Non-Fiction',
      duration: '3h 15m',
      duration_minutes: 195,
      cover_url: 'covers/book2.jpg',
      description: 'Test description 2',
    },
  ];

  it('should render CatalogPage at root path', async () => {
    vi.mocked(api.getAllBooks).mockResolvedValue(mockBooks);

    render(
      <MemoryRouter initialEntries={['/']}>
        <Header />
        <Routes>
          <Route path="/" element={<CatalogPage />} />
        </Routes>
      </MemoryRouter>
    );

    // Wait for books to load
    await waitFor(() => {
      expect(screen.getByText('Audiobook Catalog')).toBeInTheDocument();
    });

    // Verify books are displayed
    expect(screen.getByText('Test Book 1')).toBeInTheDocument();
    expect(screen.getByText('Test Book 2')).toBeInTheDocument();
  });

  it('should render DetailPage at /book/:id path', async () => {
    vi.mocked(api.getBookById).mockResolvedValue(mockBooks[0]);

    render(
      <MemoryRouter initialEntries={['/book/1']}>
        <Header />
        <Routes>
          <Route path="/book/:id" element={<DetailPage />} />
        </Routes>
      </MemoryRouter>
    );

    // Wait for book details to load
    await waitFor(() => {
      expect(screen.getByText('Test Book 1')).toBeInTheDocument();
    });

    // Verify book details are displayed
    expect(screen.getByText('Author 1')).toBeInTheDocument();
    expect(screen.getByText('Narrator 1')).toBeInTheDocument();
  });

  it('should render Header on all routes', async () => {
    vi.mocked(api.getAllBooks).mockResolvedValue(mockBooks);

    const { rerender } = render(
      <MemoryRouter initialEntries={['/']}>
        <Header />
        <Routes>
          <Route path="/" element={<CatalogPage />} />
        </Routes>
      </MemoryRouter>
    );

    // Header should be visible on catalog page
    expect(screen.getByText('Audiobook Catalog')).toBeInTheDocument();
    expect(screen.getByRole('banner')).toBeInTheDocument();

    // Rerender with detail page
    vi.mocked(api.getBookById).mockResolvedValue(mockBooks[0]);
    
    rerender(
      <MemoryRouter initialEntries={['/book/1']}>
        <Header />
        <Routes>
          <Route path="/book/:id" element={<DetailPage />} />
        </Routes>
      </MemoryRouter>
    );

    // Header should still be visible on detail page
    await waitFor(() => {
      expect(screen.getByRole('banner')).toBeInTheDocument();
    });
  });

  it('should handle loading states correctly', async () => {
    // Create a promise that we can control
    let resolveBooks: (value: any) => void;
    const booksPromise = new Promise((resolve) => {
      resolveBooks = resolve;
    });

    vi.mocked(api.getAllBooks).mockReturnValue(booksPromise as any);

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<CatalogPage />} />
        </Routes>
      </MemoryRouter>
    );

    // Should show loading indicator
    expect(screen.getByText('Loading audiobooks...')).toBeInTheDocument();

    // Resolve the promise
    resolveBooks!(mockBooks);

    // Wait for books to appear
    await waitFor(() => {
      expect(screen.getByText('Test Book 1')).toBeInTheDocument();
    });

    // Loading indicator should be gone
    expect(screen.queryByText('Loading audiobooks...')).not.toBeInTheDocument();
  });

  it('should handle error states correctly', async () => {
    vi.mocked(api.getAllBooks).mockRejectedValue(new Error('Network error'));

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<CatalogPage />} />
        </Routes>
      </MemoryRouter>
    );

    // Wait for error message to appear
    await waitFor(() => {
      expect(screen.getByText(/Network error/i)).toBeInTheDocument();
    });

    // Should show retry button
    expect(screen.getByText('Retry')).toBeInTheDocument();
  });

  it('should handle invalid book ID in DetailPage', async () => {
    render(
      <MemoryRouter initialEntries={['/book/invalid']}>
        <Routes>
          <Route path="/book/:id" element={<DetailPage />} />
        </Routes>
      </MemoryRouter>
    );

    // Wait for error message
    await waitFor(() => {
      expect(screen.getByText(/Invalid book ID/i)).toBeInTheDocument();
    });

    // Should show back button
    expect(screen.getByText('← Back to Catalog')).toBeInTheDocument();
  });
});
