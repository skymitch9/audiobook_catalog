/**
 * Leaderboard Service
 * 
 * Manages game scores and leaderboard data in Firebase Firestore.
 * Implements weekly leaderboard with automatic reset logic.
 */

import {
  collection,
  doc,
  setDoc,
  getDoc,
  getDocs,
  query,
  orderBy,
  limit,
  Timestamp,
  updateDoc
} from 'firebase/firestore';
import { db } from './firebase';

export interface PlayerScore {
  playerName: string;
  correct: number;
  wrong: number;
  bestStreak: number;
  points: number;
  score: number; // Calculated: points * 10 + bestStreak * 5
  lastPlayed: Date;
  weekId: string;
}

/**
 * Get current week identifier (YYYY-Wnn format)
 */
export const getCurrentWeek = (): string => {
  const now = new Date();
  const startOfYear = new Date(now.getFullYear(), 0, 1);
  const days = Math.floor((now.getTime() - startOfYear.getTime()) / (24 * 60 * 60 * 1000));
  const week = Math.ceil((days + startOfYear.getDay() + 1) / 7);
  return `${now.getFullYear()}-W${week.toString().padStart(2, '0')}`;
};

/**
 * Get next Monday at midnight
 */
export const getNextMonday = (): Date => {
  const now = new Date();
  const dayOfWeek = now.getDay();
  const daysUntilMonday = dayOfWeek === 0 ? 1 : 8 - dayOfWeek;
  const nextMonday = new Date(now);
  nextMonday.setDate(now.getDate() + daysUntilMonday);
  nextMonday.setHours(0, 0, 0, 0);
  return nextMonday;
};

/**
 * Submit a game result to the leaderboard
 */
export const submitGameResult = async (
  playerName: string,
  isCorrect: boolean,
  currentStreak: number,
  bonusPoints: number = 1
): Promise<void> => {
  const weekId = getCurrentWeek();
  const playerDocRef = doc(db, 'leaderboard', `${weekId}_${playerName}`);

  try {
    const playerDoc = await getDoc(playerDocRef);

    if (playerDoc.exists()) {
      // Update existing player
      const data = playerDoc.data();
      const newCorrect = isCorrect ? data.correct + 1 : data.correct;
      const newWrong = isCorrect ? data.wrong : data.wrong + 1;
      const newPoints = isCorrect ? data.points + bonusPoints : data.points;
      const newBestStreak = Math.max(data.bestStreak, currentStreak);
      const newScore = newPoints * 10 + newBestStreak * 5;

      await updateDoc(playerDocRef, {
        correct: newCorrect,
        wrong: newWrong,
        points: newPoints,
        bestStreak: newBestStreak,
        score: newScore,
        lastPlayed: Timestamp.now()
      });
    } else {
      // Create new player entry
      const newScore: PlayerScore = {
        playerName,
        correct: isCorrect ? 1 : 0,
        wrong: isCorrect ? 0 : 1,
        bestStreak: currentStreak,
        points: isCorrect ? bonusPoints : 0,
        score: isCorrect ? bonusPoints * 10 + currentStreak * 5 : 0,
        lastPlayed: new Date(),
        weekId
      };

      await setDoc(playerDocRef, {
        ...newScore,
        lastPlayed: Timestamp.now()
      });
    }
  } catch (error) {
    console.error('Error submitting game result:', error);
    throw error;
  }
};

/**
 * Get current week's leaderboard
 */
export const getLeaderboard = async (topN: number = 10): Promise<PlayerScore[]> => {
  const weekId = getCurrentWeek();

  try {
    const leaderboardRef = collection(db, 'leaderboard');
    const q = query(
      leaderboardRef,
      orderBy('score', 'desc'),
      limit(topN)
    );

    const querySnapshot = await getDocs(q);
    const leaderboard: PlayerScore[] = [];

    querySnapshot.forEach((doc) => {
      const data = doc.data();
      // Only include current week's scores
      if (data.weekId === weekId) {
        leaderboard.push({
          playerName: data.playerName,
          correct: data.correct,
          wrong: data.wrong,
          bestStreak: data.bestStreak,
          points: data.points,
          score: data.score,
          lastPlayed: data.lastPlayed.toDate(),
          weekId: data.weekId
        });
      }
    });

    return leaderboard;
  } catch (error) {
    console.error('Error fetching leaderboard:', error);
    throw error;
  }
};

/**
 * Get player's current week stats
 */
export const getPlayerStats = async (playerName: string): Promise<PlayerScore | null> => {
  const weekId = getCurrentWeek();
  const playerDocRef = doc(db, 'leaderboard', `${weekId}_${playerName}`);

  try {
    const playerDoc = await getDoc(playerDocRef);

    if (playerDoc.exists()) {
      const data = playerDoc.data();
      return {
        playerName: data.playerName,
        correct: data.correct,
        wrong: data.wrong,
        bestStreak: data.bestStreak,
        points: data.points,
        score: data.score,
        lastPlayed: data.lastPlayed.toDate(),
        weekId: data.weekId
      };
    }

    return null;
  } catch (error) {
    console.error('Error fetching player stats:', error);
    throw error;
  }
};
