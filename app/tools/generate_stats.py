#!/usr/bin/env python3
"""
Generate statistics page for the audiobook catalog
"""

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

from app.config import OUTPUT_DIR, SITE_DIR


def parse_duration_to_minutes(duration_str: str) -> int:
    """Parse duration string (HH:MM) to total minutes"""
    if not duration_str or ':' not in duration_str:
        return 0
    
    try:
        parts = duration_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        return hours * 60 + minutes
    except (ValueError, IndexError):
        return 0


def calculate_stats(csv_path: Path) -> Dict[str, Any]:
    """Calculate comprehensive statistics from the catalog CSV"""
    if not csv_path.exists():
        return {}
    
    books = []
    with open(csv_path, 'r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        books = list(reader)
    
    if not books:
        return {}
    
    # Basic counts
    total_books = len(books)
    
    # Duration analysis
    durations = [parse_duration_to_minutes(book.get('duration_hhmm', '')) for book in books]
    total_minutes = sum(durations)
    total_hours = total_minutes // 60
    avg_duration_minutes = total_minutes // total_books if total_books > 0 else 0
    
    # Unique counts
    authors = set(book.get('author', '').strip() for book in books if book.get('author', '').strip())
    narrators = set(book.get('narrator', '').strip() for book in books if book.get('narrator', '').strip())
    series = set(book.get('series', '').strip() for book in books if book.get('series', '').strip())
    genres = set(book.get('genre', '').strip() for book in books if book.get('genre', '').strip())
    years = set(book.get('year', '').strip() for book in books if book.get('year', '').strip())
    
    # Top lists
    author_counts = Counter(book.get('author', '').strip() for book in books if book.get('author', '').strip())
    narrator_counts = Counter(book.get('narrator', '').strip() for book in books if book.get('narrator', '').strip())
    series_counts = Counter(book.get('series', '').strip() for book in books if book.get('series', '').strip())
    genre_counts = Counter(book.get('genre', '').strip() for book in books if book.get('genre', '').strip())
    year_counts = Counter(book.get('year', '').strip() for book in books if book.get('year', '').strip())
    
    # Duration categories
    duration_categories = {
        'Novella (< 5h)': 0,
        'Short (5-10h)': 0,
        'Medium (11-15h)': 0,
        'Long (16-24h)': 0,
        'Extra Long (25h+)': 0
    }
    
    for duration_min in durations:
        hours = duration_min / 60
        if hours < 5:
            duration_categories['Novella (< 5h)'] += 1
        elif hours <= 10:
            duration_categories['Short (5-10h)'] += 1
        elif hours <= 15:
            duration_categories['Medium (11-15h)'] += 1
        elif hours <= 24:
            duration_categories['Long (16-24h)'] += 1
        else:
            duration_categories['Extra Long (25h+)'] += 1
    
    # Series analysis
    series_books = defaultdict(list)
    for book in books:
        series_name = book.get('series', '').strip()
        if series_name:
            series_books[series_name].append(book)
    
    # Calculate listening time estimates
    days_total = total_hours / 24
    weeks_total = days_total / 7
    months_total = days_total / 30
    years_total = days_total / 365
    
    return {
        'basic': {
            'total_books': total_books,
            'total_hours': total_hours,
            'total_minutes': total_minutes,
            'total_days': round(days_total, 1),
            'avg_duration_hours': round(avg_duration_minutes / 60, 1),
            'unique_authors': len(authors),
            'unique_narrators': len(narrators),
            'unique_series': len(series),
            'unique_genres': len(genres),
            'year_range': f"{min(years) if years else 'N/A'} - {max(years) if years else 'N/A'}"
        },
        'top_authors': author_counts.most_common(10),
        'top_narrators': narrator_counts.most_common(10),
        'top_series': series_counts.most_common(10),
        'top_genres': genre_counts.most_common(10),
        'recent_years': year_counts.most_common(10),
        'duration_categories': duration_categories,
        'listening_time': {
            'days': round(days_total, 1),
            'weeks': round(weeks_total, 1),
            'months': round(months_total, 1),
            'years': round(years_total, 2)
        },
        'insights': {
            'books_per_author': round(total_books / len(authors), 1) if authors else 0,
            'books_per_narrator': round(total_books / len(narrators), 1) if narrators else 0,
            'series_percentage': round((len(series_books) / total_books) * 100, 1) if total_books > 0 else 0,
            'avg_books_per_series': round(sum(len(books) for books in series_books.values()) / len(series_books), 1) if series_books else 0
        }
    }


def generate_stats_html(stats: Dict[str, Any], generated_at: str) -> str:
    """Generate HTML for the stats page"""
    
    def format_listening_time(stats_data):
        listening = stats_data['listening_time']
        if listening['years'] >= 1:
            return f"{listening['years']} years ({listening['months']:.1f} months)"
        elif listening['months'] >= 1:
            return f"{listening['months']:.1f} months ({listening['weeks']:.1f} weeks)"
        elif listening['weeks'] >= 1:
            return f"{listening['weeks']:.1f} weeks ({listening['days']:.1f} days)"
        else:
            return f"{listening['days']:.1f} days"
    
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Audiobook Catalog - Statistics</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
/* =============== Cyberpunk 2077 Theme =============== */
:root{{
  --pad:14px; --radius:4px;
  --bg:#0a0a12; --bg-2:#12121f;
  --text:#e8e6e3; --muted:#8a8f98;
  --border:#2a2a3a;
  --neon-yellow:#fcee0a;
  --neon-cyan:#05d9e8;
  --neon-magenta:#ff2a6d;
}}

*{{box-sizing:border-box}}
body{{
  margin:0; padding:40px 20px;
  font-family:'Rajdhani','Segoe UI',system-ui,sans-serif;
  background:var(--bg);
  background-image:
    radial-gradient(ellipse at 20% 50%, rgba(252,238,10,.03) 0%, transparent 50%),
    radial-gradient(ellipse at 80% 20%, rgba(5,217,232,.03) 0%, transparent 50%);
  min-height:100vh;
  display:flex;
  justify-content:center;
  align-items:flex-start;
  color:var(--text);
}}
#wrap{{
  background:var(--bg-2);
  border:1px solid var(--border);
  border-radius:0;
  box-shadow:0 0 30px rgba(5,217,232,.1);
  max-width:1100px;
  width:100%;
  padding:40px;
  clip-path: polygon(0 0, calc(100% - 16px) 0, 100% 16px, 100% 100%, 16px 100%, 0 calc(100% - 16px));
}}
h1{{margin:0 0 8px 0; color:var(--neon-yellow); text-transform:uppercase; letter-spacing:2px; text-shadow:0 0 10px rgba(252,238,10,.3)}}

