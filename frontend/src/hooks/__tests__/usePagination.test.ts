import { describe, it, expect } from 'vitest';
import { usePagination } from '../usePagination';

describe('usePagination', () => {
  const testItems = Array.from({ length: 100 }, (_, i) => ({ id: i + 1 }));

  it('should return all items when pageSize is "all"', () => {
    const result = usePagination(testItems, 'all', 1);
    
    expect(result.displayedItems).toHaveLength(100);
    expect(result.totalPages).toBe(1);
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(99);
  });

  it('should calculate correct pagination for first page', () => {
    const result = usePagination(testItems, 25, 1);
    
    expect(result.displayedItems).toHaveLength(25);
    expect(result.displayedItems[0]).toEqual({ id: 1 });
    expect(result.displayedItems[24]).toEqual({ id: 25 });
    expect(result.totalPages).toBe(4);
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(24);
  });

  it('should calculate correct pagination for middle page', () => {
    const result = usePagination(testItems, 25, 2);
    
    expect(result.displayedItems).toHaveLength(25);
    expect(result.displayedItems[0]).toEqual({ id: 26 });
    expect(result.displayedItems[24]).toEqual({ id: 50 });
    expect(result.startIndex).toBe(25);
    expect(result.endIndex).toBe(49);
  });

  it('should calculate correct pagination for last page', () => {
    const result = usePagination(testItems, 25, 4);
    
    expect(result.displayedItems).toHaveLength(25);
    expect(result.displayedItems[0]).toEqual({ id: 76 });
    expect(result.displayedItems[24]).toEqual({ id: 100 });
    expect(result.startIndex).toBe(75);
    expect(result.endIndex).toBe(99);
  });

  it('should handle partial last page', () => {
    const result = usePagination(testItems, 30, 4);
    
    expect(result.displayedItems).toHaveLength(10);
    expect(result.displayedItems[0]).toEqual({ id: 91 });
    expect(result.displayedItems[9]).toEqual({ id: 100 });
    expect(result.totalPages).toBe(4);
    expect(result.startIndex).toBe(90);
    expect(result.endIndex).toBe(99);
  });

  it('should clamp page number to valid range', () => {
    const result = usePagination(testItems, 25, 999);
    
    // Should show last page (page 4)
    expect(result.displayedItems[0]).toEqual({ id: 76 });
    expect(result.totalPages).toBe(4);
  });

  it('should handle empty array', () => {
    const result = usePagination([], 25, 1);
    
    expect(result.displayedItems).toHaveLength(0);
    expect(result.totalPages).toBe(1);
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(-1);
  });

  it('should handle single item', () => {
    const result = usePagination([{ id: 1 }], 25, 1);
    
    expect(result.displayedItems).toHaveLength(1);
    expect(result.totalPages).toBe(1);
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(0);
  });
});
