"""
Google Sheets reader for the training program sheet.
Parses week tabs, Daily Log, Overview, and 30-Week Progression.
"""

import re
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import json

from config import (
    CREDENTIALS_FILE, TOKEN_FILE, GOOGLE_SCOPES, GOOGLE_SCOPES_FULL,
    PROGRAM_SHEET_ID, compute_current_week, resolve_program_start_date
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_credentials() -> Credentials:
    """
    Get or refresh OAuth credentials. Opens browser on first run.
    Existing tokens are loaded with GOOGLE_SCOPES (no gmail.readonly required).
    New tokens (browser flow) request GOOGLE_SCOPES_FULL (includes gmail.readonly).
    This means reply reading activates automatically after the user re-auths.
    """
    creds = None

    if TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)
        # Use the scopes stored in the token, falling back to GOOGLE_SCOPES.
        # This avoids refresh failures when the token has fewer scopes than requested.
        stored_scopes = token_data.get("scopes", GOOGLE_SCOPES)
        creds = Credentials.from_authorized_user_info(token_data, stored_scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Browser flow: request full scopes so new tokens include gmail.readonly
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GOOGLE_SCOPES_FULL
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.parent.mkdir(exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


def get_client() -> gspread.Client:
    from gspread.http_client import BackOffHTTPClient
    return gspread.authorize(get_credentials(), http_client=BackOffHTTPClient)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_done(value: Optional[str]) -> Optional[bool]:
    """Parse the Done cell. Returns True/False/None (None = not yet marked)."""
    if value is None:
        return None
    s = str(value).strip()
    if "yes" in s.lower():
        return True
    if "no" in s.lower():
        return False
    return None  # ☐ or empty = not yet marked


def _parse_float(value) -> Optional[float]:
    """Try to extract a float from a cell value."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Week tab parser
# ---------------------------------------------------------------------------

def _detect_exercise_columns(header_row: list) -> dict:
    """
    Given the header row of a week tab (the row starting with "Exercise"),
    return a mapping of field name -> column index.

    Standard layout: Exercise(0) | Weight(1) | Sets x Reps(2) | Done(3) | Actual(4) | Notes(5)
    Extended layout adds: Session Notes(6) or any renamed column.

    The mapping is flexible: it reads column names and maps them intelligently.
    """
    col_map = {
        "name": 0,
        "weight": 1,
        "sets_reps": 2,
        "done": 3,
        "actual": 4,
        "program_note": None,
        "session_note": None,
    }

    for i, cell in enumerate(header_row):
        if i == 0:
            continue
        label = str(cell).strip().lower()
        if not label:
            continue
        if label in ("weight", "load"):
            col_map["weight"] = i
        elif "set" in label or "rep" in label or "x rep" in label:
            col_map["sets_reps"] = i
        elif label in ("done", "completed", "status"):
            col_map["done"] = i
        elif "actual" in label:
            col_map["actual"] = i
        elif any(kw in label for kw in ("coach note", "program note", "instruction")):
            col_map["program_note"] = i
        elif any(kw in label for kw in ("session note", "athlete note", "my note", "user note")):
            col_map["session_note"] = i
        elif label in ("notes", "note") and col_map["program_note"] is None and col_map["session_note"] is None:
            # Single notes column — treat as program note; agent context will explain the distinction
            col_map["program_note"] = i

    return col_map


def _parse_week_tab(rows: list[list]) -> dict:
    """
    Parse a week tab (list of rows) into structured data.
    Column layout is detected dynamically from the header row.

    Returns:
        {
            "title": "WEEK 7 — Block 2",
            "days": [
                {
                    "label": "DAY 1: Squat + Bench Heavy (~50 min)",
                    "date": None,        # date the session was done
                    "exercises": [
                        {
                            "name": "Squat",
                            "weight": "92.5kg",
                            "sets_reps": "4x4",
                            "done": True,   # True/False/None
                            "actual": None,
                            "program_note": "pause at bottom",
                            "session_note": "felt heavy",
                        }
                    ]
                }
            ],
            "weekly_notes": {
                "bodyweight": 82.5,
                "sleep": 7.0,
                "energy": 7,
                "notes": "...",
            }
        }
    """
    result = {
        "title": "",
        "days": [],
        "weekly_notes": {
            "bodyweight": None,
            "sleep": None,
            "energy": None,
            "notes": None,
        }
    }

    current_day = None
    in_weekly_notes = False
    col_map = None  # detected from the "Exercise" header row

    for row in rows:
        if not row:
            continue

        col0 = str(row[0]).strip() if row[0] is not None else ""

        # Week title (first non-empty row)
        if not result["title"] and col0.upper().startswith("WEEK"):
            result["title"] = col0
            continue

        # Weekly notes section
        if "WEEKLY NOTES" in col0.upper():
            in_weekly_notes = True
            if current_day is not None:
                result["days"].append(current_day)
                current_day = None
            continue

        if in_weekly_notes:
            if "bodyweight" in col0.lower() or "peso" in col0.lower():
                result["weekly_notes"]["bodyweight"] = _parse_float(row[1] if len(row) > 1 else None)
            elif "sleep" in col0.lower() or "sueño" in col0.lower():
                result["weekly_notes"]["sleep"] = _parse_float(row[1] if len(row) > 1 else None)
            elif "energy" in col0.lower() or "energía" in col0.lower():
                result["weekly_notes"]["energy"] = _parse_float(row[1] if len(row) > 1 else None)
            elif col0.rstrip(":").lower() in ("notes", "note", "notas", "weekly notes"):
                note_parts = [str(c).strip() for c in row[1:] if c is not None and str(c).strip()]
                result["weekly_notes"]["notes"] = " ".join(note_parts) if note_parts else None
            continue

        # New day section
        if col0.upper().startswith("DAY "):
            if current_day is not None:
                result["days"].append(current_day)

            session_date = None
            for i in range(1, len(row)):
                cell = row[i]
                if cell is None:
                    continue
                cell_str = str(cell).strip()
                if cell_str.lower().startswith("date:"):
                    date_str = cell_str[5:].strip()
                    try:
                        session_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        pass
                elif re.match(r"\d{4}-\d{2}-\d{2}", cell_str):
                    try:
                        session_date = datetime.strptime(cell_str[:10], "%Y-%m-%d").date()
                    except ValueError:
                        pass

            current_day = {
                "label": col0,
                "date": session_date,
                "exercises": [],
            }
            continue

        # Header row — detect column layout
        if col0 == "Exercise":
            col_map = _detect_exercise_columns(row)
            continue

        # Exercise row
        if current_day is not None and col0 and col0 not in ("", "—"):
            row_len = len(row)

            def _cell(idx):
                if idx is None or idx >= row_len:
                    return None
                v = row[idx]
                return str(v).strip() if v is not None and str(v).strip() else None

            # Use detected col_map if available, else fall back to positional defaults
            cm = col_map or {"weight": 1, "sets_reps": 2, "done": 3, "actual": 4,
                             "program_note": 5, "session_note": None}

            exercise = {
                "name": col0,
                "weight": _cell(cm["weight"]),
                "sets_reps": _cell(cm["sets_reps"]),
                "done": _parse_done(_cell(cm["done"])),
                "actual": _cell(cm["actual"]),
                "program_note": _cell(cm.get("program_note")),
                "session_note": _cell(cm.get("session_note")),
            }
            current_day["exercises"].append(exercise)

    # Don't forget the last day if no weekly notes section found
    if current_day is not None:
        result["days"].append(current_day)

    return result


# ---------------------------------------------------------------------------
# Overview parser
# ---------------------------------------------------------------------------

def _parse_overview(rows: list[list]) -> dict:
    """Parse Overview tab into goals dict."""
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


# ---------------------------------------------------------------------------
# 30-Week Progression parser
# ---------------------------------------------------------------------------

def _parse_progression(rows: list[list]) -> dict:
    """
    Parse the 30-Week Progression tab.
    Returns dict: week_num -> {lift: target_weight_str, ...}
    """
    headers = []
    progression = {}

    for row in rows:
        if not row or not row[0]:
            continue
        col0 = str(row[0]).strip()

        if col0 == "Week":
            # Header row
            headers = [str(c).strip() if c else "" for c in row]
            continue

        if headers and col0.replace(".", "").isdigit():
            week_num = int(float(col0))
            week_data = {}
            for i, header in enumerate(headers[3:], start=3):  # skip Week, Block, Type
                if i < len(row) and row[i] is not None and header:
                    week_data[header] = str(row[i]).strip()
            week_data["type"] = str(row[2]).strip() if len(row) > 2 and row[2] else "PROGRESS"
            week_data["block"] = int(float(str(row[1]))) if len(row) > 1 and row[1] else 0
            progression[week_num] = week_data

    return progression


# ---------------------------------------------------------------------------
# Daily Log parser
# ---------------------------------------------------------------------------

def _parse_daily_log(rows: list[list], limit: int = 30) -> list[dict]:
    """
    Parse the Daily Log tab.
    Columns: Date | Bodyweight | Steps | Sleep | Food Quality | Sun | Notes
    Returns list of dicts, most recent first, limited to `limit` rows.
    """
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

        # Parse date
        entry_date = None
        try:
            entry_date = datetime.strptime(col0[:10], "%Y-%m-%d").date()
        except ValueError:
            # Try as Excel date serial
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
        # Convert sun to bool
        if entry["sun"] in ("Y", "YES", "1", "TRUE", "S"):
            entry["sun"] = True
        elif entry["sun"] in ("N", "NO", "0", "FALSE"):
            entry["sun"] = False
        else:
            entry["sun"] = None

        entries.append(entry)

    # Return most recent first, limited
    return list(reversed(entries))[-limit:]


# ---------------------------------------------------------------------------
# Main read function
# ---------------------------------------------------------------------------

def get_program_sheet_id(sheet_id: Optional[str] = None) -> str:
    """
    Resolve the active program sheet ID.
    Priority: explicit argument > PROGRAM_SHEET_ID env var > Coach Memory registry.
    Raises ValueError if none found.
    """
    if sheet_id:
        return sheet_id
    if PROGRAM_SHEET_ID:
        return PROGRAM_SHEET_ID
    # Fall back to registry
    from memory import get_active_program_sheet_id
    registry_id = get_active_program_sheet_id()
    if registry_id:
        return registry_id
    raise ValueError(
        "No program sheet ID found. Set PROGRAM_SHEET_ID in .env or register a program sheet "
        "via: python src/memory.py --register-program"
    )


def read_program_data(week_num: Optional[int] = None, lookback: int = 3,
                      sheet_id: Optional[str] = None) -> dict:
    """
    Read all relevant data from the training program sheet.

    Args:
        week_num: Override training week number. If None, auto-computed from start date.
        lookback: How many previous weeks to include for trend analysis.
        sheet_id: Override program sheet ID (else resolved via get_program_sheet_id()).

    Returns structured dict with all data the coach needs.
    """
    if week_num is None:
        week_num = compute_current_week(resolve_program_start_date())

    resolved_sheet_id = get_program_sheet_id(sheet_id)
    client = get_client()
    sheet = client.open_by_key(resolved_sheet_id)

    result = {
        "current_week_num": week_num,
        "goals": {},
        "progression": {},
        "current_week": None,
        "prev_week_carryover": None,  # previous week tab if it has recent/unmarked sessions
        "recent_weeks": [],
        "daily_log": [],
    }

    # Overview
    try:
        ws = sheet.worksheet("Overview")
        result["goals"] = _parse_overview(ws.get_all_values())
    except gspread.WorksheetNotFound:
        pass

    # 30-Week Progression (try standard name and variants)
    for prog_tab in ("30-Week Progression", "Progression", "30-Week Targets"):
        try:
            ws = sheet.worksheet(prog_tab)
            result["progression"] = _parse_progression(ws.get_all_values())
            break
        except gspread.WorksheetNotFound:
            pass

    # Current week tab (try "Week N" and "Semana N" variants)
    for tab_name in (f"Week {week_num}", f"Semana {week_num}", f"W{week_num}"):
        try:
            ws = sheet.worksheet(tab_name)
            result["current_week"] = _parse_week_tab(ws.get_all_values())
            result["current_week"]["week_num"] = week_num
            break
        except gspread.WorksheetNotFound:
            pass

    # Previous week — include if it has sessions that look recent or incomplete.
    # "Recent" = any session with no date, or date within last 10 days.
    if week_num > 1:
        prev_num = week_num - 1
        for tab_name in (f"Week {prev_num}", f"Semana {prev_num}", f"W{prev_num}"):
            try:
                ws = sheet.worksheet(tab_name)
                prev_data = _parse_week_tab(ws.get_all_values())
                prev_data["week_num"] = prev_num

                from datetime import date as _date
                today = _date.today()
                has_recent = False
                for day in prev_data.get("days", []):
                    d = day.get("date")
                    # Include if: session date is within 10 days OR any session has no date
                    # and has at least one exercise marked done or unknown
                    exercises = day.get("exercises", [])
                    has_content = any(e.get("done") is not None or e.get("session_note") for e in exercises)
                    if d is None and has_content:
                        has_recent = True
                        break
                    if d and (today - d).days <= 10:
                        has_recent = True
                        break

                if has_recent:
                    result["prev_week_carryover"] = prev_data
                break
            except gspread.WorksheetNotFound:
                pass

    # Recent weeks for trend context (lookback, excluding the prev_week_carryover if already loaded)
    carryover_num = result["prev_week_carryover"]["week_num"] if result["prev_week_carryover"] else None
    for w in range(max(1, week_num - lookback), week_num):
        if w == carryover_num:
            continue  # already included as carryover
        for tab_name in (f"Week {w}", f"Semana {w}", f"W{w}"):
            try:
                ws = sheet.worksheet(tab_name)
                week_data = _parse_week_tab(ws.get_all_values())
                week_data["week_num"] = w
                result["recent_weeks"].append(week_data)
                break
            except gspread.WorksheetNotFound:
                pass

    # Daily Log (optional tab, try name variants)
    for log_tab in ("Daily Log", "Log", "Diario"):
        try:
            ws = sheet.worksheet(log_tab)
            result["daily_log"] = _parse_daily_log(ws.get_all_values())
            break
        except gspread.WorksheetNotFound:
            pass

    return result


# ---------------------------------------------------------------------------
# Sheet-derived week inference
# ---------------------------------------------------------------------------

def infer_week_from_sheet(sheet_id: str = None, max_weeks: int = 35) -> int:
    """
    Infer the current training week from actual sheet data.

    Searches week tabs from (calendar_week+2) downwards. Returns:
    - The highest week that has at least one Done=Yes entry and is NOT fully done
      (i.e., at least one named exercise is undone) → athlete is still in this week.
    - If the highest started week IS fully done → return week+1 (athlete finished it).
    - Falls back to compute_current_week() if sheet is unreadable or no activity found.

    This prevents the calendar-math off-by-one that fires when the athlete hasn't started
    the calendar-computed week yet but still has undone sessions in the previous week.
    """
    try:
        calendar_week = compute_current_week(resolve_program_start_date())
        client = get_client()
        sheet = client.open_by_key(get_program_sheet_id(sheet_id))

        scan_start = min(calendar_week + 2, max_weeks)
        scan_end = max(1, calendar_week - 4)

        for w in range(scan_start, scan_end - 1, -1):
            data = None
            for tab_name in (f"Week {w}", f"Semana {w}", f"W{w}"):
                try:
                    ws = sheet.worksheet(tab_name)
                    data = _parse_week_tab(ws.get_all_values())
                    break
                except gspread.WorksheetNotFound:
                    continue

            if data is None:
                continue

            named_exs = [
                ex
                for day in data.get("days", [])
                for ex in day.get("exercises", [])
                if ex.get("name")
            ]
            if not named_exs:
                continue

            done_count = sum(1 for ex in named_exs if ex.get("done") is True)
            if done_count == 0:
                continue  # week not started — skip

            # Found the highest week with any activity
            all_done = done_count == len(named_exs)
            if all_done:
                return min(w + 1, max_weeks)  # fully complete — athlete is on next week
            return w  # in progress

        return calendar_week
    except Exception:
        return compute_current_week(resolve_program_start_date())


# ---------------------------------------------------------------------------
# Write-back (used only when agent decides to update the sheet)
# ---------------------------------------------------------------------------

def update_exercise_cell(week_num: int, day_index: int, exercise_name: str,
                          field: str, value: str) -> bool:
    """
    Update a specific cell in the program sheet.
    field: "done", "actual", "notes", or "weight"
    Returns True if successful.
    """
    field_to_col = {"weight": 1, "done": 3, "actual": 4, "notes": 5}
    if field not in field_to_col:
        return False

    client = get_client()
    sheet = client.open_by_key(PROGRAM_SHEET_ID)

    try:
        ws = sheet.worksheet(f"Week {week_num}")
        rows = ws.get_all_values()

        # Find the exercise row
        day_count = -1
        for i, row in enumerate(rows):
            if row and str(row[0]).startswith("DAY "):
                day_count += 1
            if day_count == day_index and row and row[0] == exercise_name:
                # gspread is 1-indexed
                col = field_to_col[field] + 1
                ws.update_cell(i + 1, col, value)
                return True
    except Exception:
        pass

    return False


def append_daily_log_entry(entry: dict) -> bool:
    """
    Append a row to the Daily Log tab.
    entry: dict with keys matching Daily Log columns.
    """
    client = get_client()
    sheet = client.open_by_key(PROGRAM_SHEET_ID)

    try:
        ws = sheet.worksheet("Daily Log")
        row = [
            str(entry.get("date", "")),
            entry.get("bodyweight", ""),
            entry.get("steps", ""),
            entry.get("sleep", ""),
            entry.get("food_quality", ""),
            "Y" if entry.get("sun") else ("N" if entry.get("sun") is False else ""),
            entry.get("notes", ""),
        ]
        ws.append_row(row)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Dev/test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("Reading program data...")
    data = read_program_data()

    print(f"\nGoals ({len(data['goals'])} lifts):")
    for lift, g in data["goals"].items():
        print(f"  {lift}: {g['start']} → {g['goal']}")

    print(f"\nProgression table: {len(data['progression'])} weeks loaded")

    if data["current_week"]:
        cw = data["current_week"]
        print(f"\nCurrent week: {cw['title']} (Week {cw['week_num']})")
        for day in cw["days"]:
            done_count = sum(1 for e in day["exercises"] if e["done"] is True)
            print(f"  {day['label']}: {done_count}/{len(day['exercises'])} exercises done", end="")
            print(f" | date: {day['date']}" if day["date"] else "")

    print(f"\nRecent weeks: {[w['week_num'] for w in data['recent_weeks']]}")
    print(f"Daily log entries: {len(data['daily_log'])}")
