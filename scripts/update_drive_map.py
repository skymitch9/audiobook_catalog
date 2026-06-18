"""Update author_drive_map.json from current Google Drive folder listing."""
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from drive_auth import build_drive_service

DRIVE_PARENT_FOLDER_ID = "1yZHU_UryCZkuhg9zFzu5uOadx3NI0FJv"


def main():
    service = build_drive_service()
    if not service:
        print("[ERROR] Auth failed")
        return

    # Fetch all folders from Drive
    folders = {}
    page_token = None
    while True:
        results = service.files().list(
            q=f"'{DRIVE_PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=1000,
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        for f in results.get("files", []):
            folders[f["name"]] = f["id"]
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    # Build author_drive_map: folder_name -> drive URL
    author_map = {}
    for name, fid in sorted(folders.items()):
        author_map[name] = f"https://drive.google.com/drive/folders/{fid}"

    # Write to project root
    out_path = PROJECT_ROOT / "author_drive_map.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(author_map, f, ensure_ascii=False, indent=2)

    print(f"Updated {out_path} with {len(author_map)} entries")


if __name__ == "__main__":
    main()
