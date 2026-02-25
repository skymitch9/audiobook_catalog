/**
 * API Service Tests
 * 
 * Basic tests to verify the API service module is correctly configured
 * and exports the expected functions.
 */

import { describe, it, expect } from 'vitest';
import { getAllBooks, getBookById, searchBooks, getCoverUrl } from '../api';

describe('API Service', () => {
  describe('Module Exports', () => {
    it('should export getAllBooks function', () => {
      expect(getAllBooks).toBeDefined();
      expect(typeof getAllBooks).toBe('function');
    });

    it('should export getBookById function', () => {
      expect(getBookById).toBeDefined();
      expect(typeof getBookById).toBe('function');
    });

    it('should export searchBooks function', () => {
      expect(searchBooks).toBeDefined();
      expect(typeof searchBooks).toBe('function');
    });

    it('should export getCoverUrl function', () => {
      expect(getCoverUrl).toBeDefined();
      expect(typeof getCoverUrl).toBe('function');
    });
  });

  describe('getCoverUrl', () => {
    it('should construct full URL from relative path', () => {
      const path = 'covers/book1.jpg';
      const url = getCoverUrl(path);
      expect(url).toBe('http://localhost:5000/covers/book1.jpg');
    });

    it('should handle paths with leading slash', () => {
      const path = '/covers/book2.jpg';
      const url = getCoverUrl(path);
      expect(url).toBe('http://localhost:5000/covers/book2.jpg');
    });

    it('should handle empty path', () => {
      const path = '';
      const url = getCoverUrl(path);
      expect(url).toBe('http://localhost:5000/');
    });

    it('should handle paths with multiple segments', () => {
      const path = 'static/images/covers/book3.jpg';
      const url = getCoverUrl(path);
      expect(url).toBe('http://localhost:5000/static/images/covers/book3.jpg');
    });
  });
});
