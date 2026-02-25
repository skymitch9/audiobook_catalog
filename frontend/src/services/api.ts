/**
 * API Service Module
 * 
 * Centralized module for fetching audiobook data from static CSV file.
 * Provides functions for fetching audiobook data and handles errors consistently.
 * 
 * For GitHub Pages deployment, data is loaded from catalog.csv
 * 
 * Requirements: 5.1, 5.2, 5.5
 */

import axios, { AxiosError } from 'axios';
import type { Book } from '../types/Book';

/**
 * Base URL for static assets (GitHub Pages or local)
 */
const BASE_URL = import.meta.env.BASE_URL || '/';

/**
 * Parse CSV text into array of Book objects
 */
const parseCSV = (csvText: string): Book[] => {
  const lines = csvText.trim().split('\n');
  if (lines.length < 2) return [];
  
  const headers = lines[0].split(',').map(h => h.trim().replace(/^"|"$/g, ''));
  const books: Book[] = [];
  
  for (let i = 1; i < lines.length; i++) {
    const values: string[] = [];
    let current = '';
    let inQuotes = false;
    
    // Parse CSV line handling quoted values
    for (let j = 0; j < lines[i].length; j++) {
      const char = lines[i][j];
      if (char === '"') {
        inQuotes = !inQuotes;
      } else if (char === ',' && !inQuotes) {
        values.push(current.trim());
        current = '';
      } else {
        current += char;
      }
    }
    values.push(current.trim());
    
    // Create book object
    const book: any = { id: i };
    headers.forEach((header, index) => {
      const value = values[index]?.replace(/^"|"$/g, '') || '';
      book[header] = value;
    });
    
    // Transform cover_href to cover_url with full path
    if (book.cover_href) {
      book.cover_url = `${BASE_URL}${book.cover_href}`;
    }
    
    books.push(book as Book);
  }
  
  return books;
};

/**
 * Fetch and parse catalog.csv
 */
let cachedBooks: Book[] | null = null;

const fetchCatalog = async (): Promise<Book[]> => {
  if (cachedBooks) return cachedBooks;
  
  try {
    const response = await axios.get(`${BASE_URL}catalog.csv`, {
      responseType: 'text',
      timeout: 10000,
    });
    cachedBooks = parseCSV(response.data);
    return cachedBooks;
  } catch (error) {
    console.error('Failed to fetch catalog:', error);
    throw new Error('Failed to load audiobook catalog');
  }
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
  return fetchCatalog();
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
  const books = await fetchCatalog();
  const book = books.find(b => b.id === id);
  if (!book) {
    throw new Error(`Book with ID ${id} not found`);
  }
  return book;
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
  const books = await fetchCatalog();
  const lowerQuery = query.toLowerCase();
  
  return books.filter(book => {
    const searchText = [
      book.title,
      book.author,
      book.narrator,
      book.series,
      book.genre,
    ].join(' ').toLowerCase();
    
    return searchText.includes(lowerQuery);
  });
};

/**
 * Construct full URL for a book cover image
 * 
 * @param path - The relative path to the cover image
 * @returns Full URL to the cover image
 * 
 * Requirements: 5.1
 */
export const getCoverUrl = (path: string): string => {
  const cleanPath = path.startsWith('/') ? path.slice(1) : path;
  return `${BASE_URL}${cleanPath}`;
};
