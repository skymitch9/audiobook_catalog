#!/usr/bin/env python3
"""
Generate series completion tracker page.
Analyzes catalog to find series with gaps and completion status.
"""
import sys
import csv
import json
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple
import re

from app.core.index_utils import normalize_index, sort_key_for_index

def parse_index(index_str: str) -> float:
    """Parse series index to float for gap detection."""
    if not index_str:
        return 0.0
    
    normalized = normalize_index(index_str)
    if not normalized:
        return 0.0
    
    try:
        # Handle ranges (e.g., "1-3") - use first number
        if '-' in normalized:
            normalized = normalized.split('-')[0]
        return float(normalized)
    except (ValueError, TypeError):
        return 0.0

def detect_gaps(indices: List[float]) -> List[Tuple[float, float]]:
    """Detect gaps in series indices."""
    if len(indices) < 2:
        return []
    
    sorted_indices = sorted(set(indices))
    gaps = []
    
    for i in range(len(sorted_indices) - 1):
        current = sorted_indices[i]
        next_idx = sorted_indices[i + 1]
        
        # Check for gap (allowing for 0.5 increments for novellas)
        expected_next = current + 1.0
        if next_idx > expected_next + 0.1:  # Small tolerance for float comparison
            gaps.append((current, next_idx))
    
    return gaps