.stats-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 20px;
  margin: 20px 0;
}}
.stat-card {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--neon-cyan);
  border-radius: 0;
  padding: 20px;
  text-align: center;
}}
.stat-number {{
  font-size: 2.5em;
  font-weight: bold;
  color: var(--neon-yellow);
  margin-bottom: 8px;
  text-shadow: 0 0 8px rgba(252,238,10,.3);
  font-family: 'Share Tech Mono', monospace;
}}
.stat-label {{
  font-size: 1.1em;
  color: var(--neon-cyan);
  text-transform: uppercase;
  letter-spacing: .5px;
  font-size: .9em;
}}
.stat-sublabel {{
  font-size: 0.85em;
  color: var(--muted);
}}
.top-list {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 20px;
  margin: 20px 0;
}}
.top-list h3 {{
  margin-top: 0;
  color: var(--neon-magenta);
  text-transform: uppercase;
  letter-spacing: 1px;
  font-size: 1em;
}}
.top-item {{
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
}}
.top-item:last-child {{
  border-bottom: none;
}}
.top-name {{
  font-weight: 500;
  color: var(--text);
}}
.top-count {{
  color: var(--neon-cyan);
  font-weight: bold;
  font-family: 'Share Tech Mono', monospace;
}}
.insights {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 20px;
  margin: 20px 0;
}}
.insights h3 {{
  margin-top: 0;
  color: var(--neon-magenta);
  text-transform: uppercase;
  letter-spacing: 1px;
}}
.insight-item {{
  margin: 12px 0;
  padding: 12px;
  background: var(--bg-2);
  border-radius: 0;
  border-left: 3px solid var(--neon-yellow);
}}
.insight-label {{
  font-weight: bold;
  color: var(--neon-yellow);
  text-transform: uppercase;
  font-size: .85em;
}}
.insight-value {{
  color: var(--muted);
  margin-top: 4px;
}}
.nav-link {{
  display: inline-block;
  padding: 10px 16px;
  background: var(--bg);
  color: var(--neon-cyan);
  text-decoration: none;
  border-radius: 0;
  border: 1px solid var(--border);
  margin: 10px 10px 10px 0;
  text-transform: uppercase;
  font-weight: 600;
  font-size: .85em;
  letter-spacing: .5px;
  transition: all .2s;
  clip-path: polygon(6px 0, 100% 0, calc(100% - 6px) 100%, 0 100%);
}}
.nav-link:hover {{
  border-color: var(--neon-cyan);
  box-shadow: 0 0 8px rgba(5,217,232,.3);
}}
@media (max-width: 768px) {{
  .stats-grid {{
    grid-template-columns: 1fr;
  }}
}}
</style>
</head>
<body>
<div id="wrap">
  <h1>📊 Audiobook Statistics</h1>

  <div style="margin-bottom: 20px;">
    <a href="index.html" class="nav-link">← Back to Catalog</a>
    <a href="guess-game.html" class="nav-link">🎮 Play Game</a>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-number">{stats['basic']['total_books']:,}</div>
      <div class="stat-label">Total Books</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-number">{stats['basic']['total_hours']:,}</div>
      <div class="stat-label">Total Hours</div>
      <div class="stat-sublabel">{stats['basic']['total_days']} days</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-number">{stats['basic']['unique_authors']:,}</div>
      <div class="stat-label">Unique Authors</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-number">{stats['basic']['unique_narrators']:,}</div>
      <div class="stat-label">Unique Narrators</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-number">{stats['basic']['unique_series']:,}</div>
      <div class="stat-label">Unique Series</div>
    </div>
    
    <div class="stat-card">
      <div class="stat-number">{stats['basic']['avg_duration_hours']}</div>
      <div class="stat-label">Average Duration</div>
      <div class="stat-sublabel">hours per book</div>
    </div>
  </div>

  <div class="insights">
    <h3>📋 Collection Insights</h3>
    <div class="insight-item">
      <div class="insight-label">Listening Marathon</div>
      <div class="insight-value">It would take {format_listening_time(stats)} to listen to your entire collection!</div>
    </div>
    <div class="insight-item">
      <div class="insight-label">Author Diversity</div>
      <div class="insight-value">You have an average of {stats['insights']['books_per_author']} books per author.</div>
    </div>
    <div class="insight-item">
      <div class="insight-label">Series Collection</div>
      <div class="insight-value">{stats['insights']['series_percentage']}% of your books are part of a series.</div>
    </div>
    <div class="insight-item">
      <div class="insight-label">Narrator Preference</div>
      <div class="insight-value">You have an average of {stats['insights']['books_per_narrator']} books per narrator.</div>
    </div>
  </div>

  <!-- Community Stats (loaded from Firebase) -->
  <div id="community-stats" class="top-list" style="margin-top:20px">
    <h3>👥 Community Stats</h3>
    <div id="community-stats-content" style="color:var(--muted)">Loading community data...</div>
  </div>

  <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px;">
    <div class="top-list">
      <h3>📚 Top Authors</h3>"""
    
    for author, count in stats['top_authors']:
        html += f"""
      <div class="top-item">
        <span class="top-name">{author}</span>
        <span class="top-count">{count} books</span>
      </div>"""
    
    html += """
    </div>
    
    <div class="top-list">
      <h3>🎙️ Top Narrators</h3>"""
    
    for narrator, count in stats['top_narrators']:
        html += f"""
      <div class="top-item">
        <span class="top-name">{narrator}</span>
        <span class="top-count">{count} books</span>
      </div>"""
    
    html += """
    </div>
    
    <div class="top-list">
      <h3>📖 Top Series</h3>"""
    
    for series, count in stats['top_series']:
        html += f"""
      <div class="top-item">
        <span class="top-name">{series}</span>
        <span class="top-count">{count} books</span>
      </div>"""
    
    html += """
    </div>
    
    <div class="top-list">
      <h3>🎭 Top Genres</h3>"""
    
    for genre, count in stats['top_genres']:
        html += f"""
      <div class="top-item">
        <span class="top-name">{genre}</span>
        <span class="top-count">{count} books</span>
      </div>"""
    
    html += """
    </div>
  </div>

  <div class="top-list">
    <h3>⏱️ Duration Categories</h3>"""
    
    for category, count in stats['duration_categories'].items():
        percentage = round((count / stats['basic']['total_books']) * 100, 1) if stats['basic']['total_books'] > 0 else 0
        html += f"""
    <div class="top-item">
      <span class="top-name">{category}</span>
      <span class="top-count">{count} books ({percentage}%)</span>
    </div>"""
    
    html += f"""
  </div>

  <div style="margin-top: 30px; padding: 20px; background: var(--bg-2); border-radius: var(--radius); text-align: center; color: var(--muted);">
    <small>Generated at: {generated_at}</small>
  </div>
