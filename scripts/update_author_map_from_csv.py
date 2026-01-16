"""
Update author_drive_map.json from author_folders.csv
"""

import csv
import json
from pathlib import Path

def normalize_author_name(name):
    """Normalize author name to match the format in author_drive_map.json"""
    # Remove series/book info after dash
    if ' - ' in name:
        name = name.split(' - ')[0].strip()
    
    # Remove trailing slash
    name = name.rstrip('/')
    
    # Handle special cases
    name_map = {
        'J.K. Rowling': 'J.k. Rowling',
        'Sir Bedivere the Mad': 'Sir Bedivere The Mad',
        'You Yeong-Gwang': 'You Yeong-gwang',
        'Stephanie Meyer': 'Stephenie Meyer',
        'TurtleMe': 'Turtleme',
        'Ryan DeBruyn': 'Ryan Debruyn',
        'Laura McHugh': 'Laura Mchugh',
        'Karen M. McManus': 'Karen M. Mcmanus',
        'Jennette McCurdy': 'Jennette Mccurdy',
    }
    
    return name_map.get(name, name)

def update_author_map():
    """Update author_drive_map.json with data from author_folders.csv"""
    
    # Paths
    project_root = Path(__file__).parent.parent
    csv_path = project_root / 'author_folders.csv'
    map_path = project_root / 'author_drive_map.json'
    backup_path = project_root / 'author_drive_map_backup.json'
    
    # Check if CSV exists
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found!")
        return
    
    # Load existing map
    if not map_path.exists():
        print(f"ERROR: {map_path} not found!")
        return
    
    with open(map_path, 'r', encoding='utf-8') as f:
        author_map = json.load(f)
    
    print(f"Loaded existing map: {len(author_map)} authors")
    
    # Backup existing map
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(author_map, f, ensure_ascii=False, indent=2)
    print(f"Backup saved to: {backup_path}")
    
    # Read CSV and update map
    updated_count = 0
    new_count = 0
    skipped_count = 0
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            folder_name = row['name'].strip()
            folder_id = row['id'].strip()
            
            # Normalize the author name
            author_name = normalize_author_name(folder_name)
            
            # Check if this author exists in the map
            if author_name in author_map:
                if not author_map[author_name]:  # Empty value
                    author_map[author_name] = folder_id
                    updated_count += 1
                    print(f"Updated: {author_name} -> {folder_id}")
                elif author_map[author_name] != folder_id:
                    print(f"CONFLICT: {author_name}")
                    print(f"  Existing: {author_map[author_name]}")
                    print(f"  CSV:      {folder_id}")
                    skipped_count += 1
                else:
                    # Already has the same ID
                    skipped_count += 1
            else:
                # New author not in map
                print(f"NEW: {author_name} (from '{folder_name}') -> {folder_id}")
                author_map[author_name] = folder_id
                new_count += 1
    
    # Save updated map
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(author_map, f, ensure_ascii=False, indent=2)
    
    print(f"\n✓ Update complete!")
    print(f"  - Updated: {updated_count} authors (filled empty values)")
    print(f"  - Added: {new_count} new authors")
    print(f"  - Skipped: {skipped_count} (already had values)")
    print(f"  - Total: {len(author_map)} authors")
    print(f"\nSaved to: {map_path}")
    
    # Count remaining empty values
    empty_count = sum(1 for v in author_map.values() if not v)
    if empty_count > 0:
        print(f"\n⚠ {empty_count} authors still need folder IDs")
        print("\nTo see which authors are missing, run:")
        print("  cd audiobook_catalog")
        print("  python run_tests.py test_catalog_completeness -v")

if __name__ == '__main__':
    update_author_map()