def analyze_series(catalog_path: Path) -> Dict:
    """Analyze catalog for series completion."""
    if not catalog_path.exists():
        return {}
    
    # Read catalog
    with open(catalog_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        books = list(reader)
    
    # Group by series
    series_data = defaultdict(list)
    standalone_count = 0
    
    for book in books:
        series = book.get('series', '').strip()
        if not series:
            standalone_count += 1
            continue
        
        index_str = book.get('series_index_display', '')
        index_num = parse_index(index_str)
        
        series_data[series].append({
            'title': book.get('title', ''),
            'author': book.get('author', ''),
            'index_display': index_str,
            'index_num': index_num,
            'cover': book.get('cover_href', ''),
            'year': book.get('year', ''),
            'narrator': book.get('narrator', '')
        })
    
    # Analyze each series
    series_analysis = []
    
    for series_name, books_list in series_data.items():
        # Sort by index
        books_list.sort(key=lambda b: b['index_num'])
        
        # Get indices
        indices = [b['index_num'] for b in books_list if b['index_num'] > 0]
        
        # Detect gaps
        gaps = detect_gaps(indices)
        
        # Calculate completion
        if indices:
            min_idx = min(indices)
            max_idx = max(indices)
            expected_count = int(max_idx - min_idx + 1)
            actual_count = len(set(indices))
            completion_pct = (actual_count / expected_count * 100) if expected_count > 0 else 100
        else:
            completion_pct = 100
            expected_count = len(books_list)
        
        # Determine status
        if completion_pct >= 100:
            status = 'complete'
        elif completion_pct >= 75:
            status = 'mostly-complete'
        elif completion_pct >= 50:
            status = 'in-progress'
        else:
            status = 'incomplete'
        
        series_analysis.append({
            'name': series_name,
            'author': books_list[0]['author'] if books_list else '',
            'book_count': len(books_list),
            'completion_pct': round(completion_pct, 1),
            'status': status,
            'gaps': gaps,
            'books': books_list
        })
    
    # Sort by completion (incomplete first), then by name
    series_analysis.sort(key=lambda s: (s['completion_pct'], s['name']))
    
    return {
        'series': series_analysis,
        'total_series': len(series_analysis),
        'complete_series': sum(1 for s in series_analysis if s['status'] == 'complete'),
        'incomplete_series': sum(1 for s in series_analysis if s['status'] != 'complete'),
        'standalone_books': standalone_count,
        'total_books': len(books)
    }

def generate_html(analysis: Dict, output_path: Path):
    """Generate HTML page for series tracker."""
    series_list = analysis.get('series', [])
    stats = {
        'total_series': analysis.get('total_series', 0),
        'complete': analysis.get('complete_series', 0),
        'incomplete': analysis.get('incomplete_series', 0),
        'standalone': analysis.get('standalone_books', 0),
        'total_books': analysis.get('total_books', 0)
    }
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Series Completion Tracker</title>
    <link rel="stylesheet" href="static/styles.css">
    <style>
        .tracker-header {{
            text-align: center;
            padding: 2rem 1rem;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            margin-bottom: 2rem;
        }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            max-width: 1200px;
            margin: 0 auto 2rem;
            padding: 0 1rem;
        }}
        
        .stat-card {{
            background: white;
            padding: 1.5rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }}
        
        .stat-value {{
            font-size: 2.5rem;
            font-weight: bold;
            color: #667eea;
        }}
        
        .stat-label {{
            color: #666;
            margin-top: 0.5rem;
        }}
        
        .series-grid {{
            display: grid;
            gap: 1.5rem;
            max-width: 1200px;
            margin: 0 auto;
            padding: 0 1rem 2rem;
        }}
        
        .series-card {{
            background: white;
            border-radius: 8px;
            padding: 1.5rem;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        
        .series-card:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }}
        
        .series-header {{
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 1rem;
        }}
        
        .series-title {{
            font-size: 1.3rem;
            font-weight: bold;
            color: #333;
            margin: 0;
        }}
        
        .series-author {{
            color: #666;
            font-size: 0.9rem;
            margin-top: 0.25rem;
        }}
        
        .completion-badge {{
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            font-size: 0.85rem;
            font-weight: 600;
            white-space: nowrap;
        }}
        
        .status-complete {{
            background: #d4edda;
            color: #155724;
        }}
        
        .status-mostly-complete {{
            background: #d1ecf1;
            color: #0c5460;
        }}
        
        .status-in-progress {{
            background: #fff3cd;
            color: #856404;
        }}
        
        .status-incomplete {{
            background: #f8d7da;
            color: #721c24;
        }}
        
        .progress-bar {{
            height: 8px;
            background: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
            margin: 1rem 0;
        }}
        
        .progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
            transition: width 0.3s ease;
        }}
        
        .series-info {{
            display: flex;
            gap: 2rem;
            margin-bottom: 1rem;
            font-size: 0.9rem;
            color: #666;
        }}
        
        .gap-warning {{
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 0.75rem;
            margin: 1rem 0;
            border-radius: 4px;
        }}
        
        .gap-warning strong {{
            color: #856404;
        }}
        
        .books-list {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
            gap: 0.5rem;
            margin-top: 1rem;
        }}
        
        .book-cover {{
            width: 100%;
            aspect-ratio: 2/3;
            object-fit: cover;
            border-radius: 4px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }}
        
        .book-placeholder {{
            width: 100%;
            aspect-ratio: 2/3;
            background: #e9ecef;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.7rem;
            color: #999;
            text-align: center;
            padding: 0.5rem;
        }}
        
        .filter-bar {{
            max-width: 1200px;
            margin: 0 auto 2rem;
            padding: 0 1rem;
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
        }}
        
        .filter-btn {{
            padding: 0.5rem 1rem;
            border: 2px solid #667eea;
            background: white;
            color: #667eea;
            border-radius: 20px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.2s;
        }}
        
        .filter-btn:hover {{
            background: #667eea;
            color: white;
        }}
        
        .filter-btn.active {{
            background: #667eea;
            color: white;
        }}
        
        .back-link {{
            display: inline-block;
            margin: 1rem;
            padding: 0.5rem 1rem;
            background: white;
            color: #667eea;
            text-decoration: none;
            border-radius: 4px;
            font-weight: 600;
        }}
        
        .back-link:hover {{
            background: #f8f9fa;
        }}
        
        body.dark-mode .series-card,
        body.dark-mode .stat-card {{
            background: #2d3748;
            color: #e2e8f0;
        }}
        
        body.dark-mode .series-title {{
            color: #e2e8f0;
        }}
        
        body.dark-mode .series-author,
        body.dark-mode .series-info,
        body.dark-mode .stat-label {{
            color: #a0aec0;
        }}
        
        body.dark-mode .progress-bar {{
            background: #4a5568;
        }}
        
        body.dark-mode .filter-btn {{
            background: #2d3748;
            color: #667eea;
        }}
        
        body.dark-mode .filter-btn:hover,
        body.dark-mode .filter-btn.active {{
            background: #667eea;
            color: white;
        }}
    </style>
