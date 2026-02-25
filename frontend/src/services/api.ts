/**
 * API Service Module
 * 
 * Centralized module for all HTTP communication with the Flask backend API.
 * Provides functions for fetching audiobook data and handles errors consistently.
 * 
 * Base URL: http://localhost:5001/api
 * 
 * Requirements: 5.1, 5.2, 5.5
 */

import axios, { AxiosError } from 'axios';
import type { Book } from '../types/Book';

/**
 * Configure Axios instance with base URL and timeout
 */
const apiClient = axios.create({
  baseURL: 'http://localhost:5000/api',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
});

/**
 * Error handler for API requests
 * Extracts meaningful error messages from various error types
 * 
 * @param error - The error object from axios
 * @returns A user-friendly error message
 */
const handleApiError = (error: unknown): string => {
  if (axios.isAxiosError(error)) {
    const axiosError = error as AxiosError;
    
    if (axiosError.response) {
      // Server responded with error status
      const status = axiosError.response.status;
      if (status === 404) {
        return 'Resource not found';
      } else if (status === 500) {
        return 'Server error occurred';
      } else {
        return `Request failed with status ${status}`;
      }
    } else if (axiosError.request) {
      // Request made but no response received
      return 'Network error: Unable to reach the server';
    } else {
      // Error setting up the request
      return 'Request configuration error';
    }
  } else if (error instanceof Error) {
    return error.message;
  }
  
  return 'An unexpected error occurred';
};

/**
 * Fetch all books from the backend API
 * 
 * @returns Promise resolving to array of Book objects
 * @throws Error with user-friendly message if request fails
 * 
 * Endpoint: GET /api/books
 * Requirements: 5.1, 5.2
 */
export const getAllBooks = async (): Promise<Book[]> => {
  try {
    const response = await apiClient.get<Book[]>('/books');
    return response.data;
  } catch (error) {
    const errorMessage = handleApiError(error);
    throw new Error(`Failed to fetch books: ${errorMessage}`);
  }
};

/**
 * Fetch a single book by ID from the backend API
 * 
 * @param id - The unique identifier of the book
 * @returns Promise resolving to a Book object
 * @throws Error with user-friendly message if request fails or book not found
 * 
 * Endpoint: GET /api/books/{id}
 * Requirements: 5.1, 5.2
 */
export const getBookById = async (id: number): Promise<Book> => {
  try {
    const response = await apiClient.get<Book>(`/books/${id}`);
    return response.data;
  } catch (error) {
    const errorMessage = handleApiError(error);
    throw new Error(`Failed to fetch book with ID ${id}: ${errorMessage}`);
  }
};

/**
 * Search for books by query string
 * 
 * @param query - The search query string
 * @returns Promise resolving to array of matching Book objects
 * @throws Error with user-friendly message if request fails
 * 
 * Endpoint: GET /api/books/search?q={query}
 * Requirements: 5.1, 5.2
 */
export const searchBooks = async (query: string): Promise<Book[]> => {
  try {
    const response = await apiClient.get<Book[]>('/books/search', {
      params: { q: query },
    });
    return response.data;
  } catch (error) {
    const errorMessage = handleApiError(error);
    throw new Error(`Failed to search books: ${errorMessage}`);
  }
};

/**
 * Construct full URL for a book cover image
 * 
 * @param path - The relative path to the cover image
 * @returns Full URL to the cover image
 * 
 * Helper function to construct cover image URLs from relative paths.
 * The backend serves cover images from the base URL.
 * 
 * Requirements: 5.1
 */
export const getCoverUrl = (path: string): string => {
  // Remove leading slash if present to avoid double slashes
  const cleanPath = path.startsWith('/') ? path.slice(1) : path;
  return `http://localhost:5000/${cleanPath}`;
};

/**
 * Export the configured axios instance for advanced use cases
 */
export default apiClient;
