"""
Merge exported Drive folder IDs with existing author_drive_map.json

Usage:
1. Export your Drive folders to a JSON file (author_drive_map_export.json)
2. Run: python merge_author_maps.py
3. This will merge the exported IDs with your existing map
"""

import json
from pathlib import Path

def merge_author_maps():
    """Merge exported Drive IDs with existing author map."""
    
    # Paths
    project_root = Path(__file__).parent.parent
    existing_map_path = project_root / 'author_drive_map.json'
    export_map_path = project_root / 'author_drive_map_export.json'
    backup_path = project_root / 'author_drive_map_backup.json'
    
    # Load existing map
    if not existing_map_path.exists():
        print(f"ERROR: {existing_map_path} not found!")
        return
    
    with open(existing_map_path, 'r', encoding='utf-8') as f:
        existing_map = json.load(f)
    
    print(f"Loaded existing map: {len(existing_map)} authors")
    
    # Load exported map
    if not export_map_path.exists():
        print(f"\nERROR: {export_map_path} not found!")
        print("\nPlease create this file with your Drive folder exports.")
        print("Format: {\"Author Name\": \"folder_id\", ...}")
        return
    
    with open(export_map_path, 'r', encoding='utf-8') as f:
        export_map = json.load(f)
    
    print(f"Loaded export map: {len(export_map)} folders")
    
    # Backup existing map
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(existing_map, f, ensure_ascii=False, indent=2)
    print(f"\nBackup saved to: {backup_path}")
    
    # Merge maps
    updated_count = 0
    new_count = 0
    
    for author, folder_id in export_map.items():
        if author in existing_map:
            if not existing_map[author]:  # Empty value
                existing_map[author] = folder_id
                updated_count += 1
            elif existing_map[author] != folder_id:
                print(f"\nWARNING: Conflicting ID for '{author}':")
                print(f"  Existing: {existing_map[author]}")
                print(f"  New:      {folder_id}")
                response = input("  Use new ID? (y/n): ").strip().lower()
                if response == 'y':
                    existing_map[author] = folder_id
                    updated_count += 1
        else:
            existing_map[author] = folder_id
            new_count += 1
    
    # Save merged map
    with open(existing_map_path, 'w', encoding='utf-8') as f:
        json.dump(existing_map, f, ensure_ascii=False, indent=2)
    
    print(f"\n✓ Merge complete!")
    print(f"  - Updated: {updated_count} authors")
    print(f"  - Added: {new_count} new authors")
    print(f"  - Total: {len(existing_map)} authors")
    print(f"\nSaved to: {existing_map_path}")
    
    # Count empty values
    empty_count = sum(1 for v in existing_map.values() if not v)
    if empty_count > 0:
        print(f"\n⚠ {empty_count} authors still need folder IDs")

if __name__ == '__main__':
    merge_author_maps()
