# New Features

## 1. Enhanced Discord Notifications 📢

Discord notifications now show actual new books with covers when you deploy!

### Features:
- **Detects new books** by comparing current catalog with previous commit
- **Shows up to 10 new books** with rich embeds
- **Displays book covers** as thumbnails in Discord
- **Shows metadata**: Author, narrator, series, year, genre, duration
- **Library stats**: Total books and new additions count
- **Fallback**: If no new books, sends simple update notification

### How it works:
1. On deployment, `detect_new_books.py` compares catalogs
2. Generates `new_books.json` with new additions
3. `send_discord_notification.py` creates rich Discord embeds
4. Sends to your webhook with book covers and details

### Example notification:
```
📚 New Books Added!
5 new books added to the library!

📊 Library Stats: 850 total books, 5 new additions
🔗 View Catalog: [Browse Library]

[Book 1 with cover thumbnail]
Title: The Name of the Wind
Author: Patrick Rothfuss
Narrator: Nick Podehl
Series: The Kingkiller Chronicle #1
Details: 📅 2007 • 🎭 Fantasy
Duration: ⏱️ 27:55
```

---

## 2. Series Completion Tracker 📊

A dedicated page to track your series progress and find missing books!

### Features:
- **Visual progress bars** for each series
- **Completion percentage** calculation
- **Gap detection** - automatically finds missing books in series
- **Status badges**: Complete, Mostly Complete, In Progress, Incomplete
- **Book covers grid** for each series
- **Filter options**: All, Incomplete Only, Complete Only, Has Gaps
- **Statistics dashboard**: Total series, complete count, incomplete count
- **Dark mode support**

### How it works:
1. Analyzes `catalog.csv` to group books by series
2. Detects gaps in series indices (e.g., has books 1, 3, 5 - missing 2, 4)
3. Calculates completion percentage
4. Generates interactive HTML page

### Access:
- Click "📊 Series Tracker" button on main catalog page
- Or visit: `series-tracker.html` directly

### Example:
```
The Expanse Series
by James S.A. Corey
━━━━━━━━━━━━━━━━━━━━ 90%
📖 9 books • ✓ Mostly Complete

⚠️ Missing: #5

[Book covers displayed in grid]
```

---

## Technical Details

### New Files:
- `app/tools/detect_new_books.py` - Detects new books by comparing git commits
- `app/tools/send_discord_notification.py` - Sends rich Discord notifications
- `app/tools/generate_series_tracker.py` - Generates series tracker page

### Updated Files:
- `.github/workflows/deploy.yml` - Added new book detection and notification steps
- `app/main.py` - Generates series tracker automatically
- `app/web/templates/index.html` - Added series tracker link
- `requirements.txt` - Added `requests` library

### Dependencies:
- `requests>=2.31.0` - For Discord webhook API calls

---

## Usage

### Generate catalog with series tracker:
```bash
python -m app.main
```

### Test new book detection:
```bash
python -m app.tools.detect_new_books
```

### Test Discord notification (requires DISCORD_WEBHOOK env var):
```bash
export DISCORD_WEBHOOK="your_webhook_url"
export SITE_URL="https://your-site.github.io"
python -m app.tools.send_discord_notification
```

### View series tracker:
Open `site/series-tracker.html` in your browser

---

## Configuration

### Discord Webhook:
Set as GitHub Secret: `DISCORD_WEBHOOK`

### Series Tracker Customization:
Edit `app/tools/generate_series_tracker.py` to:
- Change completion thresholds
- Modify gap detection logic
- Customize styling
- Add more filters

---

## Future Enhancements

Potential improvements:
- Email notifications option
- Slack webhook support
- Series recommendations based on completion
- Export incomplete series list
- Track reading progress per series
- Series statistics (average length, most popular genres)
