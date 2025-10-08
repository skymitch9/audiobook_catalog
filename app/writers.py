import csv, html, shutil
from pathlib import Path
from typing import List, Dict

def write_csv(rows: List[Dict[str, str]], out_path: Path):
    fieldnames = ["title","series","series_index_display","series_index_sort",
                  "author","narrator","year","genre","duration_hhmm"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

def write_html(rows: List[Dict[str, str]], out_path: Path, generated_at: str, csv_link: str = "catalog.csv"):
    cols = [
        ("Title","title", False),
        ("Series","series", False),
        ("#","series_index_display", True),
        ("Author","author", False),
        ("Narrator","narrator", False),
        ("Year","year", False),
        ("Genre","genre", False),
        ("Duration (hh:mm)","duration_hhmm", False),
    ]
    def esc(s: str) -> str: return html.escape(s or "")

    sort_js = r"""
<script>
(function(){
  function getCellVal(row, idx, numeric){
    var cell = row.children[idx];
    if (numeric) {
      var key = cell.getAttribute("data-sort");
      if (key !== null && key !== "") {
        var num = parseFloat(key);
        if (!isNaN(num)) return num;
      }
      var txt = cell.innerText || cell.textContent || "";
      var num2 = parseFloat(txt);
      if (!isNaN(num2)) return num2;
      return -Infinity;
    } else { return cell.innerText || cell.textContent || ""; }
  }
  function compare(a,b,num){ if(num) return a-b; return (""+a).localeCompare(""+b, undefined, {numeric:true, sensitivity:'base'}); }
  function sortTableBy(t, idx, numeric, asc){
    var tb=t.tBodies[0], rows=Array.from(tb.querySelectorAll("tr"));
    rows.sort(function(r1,r2){ var v1=getCellVal(r1,idx,numeric), v2=getCellVal(r2,idx,numeric);
      return asc ? (compare(v1,v2,numeric)) : (compare(v2,v1,numeric)); });
    rows.forEach(function(r){ tb.appendChild(r); });
  }
  function makeTableSortable(table){
    table.querySelectorAll("th").forEach(function(th, idx){
      var asc=true; th.style.cursor="pointer";
      th.addEventListener("click", function(){
        var numeric = th.dataset.type === "num"; sortTableBy(table, idx, numeric, asc); asc=!asc;
      });
    });
  }
  function getCardSortKey(card, key, numeric){
    var v = card.getAttribute("data-" + key) || "";
    if (!numeric) return v;
    var n = parseFloat(v); return isNaN(n) ? -Infinity : n;
  }
  function sortCards(container, key, numeric, asc){
    var cards = Array.from(container.querySelectorAll(".ab-card"));
    cards.sort(function(a,b){
      var v1=getCardSortKey(a,key,numeric), v2=getCardSortKey(b,key,numeric);
      if(numeric) return asc ? v1-v2 : v2-v1;
      return asc ? (""+v1).localeCompare(""+v2, undefined, {numeric:true, sensitivity:'base'})
                 : (""+v2).localeCompare(""+v1, undefined, {numeric:true, sensitivity:'base'});
    });
    cards.forEach(function(c){ container.appendChild(c); });
  }
  function applySearch(q){
    q=q.toLowerCase();
    document.querySelectorAll("#ab-table tbody tr").forEach(function(r){ r.style.display = r.innerText.toLowerCase().indexOf(q)>=0 ? "" : "none"; });
    document.querySelectorAll("#ab-cards .ab-card").forEach(function(c){ var hay=(c.innerText||"").toLowerCase(); c.style.display = hay.indexOf(q)>=0 ? "" : "none"; });
  }
  document.addEventListener("DOMContentLoaded", function(){
    var table=document.getElementById("ab-table"); if(table) makeTableSortable(table);
    var search=document.getElementById("ab-search");
    if(search){ search.addEventListener("input", function(){ applySearch(search.value); }); }
    var sortSelect=document.getElementById("ab-sort"), ascBtn=document.getElementById("ab-sort-asc"), descBtn=document.getElementById("ab-sort-desc");
    function doSort(asc){
      var sel=sortSelect.value, parts=sel.split("|"), key=parts[0], typ=parts[1], numeric=(typ==="num");
      var idx=Array.from(document.querySelectorAll("#ab-table thead th")).findIndex(function(th){ return th.getAttribute("data-key")===key; });
      if(idx>=0) sortTableBy(table, idx, numeric, asc);
      var cont=document.getElementById("ab-cards"); if(cont) sortCards(cont, key, numeric, asc);
    }
    if(ascBtn) ascBtn.addEventListener("click", function(){ doSort(true); });
    if(descBtn) descBtn.addEventListener("click", function(){ doSort(false); });
    var toggle=document.getElementById("ab-toggle-view"); if(toggle){ toggle.addEventListener("click", function(){ document.body.classList.toggle("cards"); }); }
  });
})();
</script>
"""
    head = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Audiobook Catalog</title>
<style>
:root {{ --pad: 14px; --radius: 14px; --muted: #666; }}
* {{ box-sizing: border-box; }}
body {{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; padding: var(--pad); }}
#wrap {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ margin: 0 0 8px 0; }}
#controls {{ display: grid; gap: 8px; grid-template-columns: 1fr; align-items: center; margin: 8px 0 14px; }}
.controls-row {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
input#ab-search {{ flex:1 1 240px; padding:10px 12px; border:1px solid #ddd; border-radius: var(--radius); }}
select#ab-sort {{ padding:10px 12px; border:1px solid #ddd; border-radius: var(--radius); }}
button.btn {{ padding:10px 12px; border:1px solid #ddd; border-radius: var(--radius); background:#f7f7f7; }}
#meta {{ color: var(--muted); }}

table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #eee; padding: 10px; vertical-align: top; }}
th {{ background: #fafafa; user-select: none; position: sticky; top: 0; z-index: 1; }}
tr:nth-child(even) {{ background: #fcfcfc; }}
th[data-type="num"] {{ text-align: right; }}
td:nth-child(3) {{ text-align: right; }}

#ab-cards {{ display: none; grid-template-columns: 1fr; gap: 10px; }}
.ab-card {{ border:1px solid #eee; border-radius: var(--radius); padding:12px; background:white; box-shadow: 0 1px 0 rgba(0,0,0,0.03); }}
.ab-card .t {{ font-weight: 600; margin-bottom: 4px; }}
.ab-card .meta {{ color: var(--muted); font-size: 0.95em; }}
.ab-row {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:4px; }}
.ab-chip {{ background:#f2f2f2; padding:4px 8px; border-radius:999px; font-size:0.9em; }}

@media (max-width: 820px) {{
  body.cards #ab-table {{ display:none; }}
  body.cards #ab-cards {{ display:grid; }}
  body:not(.cards) #ab-cards {{ display:none; }}
  body:not(.cards) #ab-table {{ display:table; }}
  th, td {{ padding: 12px; }}
  input#ab-search, select#ab-sort, button.btn {{ padding:12px 14px; }}
}}
</style>
</head>
<body class="cards"><div id="wrap">
<h1>Audiobook Catalog</h1>
<div id="controls">
  <div class="controls-row">
    <input id="ab-search" type="text" placeholder="Search any field..." />
    <select id="ab-sort">
      <option value="title|txt">Sort by Title</option>
      <option value="series|txt">Sort by Series</option>
      <option value="series_index_sort|num">Sort by # (numeric)</option>
      <option value="author|txt">Sort by Author</option>
      <option value="narrator|txt">Sort by Narrator</option>
      <option value="year|txt">Sort by Year</option>
      <option value="genre|txt">Sort by Genre</option>
      <option value="duration_hhmm|txt">Sort by Duration</option>
    </select>
    <button id="ab-sort-asc" class="btn">Asc</button>
    <button id="ab-sort-desc" class="btn">Desc</button>
    <button id="ab-toggle-view" class="btn">Toggle View</button>
  </div>
  <div id="meta"><small>
    Generated at: {html.escape(generated_at)} ·
    <a href="{html.escape(csv_link)}" download>Download CSV</a>
  </small></div>
</div>
"""

    head_row = "<tr>" + "".join(
        f'<th data-key="{key}" {"data-type=\"num\"" if is_num else ""}>{esc(title)}</th>'
        for (title, key, is_num) in cols
    ) + "</tr>"

    body_rows = []
    for r in rows:
        tds = []
        for (title, key, is_num) in cols:
            if key == "series_index_display":
                sort_key = r.get("series_index_sort", "")
                tds.append(f'<td data-sort="{esc(sort_key)}">{esc(r.get(key, ""))}</td>')
            else:
                tds.append(f"<td>{esc(r.get(key, ''))}</td>")
        body_rows.append("<tr>" + "".join(tds) + "</tr>")
    table_html = f"""<table id="ab-table"><thead>{head_row}</thead><tbody>{''.join(body_rows)}</tbody></table>"""

    # Card grid
    card_parts = []
    for r in rows:
        attrs = {
            "title": r.get("title",""),
            "series": r.get("series",""),
            "series_index_sort": r.get("series_index_sort",""),
            "author": r.get("author",""),
            "narrator": r.get("narrator",""),
            "year": r.get("year",""),
            "genre": r.get("genre",""),
            "duration_hhmm": r.get("duration_hhmm",""),
        }
        data_attrs = " ".join(f'data-{k}="{esc(v)}"' for k,v in attrs.items())
        chips = []
        if r.get("series"): chips.append(f'<span class="ab-chip">Series: {esc(r["series"])}</span>')
        if r.get("series_index_display"): chips.append(f'<span class="ab-chip">#: {esc(r["series_index_display"])}</span>')
        if r.get("author"): chips.append(f'<span class="ab-chip">Author: {esc(r["author"])}</span>')
        if r.get("narrator"): chips.append(f'<span class="ab-chip">Narrator: {esc(r["narrator"])}</span>')
        if r.get("year"): chips.append(f'<span class="ab-chip">Year: {esc(r["year"])}</span>')
        if r.get("genre"): chips.append(f'<span class="ab-chip">Genre: {esc(r["genre"])}</span>')
        if r.get("duration_hhmm"): chips.append(f'<span class="ab-chip">Dur: {esc(r["duration_hhmm"])}</span>')
        card_parts.append(f"""
<div class="ab-card" {data_attrs}>
  <div class="t">{esc(r.get("title",""))}</div>
  <div class="ab-row">{''.join(chips)}</div>
</div>
""")
    cards_html = f"""<div id="ab-cards">{''.join(card_parts)}</div>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(head + table_html + cards_html + sort_js + "</div></body></html>")

def stage_site_files(timestamped_html: Path, timestamped_csv: Path, site_dir: Path, site_index_name: str, site_csv_name: str):
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / site_index_name).write_bytes(timestamped_html.read_bytes())
    (site_dir / site_csv_name).write_bytes(timestamped_csv.read_bytes())
