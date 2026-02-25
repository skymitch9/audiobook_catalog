# Utility Scripts

This folder contains utility scripts for managing the audiobook catalog.

## Scripts

### export_drive_folders.py
**Purpose:** Export Google Drive folder IDs using the Drive API

**When to use:** When you need to get folder IDs from Google Drive automatically

**Requirements:**
```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

**Setup:**
1. Enable Google Drive API
2. Create OAuth credentials
3. Download credentials.json
4. Run the script

**Usage:**
```bash
cd audiobook_catalog
python scripts/export_drive_folders.py
```

---

### update_author_map_from_csv.py
**Purpose:** Update author_drive_map.json from a CSV file

**When to use:** When you have a CSV export of author folders from Google Drive

**CSV Format:**
```csv
"name","id","path"
"Author Name","folder_id_here","/Author Name"
```

**Usage:**
```bash
# Place your CSV at: author_folders.csv (in project root)
cd audiobook_catalog
python scripts/update_author_map_from_csv.py
```

---

### merge_author_maps.py
**Purpose:** Merge two author map JSON files

**When to use:** When you have multiple sources of author folder IDs to combine

**Usage:**
```bash
# Create: author_drive_map_export.json (in project root)
cd audiobook_catalog
python scripts/merge_author_maps.py
```

---

### DRIVE_EXPORT_GUIDE.txt
**Purpose:** Complete guide for exporting Drive folder IDs

**Contains:**
- 4 different methods to export folder IDs
- Step-by-step instructions
- Google Apps Script examples
- Troubleshooting tips

---

## Common Workflows

### Adding New Authors
1. Run the generate_author_map tool:
   ```bash
   python -m app.tools.generate_author_map
   ```
2. This will add any new authors with empty folder IDs

### Updating Folder IDs from Drive
**Option A: Using CSV export**
1. Export your Drive folders to CSV
2. Place CSV at project root as `author_folders.csv`
3. Run: `python scripts/update_author_map_from_csv.py`

**Option B: Using Drive API**
1. Set up OAuth credentials (see DRIVE_EXPORT_GUIDE.txt)
2. Run: `python scripts/export_drive_folders.py`
3. Run: `python scripts/merge_author_maps.py`

### Checking Data Quality
```bash
# Using Docker (recommended)
docker compose --profile test run --rm audiobook-catalog-test python -m pytest tests/test_catalog_completeness.py -v

# Or locally
python -m pytest tests/test_catalog_completeness.py -v
```

This will show:
- How many authors have Drive links
- Which authors are missing links
- Cover extraction status
- Overall data quality metrics

---

## Notes

- All scripts create backups before modifying files
- Scripts handle name normalization automatically
- Co-authors are handled intelligently (if primary author has link, co-author entry not required)
