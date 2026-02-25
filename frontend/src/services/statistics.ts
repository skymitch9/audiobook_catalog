/**
 * Statistics Service Module
 * 
 * Calculates comprehensive statistics from the audiobook catalog.
 * Provides functions for analyzing book data and generating insights.
 * 
 * Requirements: 1.1-1.6
 */

import type { Book } from '../types/Book';

/**
 * Statistics data structure
 */
export interface Statistics {
  basic: {
    totalBooks: number;
    totalHours: number;
    totalMinutes: number;
    totalDays: number;
    avgDurationHours: number;
    uniqueAuthors: number;
    uniqueNarrators: number;
    uniqueSeries: number;
    uniqueGenres: number;
    yearRange: string;
  };
  topAuthors: Array<[string, number]>;
  topNarrators: Array<[string, number]>;
  topSeries: Array<[string, number]>;
  topGenres: Array<[string, number]>;
  durationCategories: Record<string, number>;
  listeningTime: {
    days: number;
    weeks: number;
    months: number;
    years: number;
  };
  insights: {
    booksPerAuthor: number;
    booksPerNarrator: number;
    seriesPercentage: number;
    avgBooksPerSeries: number;
  };
}

/**
 * Calculate comprehensive statistics from book array
 * 
 * @param books - Array of Book objects
 * @returns Statistics object with all calculated metrics
 * 
 * Requirements: 1.1-1.6
 */
