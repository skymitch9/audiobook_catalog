"""
Export Google Drive folder IDs for audiobook authors.
This script lists all folders in a specified Google Drive folder and exports them as JSON.

Setup:
1. Install: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
2. Enable Google Drive API: https://console.cloud.google.com/apis/library/drive.googleapis.com
3. Create OAuth credentials: https://console.cloud.google.com/apis/credentials
4. Download credentials.json and place in this directory
5. Run this script - it will open a browser for authorization
"""

import json
import os.path
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

def get_credentials():
    """Get valid user credentials from storage or run OAuth flow."""
    creds = None
    token_path = Path(__file__).parent / 'token.json'
    
    # The file token.json stores the user's access and refresh tokens
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            credentials_path = Path(__file__).parent / 'credentials.json'
            if not credentials_path.exists():
                print("ERROR: credentials.json not found!")
                print("\nSetup instructions:")
                print("1. Go to: https://console.cloud.google.com/apis/credentials")
                print("2. Create OAuth 2.0 Client ID (Desktop app)")
                print("3. Download JSON and save as 'credentials.json' in this directory")
                return None
            
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    
    return creds

def list_folders(service, parent_folder_id=None, max_results=1000):
    """List all folders in Google Drive or within a specific parent folder."""
    try:
        query = "mimeType='application/vnd.google-apps.folder'"
        if parent_folder_id:
            query += f" and '{parent_folder_id}' in parents"
        query += " and trashed=false"
        
        results = service.files().list(
            q=query,
            pageSize=max_results,
            fields="nextPageToken, files(id, name, parents)",
            orderBy="name"
        ).execute()
        
        items = results.get('files', [])
        
        # Handle pagination if there are more results
        while 'nextPageToken' in results:
            results = service.files().list(
                q=query,
                pageSize=max_results,
                fields="nextPageToken, files(id, name, parents)",
                orderBy="name",
                pageToken=results['nextPageToken']
            ).execute()
            items.extend(results.get('files', []))
        
        return items
    
    except HttpError as error:
        print(f'An error occurred: {error}')
        return []

def export_author_folders(parent_folder_id=None):
    """Export all author folders from Google Drive."""
    creds = get_credentials()
    if not creds:
        return
    
    try:
        service = build('drive', 'v3', credentials=creds)
        
        print("Fetching folders from Google Drive...")
        if parent_folder_id:
            print(f"Parent folder ID: {parent_folder_id}")
        
        folders = list_folders(service, parent_folder_id)
        
        if not folders:
            print('No folders found.')
            return
        
        print(f'\nFound {len(folders)} folders:')
        
        # Create a dictionary mapping folder names to IDs
        author_map = {}
        for folder in folders:
            name = folder['name']
            folder_id = folder['id']
            author_map[name] = folder_id
            print(f'  - {name}: {folder_id}')
        
        # Save to JSON file
        output_path = Path(__file__).parent.parent / 'author_drive_map_export.json'
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(author_map, f, ensure_ascii=False, indent=2)
        
        print(f'\n✓ Exported {len(author_map)} folders to: {output_path}')
        print('\nNext steps:')
        print('1. Review the exported file')
        print('2. Merge with existing author_drive_map.json')
        print('3. Run: python -m app.tools.generate_author_map (to add any missing authors)')
        
    except HttpError as error:
        print(f'An error occurred: {error}')

def main():
    """Main function with interactive prompts."""
    print("=" * 60)
    print("Google Drive Folder Exporter for Audiobook Catalog")
    print("=" * 60)
    print()
    
    # Ask if user wants to export from a specific folder
    print("Do you want to export folders from:")
    print("1. Root of My Drive (all folders)")
    print("2. A specific folder (e.g., your Audiobooks folder)")
    print()
    
    choice = input("Enter choice (1 or 2): ").strip()
    
    parent_folder_id = None
    if choice == '2':
        print()
        print("To get the folder ID:")
        print("1. Open the folder in Google Drive")
        print("2. Copy the ID from the URL:")
        print("   https://drive.google.com/drive/folders/FOLDER_ID_HERE")
        print()
        parent_folder_id = input("Enter parent folder ID: ").strip()
        if not parent_folder_id:
            print("No folder ID provided, using root.")
            parent_folder_id = None
    
    print()
    export_author_folders(parent_folder_id)

if __name__ == '__main__':
    main()