</div>

<script type="module">
  import {{ initializeApp }} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js';
  import {{ getFirestore, collection, getDocs }} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
  import {{ col }} from './fb-env.js';

  const app = initializeApp({{
    apiKey: "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y",
    authDomain: "audiobook-catalog.firebaseapp.com",
    projectId: "audiobook-catalog",
    storageBucket: "audiobook-catalog.firebasestorage.app",
    messagingSenderId: "68492219785",
    appId: "1:68492219785:web:7cbe57dda8712377f0bd58"
  }});
  const db = getFirestore(app);

  async function loadCommunityStats() {{
    try {{
      const [profilesSnap, reviewsSnap] = await Promise.all([
        getDocs(collection(db, col('profiles'))),
        getDocs(collection(db, col('reviews')))
      ]);

      const totalMembers = profilesSnap.size;
      const totalReviews = reviewsSnap.size;

      // Most active reviewers
      const reviewerCounts = {{}};
      let totalRating = 0;
      reviewsSnap.docs.forEach(d => {{
        const data = d.data();
        const name = data.displayName || '';
        reviewerCounts[name] = (reviewerCounts[name] || 0) + 1;
        totalRating += data.rating || 0;
      }});

      const avgRating = totalReviews > 0 ? (totalRating / totalReviews).toFixed(1) : '—';
      const topReviewers = Object.entries(reviewerCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5);

      // Most read books (most reviews)
      const bookCounts = {{}};
      reviewsSnap.docs.forEach(d => {{
        const data = d.data();
        const bookId = data.bookId || '';
        if (bookId) bookCounts[bookId] = (bookCounts[bookId] || 0) + 1;
      }});
      const topBooks = Object.entries(bookCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5);

      // Currently reading count
      let readingCount = 0;
      profilesSnap.docs.forEach(d => {{
        if (d.data().currentlyReading) readingCount++;
      }});

      let html = `
        <div class="stats-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-bottom:16px">
          <div class="stat-card"><div class="stat-number">${{totalMembers}}</div><div class="stat-label">Members</div></div>
          <div class="stat-card"><div class="stat-number">${{totalReviews}}</div><div class="stat-label">Reviews</div></div>
          <div class="stat-card"><div class="stat-number">${{avgRating}}</div><div class="stat-label">Avg Rating</div></div>
          <div class="stat-card"><div class="stat-number">${{readingCount}}</div><div class="stat-label">Reading Now</div></div>
        </div>`;

      if (topReviewers.length > 0) {{
        html += '<div style="margin-top:12px"><strong style="color:var(--neon-cyan);font-size:.85em;text-transform:uppercase">Top Reviewers</strong>';
        topReviewers.forEach(([name, count]) => {{
          html += `<div class="top-item"><span class="top-name">${{name}}</span><span class="top-count">${{count}} reviews</span></div>`;
        }});
        html += '</div>';
      }}

      if (topBooks.length > 0) {{
        html += '<div style="margin-top:12px"><strong style="color:var(--neon-cyan);font-size:.85em;text-transform:uppercase">Most Reviewed Books</strong>';
        topBooks.forEach(([bookId, count]) => {{
          const title = bookId.replace(/-/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase());
          html += `<div class="top-item"><span class="top-name">${{title}}</span><span class="top-count">${{count}} reviews</span></div>`;
        }});
        html += '</div>';
      }}

      document.getElementById('community-stats-content').innerHTML = html;
    }} catch (e) {{
      document.getElementById('community-stats-content').textContent = 'Could not load community data.';
    }}
  }}
  loadCommunityStats();
</script>
</body>
</html>"""
    
    return html


def main():
    """Generate statistics page"""
    # Use the site CSV as the data source
    csv_path = SITE_DIR / "catalog.csv"
    
    if not csv_path.exists():
        print(f"Error: {csv_path} not found. Run the main catalog generator first.")
        return
    
    print("Calculating statistics...")
    stats = calculate_stats(csv_path)
    
    if not stats:
        print("Error: No data found in catalog.")
        return
    
    # Generate timestamp
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Generate HTML
    print("Generating statistics page...")
    html_content = generate_stats_html(stats, generated_at)
    
    # Write to site directory
    stats_path = SITE_DIR / "stats.html"
    with open(stats_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"Statistics page generated: {stats_path}")
    print(f"Total books analyzed: {stats['basic']['total_books']:,}")
    print(f"Total listening time: {stats['basic']['total_hours']:,} hours ({stats['basic']['total_days']} days)")


if __name__ == "__main__":
    main()
