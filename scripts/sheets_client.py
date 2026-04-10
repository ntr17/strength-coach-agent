"""
sheets_client.py — Google Sheets + Drive auth and parsing.

Auth: service account JSON from GOOGLE_SERVICE_ACCOUNT_JSON env var.
No browser flow, no token files — clean for GitHub Actions.

Usage:
    from sheets_client import get_client, get_drive_service, parse_week_tab
"""

import json
import os
import re
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive",
]

PROGRAM_SHEET_ID = os.environ.get("PROGRAM_SHEET_ID", "")
DRIVE_FOLDER_ID  = os.environ.get("DRIVE_FOLDER_ID", "1Zi6dFQA2lCRickf6XYpfedIiFPRHrTpn")


def get_credentials() -> Credentials:
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON env var not set. "
            "Set it to the full JSON content of your service account key."
        )
    info = json.loads(sa_json)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_client() -> gspread.Client:
    return gspread.authorize(get_credentials())


def get_drive_service():
    """Return authenticated Google Drive v3 service."""
    return build("drive", "v3", credentials=get_credentials())


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_done(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    s = str(value).strip()
    if "yes" in s.lower():
        return True
    if "no" in s.lower():
        return False
    return None


def _parse_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _detect_exercise_columns(header_row: list) -> dict:
    col_map = {
        "name": 0, "weight": 1, "sets_reps": 2, "done": 3, "actual": 4,
        "program_note": None, "session_note": None, "rpe": None,
    }
    for i, cell in enumerate(header_row):
        if i == 0:
            continue
        label = str(cell).strip().lower()
        if not label:
            continue
        if label in ("weight", "load"):
            col_map["weight"] = i
        elif "set" in label or "rep" in label:
            col_map["sets_reps"] = i
        elif label in ("done", "completed", "status"):
            col_map["done"] = i
        elif "actual" in label:
            col_map["actual"] = i
        elif any(kw in label for kw in ("coach note", "program note", "instruction")):
            col_map["program_note"] = i
        elif any(kw in label for kw in ("session note", "athlete note", "my note")):
            col_map["session_note"] = i
        elif any(kw in label for kw in ("rpe", "effort", "exertion", "rir")):
            col_map["rpe"] = i
        elif label in ("notes", "note") and col_map["program_note"] is None and col_map["session_note"] is None:
            col_map["program_note"] = i
    return col_map


def parse_week_tab(rows: list) -> dict:
    """Parse a week tab (list of raw rows from gspread) into structured data."""
    result = {
        "title": "",
        "days": [],
        "weekly_notes": {"bodyweight": None, "sleep": None, "energy": None, "notes": None},
    }
    current_day = None
    in_weekly_notes = False
    col_map = None

    for row in rows:
        if not row:
            continue
        col0 = str(row[0]).strip() if row[0] is not None else ""

        if not result["title"] and col0.upper().startswith("WEEK"):
            result["title"] = col0
            continue

        if "WEEKLY NOTES" in col0.upper():
            in_weekly_notes = True
            if current_day is not None:
                result["days"].append(current_day)
                current_day = None
            continue

        if in_weekly_notes:
            if "bodyweight" in col0.lower() or "peso" in col0.lower():
                result["weekly_notes"]["bodyweight"] = _parse_float(row[1] if len(row) > 1 else None)
            elif "sleep" in col0.lower() or "sueno" in col0.lower():
                result["weekly_notes"]["sleep"] = _parse_float(row[1] if len(row) > 1 else None)
            elif "energy" in col0.lower() or "energia" in col0.lower():
                result["weekly_notes"]["energy"] = _parse_float(row[1] if len(row) > 1 else None)
            elif col0.rstrip(":").lower() in ("notes", "note", "notas", "weekly notes"):
                note_parts = [str(c).strip() for c in row[1:] if c is not None and str(c).strip()]
                result["weekly_notes"]["notes"] = " ".join(note_parts) if note_parts else None
            continue

        if col0.upper().startswith("DAY "):
            if current_day is not None:
                result["days"].append(current_day)
            session_date = None
            for i in range(1, len(row)):
                cell_str = str(row[i]).strip() if row[i] else ""
                if cell_str.lower().startswith("date:"):
                    try:
                        session_date = datetime.strptime(cell_str[5:].strip(), "%Y-%m-%d").date()
                    except ValueError:
                        pass
                elif re.match(r"\d{4}-\d{2}-\d{2}", cell_str):
                    try:
                        session_date = datetime.strptime(cell_str[:10], "%Y-%m-%d").date()
                    except ValueError:
                        pass
            current_day = {"label": col0, "date": session_date, "exercises": []}
            continue

        if col0 == "Exercise":
            col_map = _detect_exercise_columns(row)
            continue

        if current_day is not None and col0 and col0 not in ("", "-"):
            def _cell(idx):
                if idx is None or idx >= len(row):
                    return None
                v = row[idx]
                return str(v).strip() if v is not None and str(v).strip() else None

            cm = col_map or {"weight": 1, "sets_reps": 2, "done": 3, "actual": 4,
                             "program_note": 5, "session_note": None, "rpe": None}
            current_day["exercises"].append({
                "name": col0,
                "weight": _cell(cm["weight"]),
                "sets_reps": _cell(cm["sets_reps"]),
                "done": _parse_done(_cell(cm["done"])),
                "actual": _cell(cm["actual"]),
                "program_note": _cell(cm.get("program_note")),
                "session_note": _cell(cm.get("session_note")),
                "rpe": _cell(cm.get("rpe")),
            })

    if current_day is not None:
        result["days"].append(current_day)
    return result


def parse_overview(rows: list) -> dict:
    """Parse Overview tab -> goals dict."""
    goals = {}
    in_goals = False
    for row in rows:
        if not row or not row[0]:
            continue
        col0 = str(row[0]).strip()
        if "30-WEEK GOALS" in col0:
            in_goals = True
            continue
        if in_goals:
            if col0 in ("Lift", "6-BLOCK STRUCTURE", "WEEKLY STRUCTURE"):
                if col0 != "Lift":
                    in_goals = False
                continue
            if len(row) >= 3 and row[1] and row[2]:
                goals[col0] = {
                    "start": str(row[1]).strip(),
                    "goal": str(row[2]).strip(),
                    "gain": str(row[3]).strip() if len(row) > 3 and row[3] else None,
                }
    return goals


def parse_progression(rows: list) -> dict:
    """Parse 30-Week Progression tab -> {week_num: {lift: weight, ...}}"""
    headers = []
    progression = {}
    for row in rows:
        if not row or not row[0]:
            continue
        col0 = str(row[0]).strip()
        if col0 == "Week":
            headers = [str(c).strip() if c else "" for c in row]
            continue
        if headers and col0.replace(".", "").isdigit():
            week_num = int(float(col0))
            week_data = {}
            for i, header in enumerate(headers[3:], start=3):
                if i < len(row) and row[i] is not None and header:
                    week_data[header] = str(row[i]).strip()
            week_data["type"] = str(row[2]).strip() if len(row) > 2 and row[2] else "PROGRESS"
            week_data["block"] = int(float(str(row[1]))) if len(row) > 1 and row[1] else 0
            progression[week_num] = week_data
    return progression


def parse_daily_log(rows: list, limit: int = 90) -> list:
    """Parse Daily Log tab -> list of dicts, most recent first."""
    entries = []
    found_header = False
    for row in rows:
        if not row or not row[0]:
            continue
        col0 = str(row[0]).strip()
        if col0.lower() == "date":
            found_header = True
            continue
        if not found_header:
            continue
        entry_date = None
        try:
            entry_date = datetime.strptime(col0[:10], "%Y-%m-%d").date()
        except ValueError:
            try:
                entry_date = datetime.fromordinal(
                    datetime(1899, 12, 30).toordinal() + int(float(col0))
                ).date()
            except (ValueError, TypeError):
                continue
        entry = {
            "date": entry_date,
            "bodyweight": _parse_float(row[1] if len(row) > 1 else None),
            "steps": _parse_float(row[2] if len(row) > 2 else None),
            "sleep": _parse_float(row[3] if len(row) > 3 else None),
            "food_quality": _parse_float(row[4] if len(row) > 4 else None),
            "sun": str(row[5]).strip().upper() if len(row) > 5 and row[5] else None,
            "notes": str(row[6]).strip() if len(row) > 6 and row[6] else None,
        }
        sun = entry["sun"]
        entry["sun"] = True if sun in ("Y", "YES", "1", "TRUE", "S") else (False if sun in ("N", "NO", "0", "FALSE") else None)
        entries.append(entry)
    return list(reversed(entries))[-limit:]


def read_program_data(sheet_id: str = None, week_num: int = None, lookback: int = 4) -> dict:
    """Read full program data from Google Sheet."""
    sid = sheet_id or PROGRAM_SHEET_ID
    if not sid:
        raise ValueError("No PROGRAM_SHEET_ID configured")
    client = get_client()
    sheet = client.open_by_key(sid)
    result = {"goals": {}, "progression": {}, "current_week": None, "daily_log": []}
    try:
        ws = sheet.worksheet("Overview")
        result["goals"] = parse_overview(ws.get_all_values())
    except Exception:
        pass
    for tab_name in ("30-Week Progression", "Progression", "30-Week Targets"):
        try:
            ws = sheet.worksheet(tab_name)
            result["progression"] = parse_progression(ws.get_all_values())
            break
        except Exception:
            continue
    if week_num:
        for tab_name in (f"Week {week_num}", f"Semana {week_num}", f"W{week_num}"):
            try:
                ws = sheet.worksheet(tab_name)
                result["current_week"] = parse_week_tab(ws.get_all_values())
                break
            except Exception:
                continue
    for tab_name in ("Daily Log", "Log", "Diario"):
        try:
            ws = sheet.worksheet(tab_name)
            result["daily_log"] = parse_daily_log(ws.get_all_values())
            break
        except Exception:
            continue
    return result


def infer_week_from_sheet(sheet_id: str = None, max_weeks: int = 35) -> int:
    """Return the highest week tab with at least one done=True exercise."""
    sid = sheet_id or PROGRAM_SHEET_ID
    if not sid:
        return 1
    try:
        client = get_client()
        sheet = client.open_by_key(sid)
    except Exception:
        return 1
    last_active = 1
    for w in range(1, max_weeks + 1):
        week_data = None
        for tab_name in (f"Week {w}", f"Semana {w}", f"W{w}"):
            try:
                ws = sheet.worksheet(tab_name)
                week_data = parse_week_tab(ws.get_all_values())
                break
            except Exception:
                continue
        if week_data is None:
            break
        if any(ex.get("done") is True for day in week_data.get("days", []) for ex in day.get("exercises", [])):
            last_active = w
    return last_active


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
        raise ValueError("No Drive folder ID configured (DRIVE_FOLDER_ID env var or folder_id arg)")

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
        mime = "text/markdown" if filename.endswith(".md") else "application/json"
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime, resumable=False)
        if filename in existing:
            service.files().update(fileId=existing[filename], media_body=media).execute()
            print(f"  [drive] Updated: {filename}")
        else:
            service.files().create(
                body={"name": filename, "parents": [fid]},
                media_body=media,
                fields="id",
            ).execute()
            print(f"  [drive] Created: {filename}")
