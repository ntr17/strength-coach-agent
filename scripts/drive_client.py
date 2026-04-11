"""
drive_client.py — Google Drive auth and file upload via service account.

Credentials from GOOGLE_SERVICE_ACCOUNT_JSON env var (full JSON content).
The service account needs Editor access on the Drive folder.
"""

import json
import os

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1Zi6dFQA2lCRickf6XYpfedIiFPRHrTpn")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
]


def get_credentials():
    """Get service account credentials from env var."""
    from google.oauth2.service_account import Credentials
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON env var not set. "
            "Set it to the full JSON content of your service account key."
        )
    info = json.loads(sa_json)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_drive_service():
    """Return authenticated Google Drive v3 service."""
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=get_credentials())


def upload_files_to_drive(files: dict, folder_id: str = None) -> None:
    """
    Upload or update files in a Drive folder.

    Args:
        files: {filename: content_str}
        folder_id: Drive folder ID (defaults to DRIVE_FOLDER_ID env var)
    """
    from googleapiclient.http import MediaInMemoryUpload

    fid = folder_id or DRIVE_FOLDER_ID
    if not fid:
        raise ValueError("No Drive folder ID configured")

    service = get_drive_service()

    # List existing files in folder
    existing = {}
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{fid}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            existing[f["name"]] = f["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    for filename, content in files.items():
        # Upload as Google Docs so Claude's Drive integration can read them.
        # MediaInMemoryUpload uses text/plain as the source format;
        # the body mimeType tells Drive to convert to a Google Doc on ingest.
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain", resumable=False)
        if filename in existing:
            service.files().update(
                fileId=existing[filename],
                media_body=media,
            ).execute()
            print(f"  [drive] Updated: {filename}")
        else:
            service.files().create(
                body={
                    "name": filename,
                    "parents": [fid],
                    "mimeType": "application/vnd.google-apps.document",
                },
                media_body=media,
                fields="id",
            ).execute()
            print(f"  [drive] Created: {filename}")
