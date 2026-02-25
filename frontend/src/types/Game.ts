/**
 * Game state types for the duration guessing game.
 * 
 * These types define the structure for managing the interactive game
 * where users guess audiobook durations.
 */

import type { Book } from './Book';

/**
 * Feedback type for user guesses.
 */
export type GuessFeedback = 'correct' | 'too-high' | 'too-low' | null;

/**
 * User's guess structure with hours and minutes.
 */
export interface UserGuess {
  hours: number;
  minutes: number;
}

/**
 * Game state interface tracking the current game session.
 * 
 * @interface GameState
 * @property {Book | null} currentBook - The book currently being guessed (null if loading)
 * @property {UserGuess | null} userGuess - The user's current guess (null if not yet submitted)
 * @property {GuessFeedback} feedback - Feedback on the guess (correct/too-high/too-low/null)
 * @property {number} score - Total number of correct guesses in the session
 * @property {number} streak - Number of consecutive correct guesses
 * @property {boolean} showAnswer - Whether to reveal the actual duration
 */
export interface GameState {
  currentBook: Book | null;
  userGuess: UserGuess | null;
  feedback: GuessFeedback;
  score: number;
  streak: number;
  showAnswer: boolean;
}
