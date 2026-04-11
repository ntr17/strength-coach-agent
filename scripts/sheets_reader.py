"""
sheets_reader.py — Read-only Google Sheets access via service account.

Used to read SESSION_INPUT and HEALTH_INPUT tabs from the permanent coaching input sheet.
Credentials from GOOGLE_SERVICE_ACCOUNT_JSON env var (same as drive_client.py).
"""

import json
import os

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def get_credentials():
    from google.oauth2.service_account import Credentials
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set.")
    return Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)


def get_sheets_service():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=get_credentials())


def read_tab(sheet_id: str, tab_name: str, skip_header: bool = True) -> list[list[str]]:
    """
    Read all rows from a sheet tab. Returns list of lists (raw string values).
    Skips header row by default.
    """
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=tab_name,
    ).execute()
    rows = result.get("values", [])
    if skip_header and rows:
        return rows[1:]
    return rows
