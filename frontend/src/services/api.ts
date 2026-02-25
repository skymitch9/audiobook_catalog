/**
 * API Service Module
 * 
 * Centralized module for fetching audiobook data from static CSV file.
 * Provides functions for fetching audiobook data and handles errors consistently.
 * 
 * Base URL: /api (relative to Flask server)
 * 
 * Requirements: 5.1, 5.2, 5.5
 */

import type { Book } from '../types/Book';

/**
 * Get API base URL based on environment
 */
const getApiBaseUrl = (): string => {
  // In development with Vite proxy, use relative /api
  // In production with Flask serving React, use relative /api
  return '/api';
};

/**
 * Error handler for API requests
 * Extracts meaningful error messages from various error types
 */
const handleApiError = (error: unknown): string => {
  if (error instanceof Response) {
    return `Request failed with status ${error.status}`;
  } else if (error instanceof Error) {
    return error.message;
  }
  return 'An unexpected error occurred';
};

/**
 * Fetch all books from catalog.csv
 * 
 * @returns Promise resolving to array of Book objects
 * @throws Error with user-friendly message if request fails
 * 
 * Requirements: 5.1, 5.2
 */
export const getAllBooks = async (): Promise<Book[]> => {
  try {
    const response = await fetch(`${getApiBaseUrl()}/books`);
    
    if (!response.ok) {
      throw new Error(`Failed to fetch books: ${response.status} ${response.statusText}`);
    }
    
    return await response.json();
  } catch (error) {
    const errorMessage = handleApiError(error);
    throw new Error(`Failed to fetch books: ${errorMessage}`);
  }
};

/**
 * Fetch a single book by ID
 * 
 * @param id - The unique identifier of the book (row number)
 * @returns Promise resolving to a Book object
 * @throws Error with user-friendly message if book not found
 * 
 * Requirements: 5.1, 5.2
 */
export const getBookById = async (id: number): Promise<Book> => {
  try {
    const response = await fetch(`${getApiBaseUrl()}/books/${id}`);
    
    if (!response.ok) {
      throw new Error(`Failed to fetch book: ${response.status} ${response.statusText}`);
    }
    
    return await response.json();
  } catch (error) {
    const errorMessage = handleApiError(error);
    throw new Error(`Failed to fetch book with ID ${id}: ${errorMessage}`);
  }
};

/**
 * Search for books by query string (client-side filtering)
 * 
 * @param query - The search query string
 * @returns Promise resolving to array of matching Book objects
 * 
 * Requirements: 5.1, 5.2
 */
export const searchBooks = async (query: string): Promise<Book[]> => {
  try {
    const url = new URL(`${getApiBaseUrl()}/books/search`, window.location.origin);
    url.searchParams.set('q', query);
    
    const response = await fetch(url.toString());
    
    if (!response.ok) {
      throw new Error(`Failed to search books: ${response.status} ${response.statusText}`);
    }
    
    return await response.json();
  } catch (error) {
    const errorMessage = handleApiError(error);
    throw new Error(`Failed to search books: ${errorMessage}`);
  }
};

/**
 * Construct full URL for a book cover image
 * 
 * @param path - The relative path to the cover image (e.g., "covers/book.jpg")
 * @returns Full URL to the cover image served by Flask
 * 
 * Helper function to construct cover image URLs from relative paths.
 * The backend serves cover images from /api/covers/
 * 
 * Requirements: 5.1
 */
export const getCoverUrl = (path: string): string => {
  // If path already starts with /api/, return as-is
  if (path.startsWith('/api/')) {
    return path;
  }
  
  // Remove leading slash if present
  const cleanPath = path.startsWith('/') ? path.slice(1) : path;
  
  // Remove 'covers/' prefix if present (API endpoint already includes it)
  const filename = cleanPath.replace(/^covers\//, '');
  
  return `/api/covers/${filename}`;
};
