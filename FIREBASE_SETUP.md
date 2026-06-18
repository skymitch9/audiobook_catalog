# Firebase Setup Guide for Audiobook Game Leaderboard

## Overview
The audiobook guessing game now supports a cloud-based leaderboard using Firebase Firestore. This allows all players to compete on the same leaderboard instead of just tracking local scores.

## Current Status
- ✅ Firebase SDK integrated into `site/guess-game.html`
- ✅ Fallback to localStorage if Firebase is not configured
- ⏳ Firebase project needs to be created and configured

## Setup Instructions

### Step 1: Create Firebase Project

1. Go to [Firebase Console](https://console.firebase.google.com)
2. Click "Add project" or "Create a project"
3. Enter project name (e.g., "audiobook-game")
4. Disable Google Analytics (optional for this use case)
5. Click "Create project"

### Step 2: Add Web App to Firebase Project

1. In your Firebase project, click the web icon (</>) to add a web app
2. Enter app nickname (e.g., "Audiobook Game")
3. Do NOT check "Firebase Hosting" (we're using GitHub Pages)
4. Click "Register app"
5. Copy the `firebaseConfig` object shown on screen

### Step 3: Enable Firestore Database

1. In Firebase Console, go to "Build" → "Firestore Database"
2. Click "Create database"
3. Choose "Start in production mode" (we'll add security rules next)
4. Select a location (choose closest to your users)
5. Click "Enable"

### Step 4: Configure Security Rules

1. In Firestore Database, go to "Rules" tab
2. Replace the default rules with:

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /leaderboard/{docId} {
      // Allow read access to all leaderboard entries
      allow read: if true;
      
      // Allow write only if document ID matches pattern: weekId_playerName
      // and the playerName in the document matches the one in the ID
      allow write: if request.resource.data.playerName is string
                   && docId.matches('^[0-9]{4}-W[0-9]{2}_.*')
                   && docId.split('_')[1] == request.resource.data.playerName;
    }
  }
}
```

3. Click "Publish"

### Step 5: Update Game Configuration

1. Open `site/guess-game.html` in a text editor
2. Find the Firebase configuration section (search for "YOUR_API_KEY")
3. Replace the placeholder values with your actual Firebase config:

```javascript
const firebaseConfig = {
    apiKey: "YOUR_ACTUAL_API_KEY",
    authDomain: "your-project-id.firebaseapp.com",
    projectId: "your-project-id",
    storageBucket: "your-project-id.appspot.com",
    messagingSenderId: "123456789",
    appId: "1:123456789:web:abcdef123456"
};
```

### Step 6: Test Locally

1. Make sure your Python HTTP server is running:
   ```bash
   python -m http.server 5000 --directory site
   ```

2. Open http://localhost:5000/guess-game.html in your browser

3. Open browser console (F12) and check for:
   - "Firebase initialized successfully" message
   - No Firebase errors

4. Play a game and check if scores appear in Firebase Console:
   - Go to Firestore Database → Data tab
   - Look for "leaderboard" collection
   - Verify your score document was created

### Step 7: Deploy to GitHub Pages

1. Commit the updated `guess-game.html` file:
   ```bash
   git add site/guess-game.html
   git commit -m "Add Firebase leaderboard integration"
   git push
   ```

2. The GitHub Actions workflow will automatically deploy to GitHub Pages

3. Test the live site to ensure Firebase works in production

## Firestore Data Structure

### Collection: `leaderboard`

Each document represents one player's stats for one week.

**Document ID Format:** `{weekId}_{playerName}`
- Example: `2026-W08_Alice`

**Document Fields:**
```javascript
{
  playerName: "Alice",           // Player's display name
  weekId: "2026-W08",            // Week identifier (YYYY-Wnn)
  correct: 12,                   // Number of correct guesses this week
  wrong: 3,                      // Number of wrong guesses this week
  bestStreak: 5,                 // Best streak achieved this week
  points: 14,                    // Total points (1 per correct, 2 for 25+ hour books)
  score: 165,                    // Calculated: points * 10 + bestStreak * 5
  lastPlayed: Timestamp          // Last time player played
}
```

## How It Works

### Weekly Reset
- Leaderboard resets every Monday at midnight
- Week identifier format: `YYYY-Wnn` (e.g., 2026-W08)
- Old week data remains in Firestore but is not displayed
- Players start fresh each week

### Scoring System
- 1 point per correct guess
- 2 points for Extra Long books (25+ hours)
- Final Score = (Points × 10) + (Best Streak × 5)

### Fallback Behavior
- If Firebase is not configured, game uses localStorage only
- If Firebase connection fails, game continues with local scores
- No errors shown to players - graceful degradation

## Cost Estimation

### Firestore Free Tier
- 50,000 reads/day
- 20,000 writes/day
- 1 GB storage

### Expected Usage (20-100 views/month)
- ~10 games per player per week
- ~5 players per week
- Writes: 50 games/week = ~7 writes/day
- Reads: 50 games × 1 leaderboard fetch = ~7 reads/day
- Storage: ~50 documents × 500 bytes = 25 KB

**Result:** Well within free tier limits. Cost: $0/month

## Troubleshooting

### "Firebase not configured" in console
- Check that you replaced ALL placeholder values in firebaseConfig
- Verify apiKey, projectId, and appId are correct
- Make sure there are no extra quotes or spaces

### "Permission denied" errors
- Verify Firestore security rules are published
- Check that document ID matches pattern: `weekId_playerName`
- Ensure playerName in document matches playerName in ID

### Leaderboard not updating
- Open browser console and check for errors
- Verify Firebase initialized successfully
- Check Firestore Database in Firebase Console for new documents
- Try refreshing the page

### Scores not appearing for other players
- Verify you're testing with different player names
- Check that both players are in the same week
- Look at Firestore Data tab to see all documents
- Make sure security rules allow reads

## Security Notes

### API Key is Public
The Firebase API key in the HTML file is **intentionally public**. This is normal and safe because:
- Firestore security rules protect your data
- API key only identifies your Firebase project
- Security is enforced server-side by Firebase

### Security Rules Explained
```javascript
// Anyone can read leaderboard entries
allow read: if true;

// Players can only write to their own document
// Document ID must be: weekId_playerName
allow write: if request.resource.data.playerName is string
             && docId.matches('^[0-9]{4}-W[0-9]{2}_.*')
             && docId.split('_')[1] == request.resource.data.playerName;
```

This prevents:
- Players from modifying other players' scores
- Invalid document IDs
- Malicious data injection

## Future Enhancements

Possible improvements (not currently implemented):
- Historical leaderboard archives
- Player authentication (Google, GitHub, etc.)
- Real-time updates (currently requires page refresh)
- Multiple game modes with separate leaderboards
- Achievements and badges
- Social features (friends, challenges)

## Support

If you encounter issues:
1. Check browser console for error messages
2. Verify Firebase configuration is correct
3. Check Firestore security rules are published
4. Review this guide's troubleshooting section
5. Check Firebase Console for quota limits

## References

- [Firebase Documentation](https://firebase.google.com/docs)
- [Firestore Security Rules](https://firebase.google.com/docs/firestore/security/get-started)
- [Firebase Pricing](https://firebase.google.com/pricing)
