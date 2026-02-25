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
    it('should construct API URL from cover path', () => {
      const path = 'covers/book1.jpg';
      const url = getCoverUrl(path);
      expect(url).toBe('/api/covers/book1.jpg');
    });

    it('should handle paths with leading slash', () => {
      const path = '/covers/book2.jpg';
      const url = getCoverUrl(path);
      expect(url).toBe('/api/covers/book2.jpg');
    });

    it('should handle paths that already have /api/ prefix', () => {
      const path = '/api/covers/book3.jpg';
      const url = getCoverUrl(path);
      expect(url).toBe('/api/covers/book3.jpg');
    });

    it('should strip covers/ prefix from filename', () => {
      const path = 'covers/subfolder/book4.jpg';
      const url = getCoverUrl(path);
      expect(url).toBe('/api/covers/subfolder/book4.jpg');
    });
  });
});
