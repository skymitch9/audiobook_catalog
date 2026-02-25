import { describe, it, expect } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useSort } from '../useSort';
import type { Book } from '../../types/Book';

describe('useSort', () => {
  const testBooks: Book[] = [
    {
      id: 1,
      title: 'Zebra Book',
      author: 'Charlie Author',
      year: '2020',
      duration: '5h 30m',
      duration_minutes: 330,
      series: 'Series B',
      series_index: '1',
      narrator: 'Narrator A',
      genre: 'Fiction',
      cover_url: '/cover1.jpg',
    },
    {
      id: 2,
      title: 'Alpha Book',
      author: 'Bob Author',
      year: '2022',
      duration: '3h 15m',
      duration_minutes: 195,
      series: 'Series A',
      series_index: '2',
      narrator: 'Narrator B',
      genre: 'Fantasy',
      cover_url: '/cover2.jpg',
    },
    {
      id: 3,
      title: 'Middle Book',
      author: 'Alice Author',
      year: '2018',
      duration: '8h 45m',
      duration_minutes: 525,
      series: 'Series C',
      series_index: '1',
      narrator: 'Narrator C',
      genre: 'Mystery',
      cover_url: '/cover3.jpg',
    },
  ];

  it('should sort by title ascending', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'title', direction: 'asc' })
    );

    expect(result.current[0].title).toBe('Alpha Book');
    expect(result.current[1].title).toBe('Middle Book');
    expect(result.current[2].title).toBe('Zebra Book');
  });

  it('should sort by title descending', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'title', direction: 'desc' })
    );

    expect(result.current[0].title).toBe('Zebra Book');
    expect(result.current[1].title).toBe('Middle Book');
    expect(result.current[2].title).toBe('Alpha Book');
  });

  it('should sort by author ascending', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'author', direction: 'asc' })
    );

    expect(result.current[0].author).toBe('Alice Author');
    expect(result.current[1].author).toBe('Bob Author');
    expect(result.current[2].author).toBe('Charlie Author');
  });

  it('should sort by author descending', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'author', direction: 'desc' })
    );

    expect(result.current[0].author).toBe('Charlie Author');
    expect(result.current[1].author).toBe('Bob Author');
    expect(result.current[2].author).toBe('Alice Author');
  });

  it('should sort by year ascending (oldest first)', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'year', direction: 'asc' })
    );

    expect(result.current[0].year).toBe('2018');
    expect(result.current[1].year).toBe('2020');
    expect(result.current[2].year).toBe('2022');
  });

  it('should sort by year descending (newest first)', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'year', direction: 'desc' })
    );

    expect(result.current[0].year).toBe('2022');
    expect(result.current[1].year).toBe('2020');
    expect(result.current[2].year).toBe('2018');
  });

  it('should sort by duration ascending (shortest first)', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'duration', direction: 'asc' })
    );

    expect(result.current[0].duration_minutes).toBe(195);
    expect(result.current[1].duration_minutes).toBe(330);
    expect(result.current[2].duration_minutes).toBe(525);
  });

  it('should sort by duration descending (longest first)', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'duration', direction: 'desc' })
    );

    expect(result.current[0].duration_minutes).toBe(525);
    expect(result.current[1].duration_minutes).toBe(330);
    expect(result.current[2].duration_minutes).toBe(195);
  });

  it('should sort by series ascending', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'series', direction: 'asc' })
    );

    expect(result.current[0].series).toBe('Series A');
    expect(result.current[1].series).toBe('Series B');
    expect(result.current[2].series).toBe('Series C');
  });

  it('should sort by series descending', () => {
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'series', direction: 'desc' })
    );

    expect(result.current[0].series).toBe('Series C');
    expect(result.current[1].series).toBe('Series B');
    expect(result.current[2].series).toBe('Series A');
  });

  it('should handle empty array', () => {
    const { result } = renderHook(() =>
      useSort([], { field: 'title', direction: 'asc' })
    );

    expect(result.current).toHaveLength(0);
  });

  it('should not mutate original array', () => {
    const original = [...testBooks];
    const { result } = renderHook(() =>
      useSort(testBooks, { field: 'title', direction: 'asc' })
    );

    // Original array should remain unchanged
    expect(testBooks[0].id).toBe(original[0].id);
    expect(testBooks[1].id).toBe(original[1].id);
    expect(testBooks[2].id).toBe(original[2].id);

    // Sorted result should be different
    expect(result.current[0].id).not.toBe(testBooks[0].id);
  });

  it('should handle case-insensitive string sorting', () => {
    const mixedCaseBooks = [
      { ...testBooks[0], title: 'ZEBRA' },
      { ...testBooks[1], title: 'alpha' },
      { ...testBooks[2], title: 'Middle' },
    ];

    const { result } = renderHook(() =>
      useSort(mixedCaseBooks, { field: 'title', direction: 'asc' })
    );

    expect(result.current[0].title).toBe('alpha');
    expect(result.current[1].title).toBe('Middle');
    expect(result.current[2].title).toBe('ZEBRA');
  });
});
