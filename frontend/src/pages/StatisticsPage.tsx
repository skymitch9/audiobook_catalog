import { useState, useEffect } from 'react';
import { getAllBooks } from '../services/api';
import { calculateStatistics, formatListeningTime, type Statistics } from '../services/statistics';
import './StatisticsPage.css';

/**
 * StatisticsPage Component
 * 
 * Display comprehensive statistics about the audiobook catalog.
 * 
 * Features:
 * - Total books count
 * - Total listening time (hours and days)
 * - Average book duration
 * - Top 10 authors, narrators, and genres
 * - Loading and error states
 * 
 * Requirements: 1.1-1.8
 */
function StatisticsPage() {
  const [stats, setStats] = useState<Statistics | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchBooks = async () => {
      try {
        setLoading(true);
        setError(null);
        const data = await getAllBooks();
        const calculatedStats = calculateStatistics(data);
        setStats(calculatedStats);
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Failed to load statistics';
        setError(errorMessage);
      } finally {
        setLoading(false);
      }
    };

    fetchBooks();
  }, []);

  if (loading) {
    return (
      <div className="statistics-page">
        <div className="statistics-loading">
          <div className="loading-spinner"></div>
          <p>Loading statistics...</p>
        </div>
      </div>
    );
  }

  if (error || !stats) {
    return (
      <div className="statistics-page">
        <div className="statistics-error">
          <p className="error-message">⚠️ {error || 'No statistics available'}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="statistics-page">
      <h1 className="statistics-title">📊 Catalog Statistics</h1>
      
      <div className="statistics-grid">
        <div className="stat-card">
          <div className="stat-icon">📚</div>
          <div className="stat-value">{stats.basic.totalBooks.toLocaleString()}</div>
          <div className="stat-label">Total Books</div>
        </div>

        <div className="stat-card">
          <div className="stat-icon">⏱️</div>
          <div className="stat-value">{stats.basic.totalHours.toLocaleString()}</div>
          <div className="stat-label">Total Hours</div>
          <div className="stat-sublabel">{stats.basic.totalDays} days</div>
        </div>

        <div className="stat-card">
          <div className="stat-icon">✍️</div>
          <div className="stat-value">{stats.basic.uniqueAuthors.toLocaleString()}</div>
          <div className="stat-label">Unique Authors</div>
        </div>

        <div className="stat-card">
          <div className="stat-icon">🎙️</div>
          <div className="stat-value">{stats.basic.uniqueNarrators.toLocaleString()}</div>
          <div className="stat-label">Unique Narrators</div>
        </div>

        <div className="stat-card">
          <div className="stat-icon">📖</div>
          <div className="stat-value">{stats.basic.uniqueSeries.toLocaleString()}</div>
          <div className="stat-label">Unique Series</div>
        </div>

        <div className="stat-card">
          <div className="stat-icon">📊</div>
          <div className="stat-value">{stats.basic.avgDurationHours}</div>
          <div className="stat-label">Average Duration</div>
          <div className="stat-sublabel">hours per book</div>
        </div>
      </div>

      <div className="insights">
        <h3>📋 Collection Insights</h3>
        <div className="insight-item">
          <div className="insight-label">Listening Marathon</div>
          <div className="insight-value">
            It would take {formatListeningTime(stats)} to listen to your entire collection!
          </div>
        </div>
        <div className="insight-item">
          <div className="insight-label">Author Diversity</div>
          <div className="insight-value">
            You have an average of {stats.insights.booksPerAuthor} books per author.
          </div>
        </div>
        <div className="insight-item">
          <div className="insight-label">Series Collection</div>
          <div className="insight-value">
            {stats.insights.seriesPercentage}% of your books are part of a series.
          </div>
        </div>
        <div className="insight-item">
          <div className="insight-label">Narrator Preference</div>
          <div className="insight-value">
            You have an average of {stats.insights.booksPerNarrator} books per narrator.
          </div>
        </div>
      </div>

      <div className="top-lists-grid">
        <div className="top-list">
          <h3>📚 Top Authors</h3>
          {stats.topAuthors.map(([author, count]) => (
            <div key={author} className="top-item">
              <span className="top-name">{author}</span>
              <span className="top-count">{count} books</span>
            </div>
          ))}
        </div>

        <div className="top-list">
          <h3>🎙️ Top Narrators</h3>
          {stats.topNarrators.map(([narrator, count]) => (
            <div key={narrator} className="top-item">
              <span className="top-name">{narrator}</span>
              <span className="top-count">{count} books</span>
            </div>
          ))}
        </div>

        <div className="top-list">
          <h3>📖 Top Series</h3>
          {stats.topSeries.map(([series, count]) => (
            <div key={series} className="top-item">
              <span className="top-name">{series}</span>
              <span className="top-count">{count} books</span>
            </div>
          ))}
        </div>

        <div className="top-list">
          <h3>🎭 Top Genres</h3>
          {stats.topGenres.map(([genre, count]) => (
            <div key={genre} className="top-item">
              <span className="top-name">{genre}</span>
              <span className="top-count">{count} books</span>
            </div>
          ))}
        </div>
      </div>

      <div className="top-list">
        <h3>⏱️ Duration Categories</h3>
        {Object.entries(stats.durationCategories).map(([category, count]) => {
          const percentage = stats.basic.totalBooks > 0
            ? Math.round((count / stats.basic.totalBooks) * 1000) / 10
            : 0;
          return (
            <div key={category} className="top-item">
              <span className="top-name">{category}</span>
              <span className="top-count">{count} books ({percentage}%)</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default StatisticsPage;
