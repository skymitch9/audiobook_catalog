import { useState, useEffect } from 'react';
import type { Book } from '../types/Book';
import { getAllBooks } from '../services/api';
import { submitGameResult, getLeaderboard, getPlayerStats, type PlayerScore } from '../services/leaderboard';
import './GamePage.css';

/**
 * GamePage Component
 * 
 * Interactive game where users guess the duration of audiobooks.
 * 
 * Features:
 * - Random book selection
 * - Duration guessing in hours only (rounded)
 * - 3 attempts per book with visual indicators
 * - Hints based on book length category
 * - Feedback on guesses (higher/lower + closeness)
 * - Score and streak tracking
 * 
 * Requirements: 2.1-2.9
 */
function GamePage() {
  const [books, setBooks] = useState<Book[]>([]);
  const [currentBook, setCurrentBook] = useState<Book | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [score, setScore] = useState<number>(0);
  const [streak, setStreak] = useState<number>(0);
  const [guessHours, setGuessHours] = useState<string>('');
  const [feedback, setFeedback] = useState<string | null>(null);
  const [showAnswer, setShowAnswer] = useState<boolean>(false);
  const [attemptsLeft, setAttemptsLeft] = useState<number>(3);
  const [playerName, setPlayerName] = useState<string>('');
  const [showNamePrompt, setShowNamePrompt] = useState<boolean>(false);
  const [leaderboard, setLeaderboard] = useState<PlayerScore[]>([]);
  const [playerStats, setPlayerStats] = useState<PlayerScore | null>(null);
  const [showLeaderboard, setShowLeaderboard] = useState<boolean>(false);

  useEffect(() => {
    const fetchBooks = async () => {
      try {
        setLoading(true);
        setError(null);
        const data = await getAllBooks();
        setBooks(data);
        if (data.length > 0) {
          selectRandomBook(data);
        }
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Failed to load game';
        setError(errorMessage);
      } finally {
        setLoading(false);
      }
    };

    fetchBooks();

    // Load player name from localStorage
    const savedName = localStorage.getItem('audiobook-game-player');
    if (savedName) {
      setPlayerName(savedName);
      loadPlayerStats(savedName);
    } else {
      setShowNamePrompt(true);
    }

    // Load leaderboard
    loadLeaderboard();
  }, []);

  const loadLeaderboard = async () => {
    try {
      const data = await getLeaderboard(10);
      setLeaderboard(data);
    } catch (err) {
      console.error('Failed to load leaderboard:', err);
    }
  };

  const loadPlayerStats = async (name: string) => {
    try {
      const stats = await getPlayerStats(name);
      setPlayerStats(stats);
    } catch (err) {
      console.error('Failed to load player stats:', err);
    }
  };

  const handleSetPlayerName = (name: string) => {
    const trimmedName = name.trim();
    if (trimmedName) {
      setPlayerName(trimmedName);
      localStorage.setItem('audiobook-game-player', trimmedName);
      setShowNamePrompt(false);
      loadPlayerStats(trimmedName);
    }
  };

  const parseToHours = (durationMinutes: number): number => {
    const hours = Math.floor(durationMinutes / 60);
    const minutes = durationMinutes % 60;
    // Round: >= 30 minutes rounds up, < 30 rounds down
    return minutes >= 30 ? hours + 1 : Math.max(hours, 1);
  };

  const getHint = (hours: number): string => {
    if (hours < 5) return "📖 Novella";
    if (hours <= 10) return "📚 Short";
    if (hours <= 15) return "📚 Medium";
    if (hours <= 24) return "📚📚 Long";
    // For extra long, give a range hint
    const rangeStart = Math.floor(hours / 15) * 15;
    const rangeEnd = rangeStart + 15;
    return `📚📚📚 Extra Long (${rangeStart}-${rangeEnd}h)`;
  };

  const selectRandomBook = (bookList: Book[]) => {
    const randomIndex = Math.floor(Math.random() * bookList.length);
    setCurrentBook(bookList[randomIndex]);
    setGuessHours('');
    setFeedback(null);
    setShowAnswer(false);
    setAttemptsLeft(3);
  };

  const handleGuess = async () => {
    if (!currentBook) return;

    const guess = parseInt(guessHours);
    if (!guess || guess < 1) {
      alert('Please enter a valid number of hours!');
      return;
    }

    const actualHours = parseToHours(currentBook.duration_minutes || 0);
    const newAttemptsLeft = attemptsLeft - 1;
    setAttemptsLeft(newAttemptsLeft);

    if (guess === actualHours) {
      // Correct!
      const bonusPoints = actualHours >= 25 ? 2 : 1;
      const newScore = score + bonusPoints;
      const newStreak = streak + 1;
      setScore(newScore);
      setStreak(newStreak);
      const bonusText = bonusPoints > 1 ? `<br/>+${bonusPoints} bonus points for Extra Long book! 🎉` : '';
      setFeedback(`🎉 Correct! It's ${actualHours} hours!${bonusText}<br/>Streak: ${newStreak} 🔥`);
      setShowAnswer(true);

      // Submit to leaderboard
      if (playerName) {
        try {
          await submitGameResult(playerName, true, newStreak, bonusPoints);
          await loadLeaderboard();
          await loadPlayerStats(playerName);
        } catch (err) {
          console.error('Failed to submit score:', err);
        }
      }
    } else if (newAttemptsLeft === 0) {
      // Out of guesses
      setStreak(0);
      setFeedback(`❌ Out of guesses! The answer was ${actualHours} hours.`);
      setShowAnswer(true);

      // Submit wrong answer to leaderboard
      if (playerName) {
        try {
          await submitGameResult(playerName, false, 0, 0);
          await loadLeaderboard();
          await loadPlayerStats(playerName);
        } catch (err) {
          console.error('Failed to submit result:', err);
        }
      }
    } else {
      // Wrong but has attempts left
      const diff = Math.abs(guess - actualHours);
      let hint = '';
      if (diff <= 2) hint = '🔥 Very close!';
      else if (diff <= 5) hint = '🎯 Getting warmer!';
      else hint = '❄️ Not quite!';

      const direction = guess > actualHours ? 'Lower!' : 'Higher!';
      setFeedback(`${hint} ${direction}<br/>${newAttemptsLeft} ${newAttemptsLeft === 1 ? 'guess' : 'guesses'} left!`);
      setGuessHours('');
    }
  };

  const handleNext = () => {
    selectRandomBook(books);
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !showAnswer) {
      handleGuess();
    }
  };

  if (loading) {
    return (
      <div className="game-page">
        <div className="game-loading">
          <div className="loading-spinner"></div>
          <p>Loading game...</p>
        </div>
      </div>
    );
  }

  if (error || !currentBook) {
    return (
      <div className="game-page">
        <div className="game-error">
          <p className="error-message">⚠️ {error || 'No books available'}</p>
        </div>
      </div>
    );
  }

  const actualHours = parseToHours(currentBook.duration_minutes || 0);
  const hint = getHint(actualHours);

  return (
    <div className="game-page">
      {showNamePrompt && (
        <div className="name-prompt-overlay">
          <div className="name-prompt-modal">
            <h2>Welcome to Guess That Audiobook!</h2>
            <p>Enter your name to join the leaderboard:</p>
            <input
              type="text"
              placeholder="Your name"
              autoFocus
              onKeyPress={(e) => {
                if (e.key === 'Enter') {
                  handleSetPlayerName((e.target as HTMLInputElement).value);
                }
              }}
            />
            <button
              onClick={(e) => {
                const input = e.currentTarget.previousElementSibling as HTMLInputElement;
                handleSetPlayerName(input.value);
              }}
              className="btn btn-primary"
            >
              Start Playing
            </button>
          </div>
        </div>
      )}

      <div className="game-header">
        <div>
          <h1 className="game-title">🎧 Guess That Audiobook!</h1>
          <p className="game-subtitle">Can you guess how long this audiobook is?</p>
        </div>
        <button
          onClick={() => setShowLeaderboard(!showLeaderboard)}
          className="btn btn-secondary leaderboard-toggle"
        >
          {showLeaderboard ? '🎮 Play' : '🏆 Leaderboard'}
        </button>
      </div>

      {showLeaderboard ? (
        <div className="leaderboard-view">
          <h2>🏆 Weekly Leaderboard</h2>
          {playerStats && (
            <div className="player-stats-card">
              <h3>Your Stats This Week</h3>
              <div className="stats-grid">
                <div className="stat-item">
                  <span className="stat-label">Score</span>
                  <span className="stat-value">{playerStats.score}</span>
                </div>
                <div className="stat-item">
                  <span className="stat-label">Correct</span>
                  <span className="stat-value">{playerStats.correct}</span>
                </div>
                <div className="stat-item">
                  <span className="stat-label">Wrong</span>
                  <span className="stat-value">{playerStats.wrong}</span>
                </div>
                <div className="stat-item">
                  <span className="stat-label">Best Streak</span>
                  <span className="stat-value">{playerStats.bestStreak}</span>
                </div>
              </div>
            </div>
          )}
          <div className="leaderboard-table">
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Player</th>
                  <th>Score</th>
                  <th>Correct</th>
                  <th>Best Streak</th>
                </tr>
              </thead>
              <tbody>
                {leaderboard.length === 0 ? (
                  <tr>
                    <td colSpan={5} style={{ textAlign: 'center', padding: '2rem' }}>
                      No scores yet this week. Be the first!
                    </td>
                  </tr>
                ) : (
                  leaderboard.map((player, index) => (
                    <tr key={player.playerName} className={player.playerName === playerName ? 'current-player' : ''}>
                      <td>{index + 1}</td>
                      <td>{player.playerName}</td>
                      <td>{player.score}</td>
                      <td>{player.correct}</td>
                      <td>{player.bestStreak}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <p className="leaderboard-note">
            Leaderboard resets every Monday. Score = (Points × 10) + (Best Streak × 5)
          </p>
        </div>
      ) : (
        <>
          <div className="game-stats">
            <div className="stat">
              <span className="stat-label">Correct</span>
              <span className="stat-value">{score}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Streak</span>
              <span className="stat-value">{streak}</span>
            </div>
            {playerName && (
              <div className="stat">
                <span className="stat-label">Player</span>
                <span className="stat-value">{playerName}</span>
              </div>
            )}
          </div>

      <div className="game-card">
        <div className="game-cover">
          <img src={currentBook.cover_url} alt={`Cover of ${currentBook.title}`} />
        </div>

        <div className="hint-badge">{hint}</div>

        <div className="attempts-display">
          {[1, 2, 3].map((i) => (
            <span
              key={i}
              className={`attempt-dot ${i > attemptsLeft ? 'used' : ''}`}
            />
          ))}
        </div>

        {!showAnswer ? (
          <div className="game-guess">
            <p className="guess-prompt">Guess the duration in hours:</p>
            <div className="guess-input-container">
              <input
                type="number"
                min="1"
                max="100"
                value={guessHours}
                onChange={(e) => setGuessHours(e.target.value)}
                onKeyPress={handleKeyPress}
                placeholder="?"
                className="guess-input"
                autoFocus
              />
              <span className="guess-unit">hours</span>
            </div>
            {feedback && (
              <div
                className={`feedback ${feedback.includes('Correct') ? 'correct' : 'wrong'}`}
                dangerouslySetInnerHTML={{ __html: feedback }}
              />
            )}
            <button onClick={handleGuess} className="btn btn-primary">
              Submit Guess
            </button>
          </div>
        ) : (
          <div className="game-result">
            <div
              className={`feedback ${feedback?.includes('Correct') ? 'correct' : 'gameover'}`}
              dangerouslySetInnerHTML={{ __html: feedback || '' }}
            />
            <div className="book-info">
              <h3>{currentBook.title}</h3>
              <p><strong>Author:</strong> {currentBook.author}</p>
              {currentBook.narrator && <p><strong>Narrator:</strong> {currentBook.narrator}</p>}
              {currentBook.series && (
                <p>
                  <strong>Series:</strong> {currentBook.series}
                  {currentBook.series_index && ` #${currentBook.series_index}`}
                </p>
              )}
              <p><strong>Duration:</strong> {currentBook.duration}</p>
              {currentBook.genre && <p><strong>Genre:</strong> {currentBook.genre}</p>}
            </div>
            <button onClick={handleNext} className="btn btn-primary">
              Next Book →
            </button>
          </div>
        )}
      </div>

          <div className="faq-section">
            <details>
              <summary className="faq-toggle">
                <span>❓ How to Play & Book Length Guide</span>
              </summary>
              <div className="faq-content">
                <h4>🎮 How to Play:</h4>
                <ul>
                  <li>Look at the book cover and length hint</li>
                  <li>Guess the audiobook duration in hours (rounded)</li>
                  <li>You have 3 attempts per book</li>
                  <li>Get hints after each wrong guess (higher/lower + how close you are)</li>
                </ul>

                <h4>⏱️ Rounding Rules:</h4>
                <ul>
                  <li>30 minutes or more rounds UP (e.g., 10:30 → 11 hours)</li>
                  <li>Less than 30 minutes rounds DOWN (e.g., 10:29 → 10 hours)</li>
                  <li>Books under 1 hour always round to 1 hour minimum</li>
                </ul>

                <h4>📚 Book Length Categories:</h4>
                <div className="time-ranges">
                  <div className="time-range-item">
                    <strong>📖 Novella</strong>
                    <span>Under 5 hours</span>
                  </div>
                  <div className="time-range-item">
                    <strong>📚 Short</strong>
                    <span>5-10 hours</span>
                  </div>
                  <div className="time-range-item">
                    <strong>📚 Medium</strong>
                    <span>11-15 hours</span>
                  </div>
                  <div className="time-range-item">
                    <strong>📚📚 Long</strong>
                    <span>16-24 hours</span>
                  </div>
                  <div className="time-range-item">
                    <strong>📚📚📚 Extra Long</strong>
                    <span>25+ hours</span>
                    <small>(Shows 15-hour range hint)</small>
                  </div>
                </div>

                <h4>🏆 Scoring:</h4>
                <ul>
                  <li>1 point per correct answer</li>
                  <li>2 points for Extra Long books (25+ hours) - harder to guess!</li>
                </ul>
              </div>
            </details>
          </div>
        </>
      )}
    </div>
  );
}

export default GamePage;