</head>
<body>
    <a href="index.html" class="back-link">← Back to Catalog</a>
    
    <div class="tracker-header">
        <h1>📚 Series Completion Tracker</h1>
        <p>Track your series progress and find missing books</p>
    </div>
    
    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-value">{stats['total_series']}</div>
            <div class="stat-label">Total Series</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats['complete']}</div>
            <div class="stat-label">Complete</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats['incomplete']}</div>
            <div class="stat-label">Incomplete</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats['standalone']}</div>
            <div class="stat-label">Standalone Books</div>
        </div>
    </div>
    
    <div class="filter-bar">
        <button class="filter-btn active" data-filter="all">All Series</button>
        <button class="filter-btn" data-filter="incomplete">Incomplete Only</button>
        <button class="filter-btn" data-filter="complete">Complete Only</button>
        <button class="filter-btn" data-filter="gaps">Has Gaps</button>
    </div>
    
    <div class="series-grid" id="seriesGrid">
"""
    
    # Generate series cards
    for series in series_list:
        status_class = f"status-{series['status']}"
        status_text = series['status'].replace('-', ' ').title()
        completion = series['completion_pct']
        gaps = series['gaps']
        books = series['books']
        
        has_gaps = len(gaps) > 0
        gap_class = 'has-gaps' if has_gaps else ''
        
        html += f"""
        <div class="series-card {status_class} {gap_class}" data-status="{series['status']}" data-has-gaps="{str(has_gaps).lower()}">
            <div class="series-header">
                <div>
                    <h2 class="series-title">{series['name']}</h2>
                    <div class="series-author">by {series['author']}</div>
                </div>
                <span class="completion-badge {status_class}">{completion}%</span>
            </div>
            
            <div class="progress-bar">
                <div class="progress-fill" style="width: {completion}%"></div>
            </div>
            
            <div class="series-info">
                <span>📖 {series['book_count']} book{'s' if series['book_count'] != 1 else ''}</span>
                <span>✓ {status_text}</span>
            </div>
"""
        
        # Show gaps if any
        if gaps:
            gap_text = []
            for start, end in gaps:
                if end - start > 1.5:
                    gap_text.append(f"#{int(start)+1}-{int(end)-1}")
                else:
                    gap_text.append(f"#{int(start)+1}")
            
            html += f"""
            <div class="gap-warning">
                <strong>⚠️ Missing:</strong> {', '.join(gap_text)}
            </div>
"""
        
        # Show book covers
        html += """
            <div class="books-list">
"""
        for book in books:
            if book['cover']:
                html += f"""
                <img src="{book['cover']}" alt="{book['title']}" class="book-cover" title="{book['title']} (#{book['index_display']})">
"""
            else:
                html += f"""
                <div class="book-placeholder" title="{book['title']} (#{book['index_display']})">
                    #{book['index_display']}
                </div>
"""
        
        html += """
            </div>
        </div>
"""
    
    html += """
    </div>
    
    <script src="static/theme.js"></script>
    <script>
        // Filter functionality
        const filterBtns = document.querySelectorAll('.filter-btn');
        const seriesCards = document.querySelectorAll('.series-card');
        
        filterBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                // Update active button
                filterBtns.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                
                const filter = btn.dataset.filter;
                
                seriesCards.forEach(card => {
                    const status = card.dataset.status;
                    const hasGaps = card.dataset.hasGaps === 'true';
                    
                    let show = false;
                    
                    if (filter === 'all') {
                        show = true;
                    } else if (filter === 'incomplete') {
                        show = status !== 'complete';
                    } else if (filter === 'complete') {
                        show = status === 'complete';
                    } else if (filter === 'gaps') {
                        show = hasGaps;
                    }
                    
                    card.style.display = show ? 'block' : 'none';
                });
            });
        });
    </script>
</body>
</html>
"""
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"Series tracker generated: {output_path}")

def main():
    """Generate series completion tracker."""
    from app.config import SITE_DIR
    
    catalog_path = SITE_DIR / 'catalog.csv'
    output_path = SITE_DIR / 'series-tracker.html'
    
    print("Analyzing series completion...")
    analysis = analyze_series(catalog_path)
    
    print(f"Found {analysis['total_series']} series")
    print(f"  Complete: {analysis['complete_series']}")
    print(f"  Incomplete: {analysis['incomplete_series']}")
    
    print("\nGenerating HTML...")
    generate_html(analysis, output_path)
    
    print("✓ Series tracker page created!")

if __name__ == '__main__':
    main()
