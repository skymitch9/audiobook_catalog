# app/web/html_builder.py
# Builds the HTML page by filling the template with generated table rows and cards.
from __future__ import annotations
from pathlib import Path
from typing import List, Dict, Optional
import html
import json

TEMPLATE_DIR  = Path(__file__).parent / "templates"
STATIC_DIR    = Path(__file__).parent / "static"   # kept for other assets if you have them
TEMPLATE_FILE = TEMPLATE_DIR / "index.html"

def _esc(s) -> str:
    """Escape HTML characters. Handles strings, numbers, and None."""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return html.escape(s)

def _cover_button(r: Dict[str, str], inline: bool=False) -> str:
    """
    Wrap cover <img> in a button with data-* attributes used by the modal.
    If no cover, returns empty string.
    """
    cover = r.get("cover_href") or ""
    if not cover:
        return ""
    cls = "cover-btn inline" if inline else "cover-btn"
    # Data attributes: keep short names to minimize HTML size
    data_attrs = " ".join([
        f'data-cover="{_esc(cover)}"',
        f'data-title="{_esc(r.get("title",""))}"',
        f'data-series="{_esc(r.get("series",""))}"',
        f'data-index="{_esc(r.get("series_index_display",""))}"',
        f'data-author="{_esc(r.get("author",""))}"',
        f'data-narrator="{_esc(r.get("narrator",""))}"',
        f'data-year="{_esc(r.get("year",""))}"',
        f'data-genre="{_esc(r.get("genre",""))}"',
        f'data-duration="{_esc(r.get("duration_hhmm",""))}"',
        f'data-desc="{_esc(r.get("desc",""))}"'  # may be empty if not provided
    ])
    img_cls = "cover-inline" if inline else "cover"
    return f'<button class="{cls}" {data_attrs}><img class="{img_cls}" src="{_esc(cover)}" alt="Cover of {_esc(r.get("title",""))}" loading="lazy" /></button>'

def _row_cells(r: Dict[str, str]) -> str:
    """
    ORDER must match the header in templates/index.html.
    '#' cell shows series_index_display but sorts by data-sort=series_index_sort.
    """
    cover_html = _cover_button(r, inline=False)
    return "".join([
        f"<td>{cover_html}</td>",
        f"<td>{_esc(r.get('title',''))}</td>",
        f"<td>{_esc(r.get('series',''))}</td>",
        f'<td data-sort="{_esc(r.get("series_index_sort",""))}">{_esc(r.get("series_index_display",""))}</td>',
        f"<td>{_esc(r.get('author',''))}</td>",
        f"<td>{_esc(r.get('narrator',''))}</td>",
        f"<td>{_esc(r.get('year',''))}</td>",
        f"<td>{_esc(r.get('genre',''))}</td>",
        f"<td>{_esc(r.get('duration_hhmm',''))}</td>",
    ])

def _table_rows_html(rows: List[Dict[str, str]]) -> str:
    return "".join(f"<tr>{_row_cells(r)}</tr>" for r in rows)

def _card_html(r: Dict[str, str]) -> str:
    # data-* attrs for sorting in cards
    attrs = {
        "title": r.get("title",""),
        "series": r.get("series",""),
        "series_index_sort": r.get("series_index_sort",""),
        "author": r.get("author",""),
        "narrator": r.get("narrator",""),
        "year": r.get("year",""),
        "genre": r.get("genre",""),
        "duration_hhmm": r.get("duration_hhmm",""),
        # Add modal data attributes
        "cover": r.get("cover_href",""),
        "index": r.get("series_index_display",""),
        "desc": r.get("desc","")
    }
    data_attrs = " ".join(f'data-{k}="{_esc(v)}"' for k, v in attrs.items())
    thumb = _cover_button(r, inline=True)

    chips = []
    if r.get("series"):               chips.append(f'<span class="ab-chip">Series: {_esc(r["series"])}</span>')
    if r.get("series_index_display"): chips.append(f'<span class="ab-chip">#: {_esc(r["series_index_display"])}</span>')
    if r.get("author"):               chips.append(f'<span class="ab-chip">Author: {_esc(r["author"])}</span>')
    if r.get("narrator"):             chips.append(f'<span class="ab-chip">Narrator: {_esc(r["narrator"])}</span>')
    if r.get("year"):                 chips.append(f'<span class="ab-chip">Year: {_esc(r["year"])}</span>')
    if r.get("genre"):                chips.append(f'<span class="ab-chip">Genre: {_esc(r["genre"])}</span>')
    if r.get("duration_hhmm"):        chips.append(f'<span class="ab-chip">Dur: {_esc(r["duration_hhmm"])}</span>')

    return f"""
<div class="ab-card" {data_attrs}>
  <div class="t">{thumb}<span>{_esc(r.get("title",""))}</span></div>
  <div class="ab-row">{''.join(chips)}</div>
</div>
"""

def _cards_html(rows: List[Dict[str, str]]) -> str:
    return "".join(_card_html(r) for r in rows)

def _load_author_map() -> str:
    """Load the author drive map JSON and return as string for template injection."""
    try:
        # Try multiple possible locations for the author map file
        possible_paths = [
            Path("author_drive_map.json"),  # Current working directory
            Path(__file__).parent.parent.parent / "author_drive_map.json",  # Parent of audiobook_catalog
            Path(__file__).parent.parent.parent.parent / "author_drive_map.json",  # Root directory
        ]
        
        for author_map_path in possible_paths:
            if author_map_path.exists():
                with open(author_map_path, 'r', encoding='utf-8') as f:
                    author_map = json.load(f)
                print(f"Loaded author map from: {author_map_path}")
                return json.dumps(author_map, separators=(',', ':'))
        
        print(f"Warning: author_drive_map.json not found in any of these locations: {[str(p) for p in possible_paths]}")
        return "{}"
    except Exception as e:
        print(f"Warning: Could not load author_drive_map.json: {e}")
        return "{}"

def render_index_html(
    rows: List[Dict[str, str]],
    out_path: Path,
    generated_at: str,
    csv_link: str,
    drive_link: Optional[str],
) -> None:
    html_template = TEMPLATE_FILE.read_text(encoding="utf-8")
    table_rows = _table_rows_html(rows)
    cards      = _cards_html(rows)
    author_map_json = _load_author_map()

    filled = (
        html_template
        .replace("{{GENERATED_AT}}", _esc(generated_at))
        .replace("{{CSV_LINK}}", _esc(csv_link))
        .replace(
            "{{DRIVE_LINK_BLOCK}}",
            (f' · <a href="{_esc(drive_link)}" target="_blank" rel="noopener">Google Drive folder</a>'
             if drive_link else "")
        )
        .replace("{{TABLE_ROWS}}", table_rows)
        .replace("{{CARDS}}", cards)
        .replace("{{AUTHOR_MAP_JSON}}", author_map_json)
        .replace("{{AUTHOR_DRIVE_MAP_URL}}", "")  # Keep for compatibility
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(filled, encoding="utf-8")
