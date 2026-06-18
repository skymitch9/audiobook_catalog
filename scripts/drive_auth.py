"""
Shared Google Drive OAuth helper.
Handles credential management for Drive API access (read + write).

Setup:
1. Enable Google Drive API: https://console.cloud.google.com/apis/library/drive.googleapis.com
2. Create OAuth 2.0 Client ID (Desktop app): https://console.cloud.google.com/apis/credentials
3. Download JSON and save as 'credentials.json' in this directory (scripts/)
4. First run will open a browser for authorization
"""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read + write files and folders the app created or user granted access to.
# If you change scopes, delete token.json to re-authorize.
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]

SCRIPTS_DIR = Path(__file__).resolve().parent
TOKEN_PATH = SCRIPTS_DIR / "token.json"
CREDENTIALS_PATH = SCRIPTS_DIR / "credentials.json"


def get_credentials() -> Credentials | None:
    """Get valid OAuth2 credentials, refreshing or re-authorizing as needed."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                print("ERROR: credentials.json not found in scripts/ directory!")
                print()
                print("Setup instructions:")
                print("1. Go to: https://console.cloud.google.com/apis/credentials")
                print("2. Create OAuth 2.0 Client ID (Desktop app)")
                print("3. Download JSON and save as 'scripts/credentials.json'")
                return None

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist for next run
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    return creds


def build_drive_service():
    """Build and return a Google Drive v3 service instance."""
    creds = get_credentials()
    if not creds:
        return None
    return build("drive", "v3", credentials=creds)