export const calculateStatistics = (books: Book[]): Statistics => {
  if (books.length === 0) {
    return {
      basic: {
        totalBooks: 0,
        totalHours: 0,
        totalMinutes: 0,
        totalDays: 0,
        avgDurationHours: 0,
        uniqueAuthors: 0,
        uniqueNarrators: 0,
        uniqueSeries: 0,
        uniqueGenres: 0,
        yearRange: 'N/A',
      },
      topAuthors: [],
      topNarrators: [],
      topSeries: [],
      topGenres: [],
      durationCategories: {},
      listeningTime: { days: 0, weeks: 0, months: 0, years: 0 },
      insights: {
        booksPerAuthor: 0,
        booksPerNarrator: 0,
        seriesPercentage: 0,
        avgBooksPerSeries: 0,
      },
    };
  }

  // Basic counts
  const totalBooks = books.length;
  const totalMinutes = books.reduce((sum, book) => sum + (book.duration_minutes || 0), 0);
  const totalHours = Math.floor(totalMinutes / 60);
  const avgDurationMinutes = Math.floor(totalMinutes / totalBooks);

  // Unique counts
  const authors = new Set(books.map(b => b.author).filter(a => a && a.trim()));
  const narrators = new Set(books.map(b => b.narrator).filter(n => n && n.trim()));
  const series = new Set(books.map(b => b.series).filter(s => s && s.trim()));
  const genres = new Set(books.map(b => b.genre).filter(g => g && g.trim()));
  const years = books.map(b => b.year).filter(y => y && y.trim());

  // Top lists using counters
  const authorCounts = countOccurrences(books.map(b => b.author).filter(a => a && a.trim()));
  const narratorCounts = countOccurrences(books.map(b => b.narrator).filter(n => n && n.trim()));
  const seriesCounts = countOccurrences(books.map(b => b.series).filter(s => s && s.trim()));
  const genreCounts = countOccurrences(books.map(b => b.genre).filter(g => g && g.trim()));

  // Duration categories
  const durationCategories: Record<string, number> = {
    'Novella (< 5h)': 0,
    'Short (5-10h)': 0,
    'Medium (11-15h)': 0,
    'Long (16-24h)': 0,
    'Extra Long (25h+)': 0,
  };

  books.forEach(book => {
    const hours = (book.duration_minutes || 0) / 60;
    if (hours < 5) {
      durationCategories['Novella (< 5h)']++;
    } else if (hours <= 10) {
      durationCategories['Short (5-10h)']++;
    } else if (hours <= 15) {
      durationCategories['Medium (11-15h)']++;
    } else if (hours <= 24) {
      durationCategories['Long (16-24h)']++;
    } else {
      durationCategories['Extra Long (25h+)']++;
    }
  });

  // Series analysis
  const seriesBooks = new Map<string, Book[]>();
  books.forEach(book => {
    if (book.series && book.series.trim()) {
      const seriesName = book.series.trim();
      if (!seriesBooks.has(seriesName)) {
        seriesBooks.set(seriesName, []);
      }
      seriesBooks.get(seriesName)!.push(book);
    }
  });

  // Calculate listening time estimates
  const daysTotal = totalHours / 24;
  const weeksTotal = daysTotal / 7;
  const monthsTotal = daysTotal / 30;
  const yearsTotal = daysTotal / 365;

  // Year range
  const yearNumbers = years.map(y => parseInt(y)).filter(y => !isNaN(y));
  const yearRange = yearNumbers.length > 0
    ? `${Math.min(...yearNumbers)} - ${Math.max(...yearNumbers)}`
    : 'N/A';

  return {
    basic: {
      totalBooks,
      totalHours,
      totalMinutes,
      totalDays: Math.round(daysTotal * 10) / 10,
      avgDurationHours: Math.round((avgDurationMinutes / 60) * 10) / 10,
      uniqueAuthors: authors.size,
      uniqueNarrators: narrators.size,
      uniqueSeries: series.size,
      uniqueGenres: genres.size,
      yearRange,
    },
    topAuthors: getTopN(authorCounts, 10),
    topNarrators: getTopN(narratorCounts, 10),
    topSeries: getTopN(seriesCounts, 10),
    topGenres: getTopN(genreCounts, 10),
    durationCategories,
    listeningTime: {
      days: Math.round(daysTotal * 10) / 10,
      weeks: Math.round(weeksTotal * 10) / 10,
      months: Math.round(monthsTotal * 10) / 10,
      years: Math.round(yearsTotal * 100) / 100,
    },
    insights: {
      booksPerAuthor: authors.size > 0 ? Math.round((totalBooks / authors.size) * 10) / 10 : 0,
      booksPerNarrator: narrators.size > 0 ? Math.round((totalBooks / narrators.size) * 10) / 10 : 0,
      seriesPercentage: totalBooks > 0 ? Math.round((seriesBooks.size / totalBooks) * 1000) / 10 : 0,
      avgBooksPerSeries: seriesBooks.size > 0
        ? Math.round((Array.from(seriesBooks.values()).reduce((sum, books) => sum + books.length, 0) / seriesBooks.size) * 10) / 10
        : 0,
    },
  };
};

/**
 * Count occurrences of items in an array
 * 
 * @param items - Array of strings to count
 * @returns Map of item to count
 */
const countOccurrences = (items: string[]): Map<string, number> => {
  const counts = new Map<string, number>();
  items.forEach(item => {
    counts.set(item, (counts.get(item) || 0) + 1);
  });
  return counts;
};

/**
 * Get top N items from a count map
 * 
 * @param counts - Map of item to count
 * @param n - Number of top items to return
 * @returns Array of [item, count] tuples sorted by count descending
 */
const getTopN = (counts: Map<string, number>, n: number): Array<[string, number]> => {
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, n);
};

/**
 * Format listening time in human-readable format
 * 
 * @param stats - Statistics object
 * @returns Formatted listening time string
 */
export const formatListeningTime = (stats: Statistics): string => {
  const { years, months, weeks, days } = stats.listeningTime;
  
  if (years >= 1) {
    return `${years} years (${months.toFixed(1)} months)`;
  } else if (months >= 1) {
    return `${months.toFixed(1)} months (${weeks.toFixed(1)} weeks)`;
  } else if (weeks >= 1) {
    return `${weeks.toFixed(1)} weeks (${days.toFixed(1)} days)`;
  } else {
    return `${days.toFixed(1)} days`;
  }
};
