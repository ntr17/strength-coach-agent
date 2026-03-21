"""
Coach Memory Sheet — the agent's persistent brain.
Reads and writes to a dedicated Google Sheet that persists across programs.

Tabs:
  Athlete Profile   - stable personal info (user edits)
  Long-Term Goals   - multi-year aspirations (user edits)
  Lift History      - append-only session log (agent writes)
  Health Log        - append-only health data (agent writes)
  Life Context      - journal of context changes (agent appends)
  Program History   - programs run (agent updates)
  Coach Log         - agent's own notes and email summaries
  Strategic Plan    - multi-month phases and targets (agent updates weekly)
  Planning Notes    - free-form coach thinking from each planning pass
  Telegram Log      - bidirectional conversation history with athlete
"""

from datetime import date, datetime, timedelta
from typing import Optional

import gspread

from sheets import get_client
from config import MEMORY_SHEET_ID


# ---------------------------------------------------------------------------
# Tab names
# ---------------------------------------------------------------------------

TAB_PROFILE = "Athlete Profile"
TAB_GOALS = "Long-Term Goals"
TAB_LIFT_HISTORY = "Lift History"
TAB_HEALTH_LOG = "Health Log"
TAB_LIFE_CONTEXT = "Life Context"
TAB_PROGRAM_HISTORY = "Program History"
TAB_COACH_LOG = "Coach Log"
TAB_SHEET_REGISTRY = "Active Sheets"
TAB_COMMANDS = "Commands"
TAB_STRATEGIC_PLAN = "Strategic Plan"
TAB_PLANNING_NOTES = "Planning Notes"
TAB_TELEGRAM_LOG = "Telegram Log"
TAB_COACH_FOCUS = "Coach Focus"
TAB_COACH_STATE = "Coach State"
TAB_ATHLETE_PREFS = "Athlete Preferences"
TAB_TRACKED_LIFTS = "Tracked Lifts"
TAB_COMMITMENTS = "Commitments"

LIFT_HISTORY_HEADERS = ["Date", "Week", "Day", "Exercise", "Prescribed Weight",
                         "Actual Weight/Reps", "Completed", "Notes", "Est 1RM"]
HEALTH_LOG_HEADERS = ["Date", "Bodyweight (kg)", "Steps", "Sleep (hrs)",
                       "Food Quality (1-10)", "Sun (Y/N)", "Notes"]
LIFE_CONTEXT_HEADERS = ["Date", "Context"]
PROGRAM_HISTORY_HEADERS = ["Program", "Start Date", "End Date", "Weeks Completed", "Notes"]
COACH_LOG_HEADERS = ["Date", "Key Observations", "Email Summary"]
SHEET_REGISTRY_HEADERS = ["Name", "Sheet ID", "Type", "Status", "Created", "Start Date", "Total Weeks", "Notes"]
COMMANDS_HEADERS = ["Command", "Value", "Expires", "Applied"]
STRATEGIC_PLAN_HEADERS = ["Phase", "Start Date", "End Date", "Focus", "Key Targets", "Notes", "Last Updated"]
PLANNING_NOTES_HEADERS = ["Date", "Notes"]
TELEGRAM_LOG_HEADERS = ["Date", "Time", "Direction", "Message", "Processed"]
# Coach's internal tracking list — what it's watching, following up on, or has logged as landmarks
COACH_FOCUS_HEADERS = ["Date Added", "Category", "Item", "Status", "Last Mentioned", "Priority"]
# Category values: TRACKING | FOLLOWUP | LANDMARK | CONCERN
# Status values:   OPEN | RESOLVED | STALE
# Priority values: NORMAL (expire after 30d) | HIGH (expire after 90d) | PINNED (never expires)

# Coach's compressed knowledge — domain summaries written by the coach each run.
# PRIMARY prompt input (replaces raw data dump as data grows). One row per domain, upserted.
COACH_STATE_HEADERS = ["Domain", "Summary", "Confidence", "Last Updated"]
# Domain examples: SQUAT | BENCH | DEADLIFT | OHP | HEALTH | SCHEDULE | LIFESTYLE | GOALS | PROGRAM
# Confidence: HIGH | MEDIUM | LOW (based on data recency/completeness)

# Athlete's explicit preferences — how they want the coach to behave.
# Written by coach when athlete states a preference via any channel.
ATHLETE_PREFS_HEADERS = ["Category", "Preference", "Source", "Added Date"]
# Category examples: OUTPUT | TOPICS | STYLE | SCHEDULE

# Tracked lifts — which exercises the coach monitors as key lifts.
# Replaces the hardcoded KEY_LIFTS list: coach can add/remove lifts dynamically via Telegram.
TRACKED_LIFTS_HEADERS = ["Name", "Domain", "Match Pattern", "Type", "Active", "Added", "Notes"]

# Commitments — explicit coach promises to follow up on something specific.
# Separate from Coach Focus (which is reactive / observation-based).
# This is proactive: coach said "I'll check X" → it must happen.
COMMITMENTS_HEADERS = ["Date Added", "Commitment", "Due Date", "Status", "Resolved Date", "Notes"]
# Status: OPEN | RESOLVED | DEFERRED
# Name: display name, e.g. "Squat"
# Domain: Coach State domain key (uppercase, no spaces), e.g. "SQUAT"
# Match Pattern: substring matched against "Exercise" column in Lift History (case-insensitive)
# Type: MAIN | AUXILIARY | ACCESSORY
#   MAIN      — primary strength lifts (plateau detection, Coach State domain, 1RM tracking)
#   AUXILIARY — secondary lifts tracked for 1RM but not plateau-detected or Coach State domain
#   ACCESSORY — logged but not 1RM-tracked or plateau-detected
# Active: Y | N (N = soft-delete, still in history)


# ---------------------------------------------------------------------------
# Sheet access
# ---------------------------------------------------------------------------

def _get_memory_sheet() -> gspread.Spreadsheet:
    client = get_client()
    return client.open_by_key(MEMORY_SHEET_ID)


def _get_tab(sheet: gspread.Spreadsheet, name: str) -> gspread.Worksheet:
    try:
        return sheet.worksheet(name)
    except gspread.WorksheetNotFound:
        raise RuntimeError(
            f"Coach Memory tab '{name}' not found. "
            "Run `python src/memory.py --setup` to create the sheet structure."
        )


# ---------------------------------------------------------------------------
# Read functions
# ---------------------------------------------------------------------------

def read_athlete_profile() -> str:
    """Read Athlete Profile tab as raw text."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_PROFILE)
    rows = ws.get_all_values()
    lines = []
    for row in rows:
        line = " | ".join(str(c).strip() for c in row if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def read_long_term_goals() -> str:
    """Read Long-Term Goals tab as raw text."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_GOALS)
    rows = ws.get_all_values()
    lines = []
    for row in rows:
        line = " | ".join(str(c).strip() for c in row if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def get_tab_row_count(tab_name: str) -> int:
    """Return the number of data rows in a tab (excluding header). 0 if missing."""
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return 0
    rows = ws.get_all_values()
    return max(0, len(rows) - 1)


def archive_old_rows(tab_name: str, before_date: date,
                     archive_tab_name: str = None) -> int:
    """
    Move rows whose Date column is older than before_date to an archive tab.
    Creates the archive tab if it doesn't exist.
    Returns count of rows moved.
    Safe to call even if the tab has few rows — returns 0 without doing anything.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return 0

    rows = ws.get_all_values()
    if len(rows) <= 1:
        return 0

    headers = rows[0]
    archive_name = archive_tab_name or f"{tab_name} Archive"
    date_col_idx = 0  # Date is always column A

    old_rows = []
    keep_row_indices = []  # 1-indexed in gspread (row 1 = header)

    for i, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        date_str = row[date_col_idx].strip() if row else ""
        try:
            row_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            if row_date < before_date:
                old_rows.append(row)
            else:
                keep_row_indices.append(i)
        except (ValueError, TypeError):
            keep_row_indices.append(i)  # keep rows with unparseable dates

    if not old_rows:
        return 0

    # Write to archive tab
    try:
        archive_ws = sheet.worksheet(archive_name)
    except gspread.WorksheetNotFound:
        archive_ws = sheet.add_worksheet(title=archive_name, rows=2000, cols=len(headers) + 2)
        archive_ws.append_row(headers)
        print(f"    [Archive] Created tab '{archive_name}'")

    archive_ws.append_rows(old_rows)

    # Rebuild source tab: keep header + non-old rows
    keep_data = [rows[i - 1] for i in keep_row_indices]  # 0-indexed in rows list
    # Clear all data rows and rewrite
    if len(rows) > 1:
        ws.delete_rows(2, len(rows))
    if keep_data:
        ws.append_rows(keep_data)

    print(f"    [Archive] Moved {len(old_rows)} rows from '{tab_name}' → '{archive_name}'")
    return len(old_rows)


def read_lift_history(limit: int = 80, after_date: date = None) -> list[dict]:
    """
    Read last N rows of Lift History.
    after_date: if provided, only return rows on or after that date.
    Warns if tab exceeds 500 rows (scalability indicator).
    """
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFT_HISTORY)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    total_data_rows = len(rows) - 1
    if total_data_rows > 500:
        print(f"  [ScaleWarning] Lift History has {total_data_rows} rows "
              f"— consider archiving rows > 1 year old with archive_old_rows()")

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if after_date:
            try:
                row_date = datetime.strptime(entry.get("Date", "")[:10], "%Y-%m-%d").date()
                if row_date < after_date:
                    continue
            except (ValueError, TypeError):
                pass
        entries.append(entry)

    return entries[-limit:]


def read_health_log(limit: int = 30, after_date: date = None) -> list[dict]:
    """
    Read last N rows of Health Log.
    after_date: if provided, only return rows on or after that date.
    Warns if tab exceeds 500 rows.
    """
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_HEALTH_LOG)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    total_data_rows = len(rows) - 1
    if total_data_rows > 500:
        print(f"  [ScaleWarning] Health Log has {total_data_rows} rows "
              f"— consider archiving rows > 1 year old with archive_old_rows()")

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if after_date:
            try:
                row_date = datetime.strptime(entry.get("Date", "")[:10], "%Y-%m-%d").date()
                if row_date < after_date:
                    continue
            except (ValueError, TypeError):
                pass
        entries.append(entry)

    return entries[-limit:]


def read_life_context(limit: int = 10) -> list[dict]:
    """Read last N life context entries."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFE_CONTEXT)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    entries = []
    for row in rows[1:]:
        if len(row) >= 2 and any(row):
            entries.append({"date": row[0], "context": row[1] if len(row) > 1 else ""})

    return entries[-limit:]


def read_program_history() -> list[dict]:
    """Read all program history entries."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_PROGRAM_HISTORY)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))
    return entries


def read_coach_log(limit: int = 7) -> list[dict]:
    """Read last N coach log entries."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_COACH_LOG)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))

    return entries[-limit:]


def read_sheet_registry() -> list[dict]:
    """Read the Active Sheets registry."""
    sheet = _get_memory_sheet()
    try:
        ws = _get_tab(sheet, TAB_SHEET_REGISTRY)
    except RuntimeError:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))
    return entries


def read_commands() -> list[dict]:
    """
    Read the Commands tab. Each row: Command | Value | Expires | Applied.
    Rows include a '_row_index' key (1-indexed, for use with mark_command_applied).
    Returns empty list if the tab doesn't exist yet.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COMMANDS)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for i, row in enumerate(rows[1:], start=2):  # row 2 = first data row (1-indexed)
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        entry["_row_index"] = i
        entries.append(entry)
    return entries


def append_command(command: str, value: str, expires: str = "") -> None:
    """
    Append a new command row to the Commands tab.
    Used to log pending proposals (PENDING_PROPOSAL) and other agent-initiated commands.
    command: e.g. "PENDING_PROPOSAL", "SKIP_UNTIL"
    value: the proposal text or command value
    expires: optional expiry date (YYYY-MM-DD); empty = no expiry
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COMMANDS)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_COMMANDS, rows=200, cols=6)
        ws.append_row(COMMANDS_HEADERS)
    ws.append_row([command.upper(), value, expires, "N"])


def mark_command_applied(row_index: int) -> None:
    """Mark a command row as applied (sets the Applied column to Y)."""
    sheet = _get_memory_sheet()
    ws = sheet.worksheet(TAB_COMMANDS)
    applied_col = COMMANDS_HEADERS.index("Applied") + 1  # 1-indexed
    ws.update_cell(row_index, applied_col, "Y")


def check_skip_today() -> Optional[date]:
    """
    Check for an active SKIP_UNTIL command.
    Returns the skip-until date if today <= that date and Applied != Y.
    Returns None if no active skip.
    """
    today = date.today()
    for cmd in read_commands():
        if cmd.get("Command", "").upper().strip() != "SKIP_UNTIL":
            continue
        if cmd.get("Applied", "").upper().strip() == "Y":
            continue
        try:
            skip_until = datetime.strptime(cmd["Value"][:10], "%Y-%m-%d").date()
            if today <= skip_until:
                return skip_until
        except (ValueError, KeyError):
            pass
    return None


def read_strategic_plan() -> list[dict]:
    """Read the Strategic Plan tab. Returns list of phase dicts."""
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_STRATEGIC_PLAN)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))
    return entries


def upsert_strategic_plan(phases: list[dict]) -> None:
    """
    Write/update the Strategic Plan tab.
    Each dict should have keys matching STRATEGIC_PLAN_HEADERS.
    Replaces all existing data rows (clears and rewrites).
    """
    if not phases:
        return
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_STRATEGIC_PLAN)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_STRATEGIC_PLAN, rows=100, cols=10)
        ws.append_row(STRATEGIC_PLAN_HEADERS)

    # Clear existing data rows (keep header)
    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.delete_rows(2, len(existing))

    rows = []
    today = str(date.today())
    for p in phases:
        rows.append([
            p.get("Phase", ""),
            p.get("Start Date", ""),
            p.get("End Date", ""),
            p.get("Focus", ""),
            p.get("Key Targets", ""),
            p.get("Notes", ""),
            p.get("Last Updated", today),
        ])
    ws.append_rows(rows)


def append_planning_notes(notes: str, run_date: Optional[date] = None) -> None:
    """Append a free-form planning session note."""
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_PLANNING_NOTES)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_PLANNING_NOTES, rows=500, cols=5)
        ws.append_row(PLANNING_NOTES_HEADERS)
    d = str(run_date or date.today())
    ws.append_row([d, notes])


def read_planning_notes(limit: int = 3) -> list[dict]:
    """Read recent planning notes."""
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_PLANNING_NOTES)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append({"date": row[0], "notes": row[1] if len(row) > 1 else ""})
    return entries[-limit:]


def append_telegram_log(direction: str, message: str,
                         log_date: Optional[date] = None) -> None:
    """
    Append a Telegram message to the log.
    direction: 'IN' (from athlete) or 'OUT' (from coach)
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_TELEGRAM_LOG)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_TELEGRAM_LOG, rows=1000, cols=6)
        ws.append_row(TELEGRAM_LOG_HEADERS)
    now = datetime.now()
    d = str(log_date or now.date())
    t = now.strftime("%H:%M")
    ws.append_row([d, t, direction.upper(), message, "N"])


def read_telegram_log(limit: int = 10) -> list[dict]:
    """Read recent Telegram log entries."""
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_TELEGRAM_LOG)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entries.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))
    return entries[-limit:]


def read_telegram_log_since(since_date: date, limit: int = 100) -> list[dict]:
    """Read Telegram log entries on or after since_date. Used by email pipeline."""
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_TELEGRAM_LOG)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        try:
            row_date = datetime.strptime(entry.get("Date", ""), "%Y-%m-%d").date()
            if row_date >= since_date:
                entries.append(entry)
        except (ValueError, TypeError):
            pass
    return entries[-limit:]


def log_open_question(question: str, source: str = "EMAIL") -> None:
    """Log an unanswered question to Commands tab so both channels can track it."""
    append_command("OPEN_QUESTION", f"[{source}] {question}")


def get_open_questions() -> list[dict]:
    """Return unanswered OPEN_QUESTION commands."""
    return [
        c for c in read_commands()
        if c.get("Command", "").upper() == "OPEN_QUESTION"
        and c.get("Applied", "").upper() not in ("Y", "DECLINED")
    ]


def read_coach_focus(status_filter: str = "OPEN") -> list[dict]:
    """
    Read Coach Focus entries. status_filter=None returns all, 'OPEN' returns only active items.
    Coach Focus is the coach's internal watch list: what it's tracking, following up on, or has logged.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COACH_FOCUS)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if status_filter is None or entry.get("Status", "OPEN") == status_filter:
            entries.append(entry)
    return entries


def expire_stale_focus_items() -> int:
    """
    Mark OPEN Coach Focus items as STALE based on their Priority:
      NORMAL  → expire after 30 days
      HIGH    → expire after 90 days
      PINNED  → never expires
    Returns count of items expired.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COACH_FOCUS)
    except gspread.WorksheetNotFound:
        return 0
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return 0

    headers = rows[0]
    date_col = headers.index("Date Added") + 1 if "Date Added" in headers else 1
    status_col = headers.index("Status") + 1 if "Status" in headers else 4
    priority_col = headers.index("Priority") + 1 if "Priority" in headers else None
    today = date.today()
    expired = 0

    for i, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        status = row[status_col - 1].strip().upper() if len(row) >= status_col else ""
        if status != "OPEN":
            continue

        priority = ""
        if priority_col and len(row) >= priority_col:
            priority = row[priority_col - 1].strip().upper()
        if not priority:
            priority = "NORMAL"

        if priority == "PINNED":
            continue  # never expires

        threshold = 90 if priority == "HIGH" else 30
        cutoff = today - timedelta(days=threshold)

        date_str = row[date_col - 1].strip() if len(row) >= date_col else ""
        try:
            added = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            if added < cutoff:
                ws.update_cell(i, status_col, "STALE")
                expired += 1
        except (ValueError, TypeError):
            pass

    return expired


def append_coach_focus(category: str, item: str,
                        last_mentioned: str = "",
                        priority: str = "NORMAL") -> None:
    """
    Add a new item to the coach's focus list.
    category: TRACKING | FOLLOWUP | LANDMARK | CONCERN
    priority: NORMAL (30d expiry) | HIGH (90d expiry) | PINNED (never expires)
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COACH_FOCUS)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_COACH_FOCUS, rows=500, cols=8)
        ws.append_row(COACH_FOCUS_HEADERS)
    today = str(date.today())
    ws.append_row([today, category.upper(), item, "OPEN",
                   last_mentioned or today, priority.upper()])


def update_coach_focus_status(item_substring: str, new_status: str,
                               last_mentioned: str = "") -> bool:
    """
    Find a Coach Focus item by substring match and update its status.
    Returns True if found and updated, False otherwise.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COACH_FOCUS)
    except gspread.WorksheetNotFound:
        return False
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return False
    headers = rows[0]
    item_col = headers.index("Item") + 1 if "Item" in headers else 3
    status_col = headers.index("Status") + 1 if "Status" in headers else 4
    last_col = headers.index("Last Mentioned") + 1 if "Last Mentioned" in headers else 5

    needle = item_substring.lower()
    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= item_col and needle in row[item_col - 1].lower():
            ws.update_cell(i, status_col, new_status)
            if last_mentioned:
                ws.update_cell(i, last_col, last_mentioned)
            return True
    return False


def read_telegram_unprocessed(limit: int = 50) -> list[dict]:
    """
    Return Telegram messages where Processed != 'Y', oldest first.
    Used by the Telegram Processor to extract structured facts.
    Each entry includes '_row_index' for marking processed later.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_TELEGRAM_LOG)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for i, row in enumerate(rows[1:], start=2):
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if entry.get("Processed", "N").upper() != "Y":
            entry["_row_index"] = i
            entries.append(entry)
    return entries[-limit:]


def mark_telegram_processed(row_indices: list[int]) -> None:
    """Mark a list of Telegram Log rows (1-indexed) as processed."""
    if not row_indices:
        return
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_TELEGRAM_LOG)
    except gspread.WorksheetNotFound:
        return
    headers = ws.row_values(1)
    processed_col = headers.index("Processed") + 1 if "Processed" in headers else 5
    for idx in row_indices:
        ws.update_cell(idx, processed_col, "Y")


# ---------------------------------------------------------------------------
# Coach State — compressed domain summaries (Tier 1, primary prompt input)
# ---------------------------------------------------------------------------

def read_coach_state() -> dict:
    """
    Read the Coach State tab. Returns dict keyed by Domain for fast lookup.
    Example: {"SQUAT": {"summary": "...", "confidence": "HIGH", "last_updated": "2026-03-07"}}
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COACH_STATE)
    except gspread.WorksheetNotFound:
        return {}
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return {}
    headers = rows[0]
    state = {}
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        domain = entry.get("Domain", "").strip().upper()
        if domain and not domain.startswith("#"):
            state[domain] = {
                "summary": entry.get("Summary", ""),
                "confidence": entry.get("Confidence", ""),
                "last_updated": entry.get("Last Updated", ""),
            }
    return state


def upsert_coach_state(domain: str, summary: str, confidence: str = "MEDIUM") -> None:
    """
    Write or update a domain entry in the Coach State tab.
    Each domain has exactly one row — this upserts by domain name.
    Called at end of each run to keep state current and bounded.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COACH_STATE)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_COACH_STATE, rows=100, cols=6)
        ws.append_row(COACH_STATE_HEADERS)

    today = str(date.today())
    domain_upper = domain.upper().strip()
    rows = ws.get_all_values()
    headers = rows[0] if rows else COACH_STATE_HEADERS
    domain_col = headers.index("Domain") + 1 if "Domain" in headers else 1
    summary_col = headers.index("Summary") + 1 if "Summary" in headers else 2
    confidence_col = headers.index("Confidence") + 1 if "Confidence" in headers else 3
    updated_col = headers.index("Last Updated") + 1 if "Last Updated" in headers else 4

    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= domain_col and row[domain_col - 1].upper().strip() == domain_upper:
            ws.update_cell(i, summary_col, summary)
            ws.update_cell(i, confidence_col, confidence.upper())
            ws.update_cell(i, updated_col, today)
            return

    ws.append_row([domain_upper, summary, confidence.upper(), today])


# ---------------------------------------------------------------------------
# Athlete Preferences — explicit behavior flags from athlete feedback
# ---------------------------------------------------------------------------

def read_athlete_preferences() -> list[dict]:
    """
    Read all athlete preferences. Used before any output decision.
    Returns list of {category, preference, source, added_date}.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_ATHLETE_PREFS)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if not entry.get("Preference", "").startswith("#"):
            entries.append(entry)
    return entries


def append_athlete_preference(category: str, preference: str, source: str = "") -> None:
    """
    Record an explicit athlete preference.
    category: OUTPUT | TOPICS | STYLE | SCHEDULE
    source: where this came from, e.g. "Telegram 2026-03-07"
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_ATHLETE_PREFS)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_ATHLETE_PREFS, rows=200, cols=6)
        ws.append_row(ATHLETE_PREFS_HEADERS)
    today = str(date.today())
    ws.append_row([category.upper(), preference, source, today])


# ---------------------------------------------------------------------------
# Tracked Lifts — dynamic key-lift registry (replaces hardcoded KEY_LIFTS)
# ---------------------------------------------------------------------------

def read_tracked_lifts(active_only: bool = True) -> list[dict]:
    """
    Read the Tracked Lifts tab. Falls back to KEY_LIFTS from config if the tab
    is empty or missing — so the system works on first run without setup.

    Returns list of dicts with keys: name, domain, match_pattern, lift_type, active, added, notes.
    By default (active_only=True) only returns lifts where Active == 'Y'.
    """
    from config import KEY_LIFTS

    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_TRACKED_LIFTS)
    except gspread.WorksheetNotFound:
        return _key_lifts_fallback()

    rows = ws.get_all_values()
    if len(rows) <= 1:
        return _key_lifts_fallback()

    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if entry.get("Name", "").startswith("#"):
            continue
        if active_only and entry.get("Active", "Y").upper() != "Y":
            continue
        entries.append({
            "name": entry.get("Name", "").strip(),
            "domain": entry.get("Domain", "").strip().upper(),
            "match_pattern": entry.get("Match Pattern", "").strip(),
            "lift_type": entry.get("Type", "MAIN").strip().upper(),
            "active": entry.get("Active", "Y").strip().upper(),
            "added": entry.get("Added", "").strip(),
            "notes": entry.get("Notes", "").strip(),
        })

    # Fallback if tab was empty or all rows were comments
    if not entries:
        return _key_lifts_fallback()
    return entries


def _key_lifts_fallback() -> list[dict]:
    """Convert KEY_LIFTS constant into the tracked-lifts dict format."""
    from config import KEY_LIFTS
    return [
        {
            "name": lift_name,
            "domain": domain,
            "match_pattern": lift_name,
            "lift_type": "MAIN",
            "active": "Y",
            "added": "",
            "notes": "seeded from config.KEY_LIFTS",
        }
        for domain, lift_name in KEY_LIFTS
    ]


def add_tracked_lift(name: str, domain: str, match_pattern: str,
                     lift_type: str = "MAIN", notes: str = "") -> None:
    """
    Add a new lift to the Tracked Lifts tab.
    Creates the tab (with seed data) if it doesn't exist yet.
    lift_type: MAIN | AUXILIARY | ACCESSORY
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_TRACKED_LIFTS)
    except gspread.WorksheetNotFound:
        ws = _create_tracked_lifts_tab(sheet)

    today = str(date.today())
    ws.append_row([
        name.strip(),
        domain.strip().upper(),
        match_pattern.strip(),
        lift_type.strip().upper(),
        "Y",
        today,
        notes,
    ])


def _create_tracked_lifts_tab(sheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Create and seed the Tracked Lifts tab from KEY_LIFTS."""
    from config import KEY_LIFTS
    ws = sheet.add_worksheet(title=TAB_TRACKED_LIFTS, rows=200, cols=8)
    ws.append_row(TRACKED_LIFTS_HEADERS)
    ws.append_row(["# Lift registry. Type: MAIN|AUXILIARY|ACCESSORY. Active: Y|N.", "", "", "", "", "", ""])
    today = str(date.today())
    seed_rows = [
        [lift_name, domain, lift_name, "MAIN", "Y", today, "seeded from config"]
        for domain, lift_name in KEY_LIFTS
    ]
    ws.append_rows(seed_rows)
    return ws


def read_lift_history_for_exercise(exercise_name: str) -> list[dict]:
    """Read full Lift History for a specific exercise (for per-lift deep dive)."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFT_HISTORY)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if exercise_name.lower() in entry.get("Exercise", "").lower():
            entries.append(entry)
    return entries


def get_session_dates_from_lift_history(week_num: int) -> dict:
    """
    Return {day_label: date_str} for sessions logged in Lift History for the given week.
    Used by prompt.py to cross-reference Done=Yes entries with actual workout dates,
    so the coach knows WHEN sessions happened (not just that they happened).
    """
    rows = read_lift_history(limit=200)
    dates = {}
    for row in rows:
        if str(row.get("Week", "")).strip() != str(week_num):
            continue
        day = row.get("Day", "").strip()
        date_str = row.get("Date", "").strip()
        if day and date_str and day not in dates:
            dates[day] = date_str
    return dates


def get_active_program_sheet_id() -> Optional[str]:
    """
    Return the Sheet ID for the currently active Program sheet from the registry.
    Returns None if no active program is registered.
    """
    info = get_active_program_info()
    return info.get("sheet_id") if info else None


def get_active_program_info() -> Optional[dict]:
    """
    Return metadata for the currently active program from the registry.
    Keys: sheet_id, name, start_date, total_weeks, notes
    Returns None if no active program is registered.
    """
    for entry in read_sheet_registry():
        if entry.get("Type") == "Program" and entry.get("Status", "").lower() == "active":
            return {
                "sheet_id": entry.get("Sheet ID", "").strip() or None,
                "name": entry.get("Name", "").strip(),
                "start_date": entry.get("Start Date", "").strip(),
                "total_weeks": entry.get("Total Weeks", "").strip(),
                "notes": entry.get("Notes", "").strip(),
            }
    return None


def transition_program(old_sheet_id: str, new_sheet_id: str, new_name: str,
                        new_start_date: str, total_weeks: str = "",
                        notes: str = "") -> None:
    """
    Archive the current active program and register a new one as active.
    Called by the coach when transitioning between programs.
    """
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_SHEET_REGISTRY)
    rows = ws.get_all_values()

    # Mark old program as Archive
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > 1 and row[1].strip() == old_sheet_id:
            status_col = SHEET_REGISTRY_HEADERS.index("Status") + 1
            ws.update_cell(i, status_col, "Archive")
            break

    # Register new program
    register_sheet(new_name, new_sheet_id, "Program", status="active",
                   start_date=new_start_date, total_weeks=total_weeks, notes=notes)


def activate_pending_program() -> Optional[str]:
    """
    Transition flow when athlete confirms a newly created program:
    - Sets all ACTIVE programs → COMPLETED
    - Sets the most recent PENDING program → ACTIVE
    Returns the name of the activated program, or None if no PENDING program found.
    """
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_SHEET_REGISTRY)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return None

    headers = rows[0]
    status_col = headers.index("Status") + 1  # 1-indexed

    pending_row = None
    pending_name = None
    for i, row in enumerate(rows[1:], start=2):
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        status = entry.get("Status", "").upper().strip()
        if status == "PENDING" and pending_row is None:
            pending_row = i
            pending_name = entry.get("Name", "New Program")
        elif status == "ACTIVE":
            ws.update_cell(i, status_col, "COMPLETED")

    if pending_row:
        ws.update_cell(pending_row, status_col, "ACTIVE")
        return pending_name
    return None


def get_last_run_date() -> Optional[date]:
    """Return the date of the most recent coach run, from the Coach Log."""
    entries = read_coach_log(limit=1)
    if not entries:
        return None
    date_str = entries[-1].get("Date", "")
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def read_all() -> dict:
    """Read all Coach Memory data at once."""
    return {
        "athlete_profile": read_athlete_profile(),
        "long_term_goals": read_long_term_goals(),
        "lift_history": read_lift_history(),
        "health_log": read_health_log(),
        "life_context": read_life_context(),
        "program_history": read_program_history(),
        "coach_log": read_coach_log(),
        "sheet_registry": read_sheet_registry(),
        "commands": read_commands(),
        "strategic_plan": read_strategic_plan(),
        "planning_notes": read_planning_notes(),
        "telegram_log": read_telegram_log(),
        "coach_focus": read_coach_focus(),
        "coach_state": read_coach_state(),
        "athlete_preferences": read_athlete_preferences(),
        "tracked_lifts": read_tracked_lifts(),
        "commitments": read_commitments(),
    }


# ---------------------------------------------------------------------------
# Write functions
# ---------------------------------------------------------------------------

def compute_epley(weight_str: str, sets_reps_str: str) -> Optional[float]:
    """
    Estimate 1RM using the Epley formula: 1RM = weight * (1 + reps/30).
    Parses weight from strings like "92.5kg", "92.5", and reps from "4x4", "3x5".
    Returns None if parsing fails.
    """
    import re as _re
    if not weight_str or not sets_reps_str:
        return None
    try:
        weight_match = _re.search(r"(\d+(?:[.,]\d+)?)", str(weight_str))
        if not weight_match:
            return None
        weight = float(weight_match.group(1).replace(",", "."))

        # "4x4" -> reps=4, "3x5" -> reps=5 (last number = reps per set)
        reps_match = _re.search(r"\d+[xX](\d+)", str(sets_reps_str))
        if not reps_match:
            # Maybe just a number like "5" meaning 5 reps
            reps_match = _re.search(r"(\d+)", str(sets_reps_str))
        if not reps_match:
            return None
        reps = int(reps_match.group(1))

        if reps == 0 or weight == 0:
            return None
        est_1rm = weight * (1 + reps / 30)
        return round(est_1rm, 1)
    except (ValueError, TypeError):
        return None


def _lift_history_key(row_values: list) -> tuple:
    """
    Canonical dedup key for a Lift History row.
    Uses (week, day_label, exercise) — stable even when date column is empty.
    Normalised to lowercase + stripped to handle minor formatting differences.
    """
    week = str(row_values[1] if len(row_values) > 1 else "").strip().lower()
    day  = str(row_values[2] if len(row_values) > 2 else "").strip().lower()
    exer = str(row_values[3] if len(row_values) > 3 else "").strip().lower()
    return (week, day, exer)


def _session_to_row(s: dict) -> list:
    """Convert a session dict to a Lift History row (list of cell values)."""
    weight_for_1rm = s.get("actual") or s.get("prescribed_weight", "")
    est_1rm = s.get("est_1rm") or compute_epley(weight_for_1rm, s.get("sets_reps", ""))
    est_1rm_str = str(est_1rm) if est_1rm is not None else ""
    return [
        str(s.get("date", date.today())),
        str(s.get("week", "")),
        str(s.get("day_label", "")),
        str(s.get("exercise_name", "")),
        str(s.get("prescribed_weight", "")),
        str(s.get("actual", "")),
        "Y" if s.get("completed") else ("N" if s.get("completed") is False else "?"),
        str(s.get("notes", "")),
        est_1rm_str,
    ]


def upsert_lift_history(sessions: list[dict]) -> tuple[int, int]:
    """
    Upsert sessions into Lift History.

    Match key: (week, day_label, exercise_name) — robust even when date is missing.
    - If a matching row exists and actual/notes/est_1rm have changed → update it.
    - If no match → append as new row.

    Returns (inserted, updated) counts.
    """
    if not sessions:
        return 0, 0

    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFT_HISTORY)
    all_rows = ws.get_all_values()

    # Build key → (row_index_1based, current_actual, current_notes, current_1rm) map
    # Skip header row (index 0)
    existing: dict[tuple, tuple] = {}
    for i, row in enumerate(all_rows[1:], start=2):  # gspread row 2 = first data row
        key = _lift_history_key(row)
        if key[2]:  # only index if exercise is non-empty
            existing[key] = (i, row)

    inserted = 0
    updated = 0
    rows_to_append = []

    for s in sessions:
        new_row = _session_to_row(s)
        key = (
            str(s.get("week", "")).strip().lower(),
            str(s.get("day_label", "")).strip().lower(),
            str(s.get("exercise_name", "")).strip().lower(),
        )

        if key[2] and key in existing:
            # Row exists — check if updatable fields changed
            row_idx, old_row = existing[key]
            old_actual  = str(old_row[5] if len(old_row) > 5 else "").strip()
            old_notes   = str(old_row[7] if len(old_row) > 7 else "").strip()
            old_1rm     = str(old_row[8] if len(old_row) > 8 else "").strip()
            new_actual  = new_row[5].strip()
            new_notes   = new_row[7].strip()
            new_1rm     = new_row[8].strip()

            changed = (
                (new_actual  and new_actual  != old_actual)  or
                (new_notes   and new_notes   != old_notes)   or
                (new_1rm     and new_1rm     != old_1rm)
            )
            if changed:
                # Update only the mutable columns: Actual(6), Notes(8), Est1RM(9) — 1-indexed
                if new_actual and new_actual != old_actual:
                    ws.update_cell(row_idx, 6, new_actual)
                if new_notes and new_notes != old_notes:
                    ws.update_cell(row_idx, 8, new_notes)
                if new_1rm and new_1rm != old_1rm:
                    ws.update_cell(row_idx, 9, new_1rm)
                updated += 1
        else:
            rows_to_append.append(new_row)

    if rows_to_append:
        ws.append_rows(rows_to_append)
        inserted += len(rows_to_append)

    return inserted, updated


def append_lift_history(sessions: list[dict]) -> None:
    """
    Append new session data to Lift History.
    Each session dict: {week, day_label, exercise_name, prescribed_weight,
                        actual, completed, notes, date, est_1rm (optional)}
    Est 1RM is computed automatically if not provided.
    NOTE: prefer upsert_lift_history() for new code — this is kept for compatibility.
    """
    if not sessions:
        return
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFT_HISTORY)

    rows = [_session_to_row(s) for s in sessions]
    ws.append_rows(rows)


def append_health_log(entries: list[dict]) -> None:
    """
    Append new health log entries (from Daily Log tab).
    Each entry: {date, bodyweight, steps, sleep, food_quality, sun, notes}
    """
    if not entries:
        return
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_HEALTH_LOG)

    rows = []
    for e in entries:
        sun = e.get("sun")
        sun_str = "Y" if sun is True else ("N" if sun is False else "")
        rows.append([
            str(e.get("date", "")),
            str(e.get("bodyweight", "") or ""),
            str(e.get("steps", "") or ""),
            str(e.get("sleep", "") or ""),
            str(e.get("food_quality", "") or ""),
            sun_str,
            str(e.get("notes", "") or ""),
        ])

    ws.append_rows(rows)


def upsert_health_log_row(date_str: str, updates: dict) -> str:
    """
    Upsert a Health Log row by date. Reads live headers dynamically so that
    new columns (e.g. "HRV (ms)", "Body Battery") are handled transparently.

    - If the date already exists: updates only columns where updates[key] is not None.
    - If the date is new: appends a row with all provided values.
    - If a column in `updates` doesn't exist in the header row: appends the column header first.

    updates keys should match Health Log column headers exactly, e.g.:
      "Bodyweight (kg)", "Steps", "Sleep (hrs)", "HRV (ms)", "Body Battery"

    Returns: "inserted" | "updated" | "skipped"
    """
    if not updates:
        return "skipped"

    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_HEALTH_LOG)
    all_rows = ws.get_all_values()

    if not all_rows:
        # Sheet is empty — write header then insert
        headers = list(HEALTH_LOG_HEADERS)
        for key in updates:
            if key not in headers:
                headers.append(key)
        ws.append_row(headers)
        row = [date_str] + [""] * (len(headers) - 1)
        for key, val in updates.items():
            if val is not None and key in headers:
                row[headers.index(key)] = str(val)
        ws.append_row(row)
        return "inserted"

    headers = all_rows[0]

    # Extend header row if new columns needed
    for key in updates:
        if key not in headers and key != "Date":
            headers.append(key)
            ws.update_cell(1, len(headers), key)

    # Build col index map: column name -> 0-based index
    col_idx = {h: i for i, h in enumerate(headers)}

    # Find existing row for this date
    existing_row_num = None  # 1-based gspread row
    for i, row in enumerate(all_rows[1:], start=2):
        row_date = str(row[0]).strip()[:10] if row else ""
        if row_date == str(date_str)[:10]:
            existing_row_num = i
            break

    if existing_row_num is not None:
        # Update only the columns provided (non-None values)
        changed = False
        existing_row = all_rows[existing_row_num - 1]
        for key, val in updates.items():
            if val is None or key == "Date":
                continue
            col = col_idx.get(key)
            if col is None:
                continue
            # Only overwrite if the cell is currently empty
            current = str(existing_row[col]).strip() if col < len(existing_row) else ""
            if not current:
                ws.update_cell(existing_row_num, col + 1, str(val))  # gspread 1-indexed
                changed = True
        return "updated" if changed else "skipped"
    else:
        # New date — build and append a full row
        row = [""] * len(headers)
        row[0] = str(date_str)
        for key, val in updates.items():
            if val is not None and key in col_idx:
                row[col_idx[key]] = str(val)
        ws.append_row(row)
        return "inserted"


def register_sheet(name: str, sheet_id: str, sheet_type: str,
                   status: str = "active", start_date: str = "",
                   total_weeks: str = "", notes: str = "") -> None:
    """
    Register a sheet in the Active Sheets registry.
    sheet_type: "Program" | "Auxiliary" | "Archive"
    start_date: YYYY-MM-DD when the program started (for week computation)
    total_weeks: how many weeks the program runs (e.g. "30")
    """
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_SHEET_REGISTRY)
    ws.append_row([name, sheet_id, sheet_type, status,
                   str(date.today()), start_date, str(total_weeks), notes])


def create_and_register_sheet(name: str, sheet_type: str,
                               tabs: list[dict] = None, notes: str = "") -> str:
    """
    Create a new Google Sheet, set up tabs with headers, register it in the registry.
    tabs: list of {"title": str, "headers": list[str]} — if None, creates one blank tab.
    Returns the new sheet ID.
    """
    client = get_client()
    new_sheet = client.create(name)
    sheet_id = new_sheet.id

    if tabs:
        # Rename the default Sheet1 to first tab, then add the rest
        ws_default = new_sheet.get_worksheet(0)
        ws_default.update_title(tabs[0]["title"])
        if tabs[0].get("headers"):
            ws_default.append_row(tabs[0]["headers"])

        for tab in tabs[1:]:
            ws = new_sheet.add_worksheet(title=tab["title"], rows=1000, cols=20)
            if tab.get("headers"):
                ws.append_row(tab["headers"])

    register_sheet(name, sheet_id, sheet_type, status="active", notes=notes)
    return sheet_id


def append_life_context(context_note: str, context_date: Optional[date] = None) -> None:
    """Append a life context change detected from notes. Skips exact duplicates."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_LIFE_CONTEXT)
    d = str(context_date or date.today())
    # Dedup: skip if same date+note already exists
    existing = ws.get_all_values()
    for row in existing[1:]:
        if len(row) >= 2 and row[0] == d and row[1].strip() == context_note.strip():
            return
    ws.append_row([d, context_note])


def log_coach_run(observations: str, email_summary: str,
                  run_date: Optional[date] = None, cost_usd: float = 0.0) -> None:
    """Log what the agent observed and sent today, including API cost."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_COACH_LOG)
    d = str(run_date or date.today())
    obs = observations
    if cost_usd:
        obs = f"{observations} | cost: ${cost_usd:.4f}"
    ws.append_row([d, obs, email_summary])


def update_program_history(program_name: str, start_date: str,
                            end_date: str = "", weeks_completed: int = 0,
                            notes: str = "") -> None:
    """Add or update a program history entry."""
    sheet = _get_memory_sheet()
    ws = _get_tab(sheet, TAB_PROGRAM_HISTORY)
    ws.append_row([program_name, start_date, end_date, str(weeks_completed), notes])


# ---------------------------------------------------------------------------
# Sync: detect new data and append to history
# ---------------------------------------------------------------------------

def sync_sessions_to_history(program_data: dict) -> list[dict]:
    """
    Upsert program sessions into Lift History.

    For each Done session: insert if new, update if actual/notes/est_1rm changed.
    Match key is (week, day_label, exercise) — robust even when session dates are missing.
    Returns list of sessions that were inserted or updated.
    """
    current_week = program_data.get("current_week")
    if not current_week:
        return []

    week_num = current_week.get("week_num", "?")
    sessions = []

    for day in current_week.get("days", []):
        day_label = day.get("label", "")
        session_date = day.get("date")

        for ex in day.get("exercises", []):
            if ex.get("done") is not True:
                continue
            sessions.append({
                "date": session_date or date.today(),
                "week": week_num,
                "day_label": day_label,
                "exercise_name": ex["name"],
                "prescribed_weight": ex.get("weight", ""),
                "sets_reps": ex.get("sets_reps", ""),
                "actual": ex.get("actual", ""),
                "completed": True,
                "notes": ex.get("session_note") or ex.get("notes", ""),
            })

    if sessions:
        inserted, updated = upsert_lift_history(sessions)
        if inserted or updated:
            print(f"    [LiftHistory] {inserted} inserted, {updated} updated")

    return sessions


def sync_health_log(program_data: dict) -> list[dict]:
    """
    Sync new Daily Log entries to Health Log in Coach Memory.
    Returns list of newly synced entries.
    """
    existing = read_health_log(limit=500)
    existing_dates = {row.get("Date", "") for row in existing}

    new_entries = []
    for entry in program_data.get("daily_log", []):
        date_str = str(entry.get("date", ""))
        if date_str in existing_dates:
            continue
        new_entries.append(entry)

    if new_entries:
        append_health_log(new_entries)

    return new_entries


# ---------------------------------------------------------------------------
# Commitments — explicit coach promises tracked to completion
# ---------------------------------------------------------------------------

def read_commitments(status_filter: str = "OPEN") -> list[dict]:
    """
    Read Commitments. status_filter=None returns all, 'OPEN' returns pending items.
    Returns list of dicts with keys: Date Added, Commitment, Due Date, Status, Resolved Date, Notes.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COMMITMENTS)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        if not any(row):
            continue
        entry = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if entry.get("Commitment", "").startswith("#"):
            continue
        if status_filter is None or entry.get("Status", "OPEN").upper() == status_filter.upper():
            entries.append(entry)
    return entries


def append_commitment(commitment: str, due_date: str = "", notes: str = "") -> None:
    """
    Log a new coach commitment (an explicit promise to follow up).
    commitment: what the coach promised, e.g. "Check if Nacho's elbow recovered by next week"
    due_date: YYYY-MM-DD when this should be followed up, blank = open-ended
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COMMITMENTS)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_COMMITMENTS, rows=500, cols=len(COMMITMENTS_HEADERS) + 2)
        ws.append_row(COMMITMENTS_HEADERS)
    today = str(date.today())
    ws.append_row([today, commitment, due_date, "OPEN", "", notes])


def resolve_commitment(commitment_substring: str, resolved_notes: str = "") -> bool:
    """
    Mark a commitment as RESOLVED by substring match on the Commitment text.
    Returns True if found and resolved.
    """
    sheet = _get_memory_sheet()
    try:
        ws = sheet.worksheet(TAB_COMMITMENTS)
    except gspread.WorksheetNotFound:
        return False
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return False
    headers = rows[0]
    commit_col = headers.index("Commitment") + 1 if "Commitment" in headers else 2
    status_col = headers.index("Status") + 1 if "Status" in headers else 4
    resolved_col = headers.index("Resolved Date") + 1 if "Resolved Date" in headers else 5
    notes_col = headers.index("Notes") + 1 if "Notes" in headers else 6

    needle = commitment_substring.lower()
    today = str(date.today())
    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= commit_col and needle in row[commit_col - 1].lower():
            current_status = row[status_col - 1].upper().strip() if len(row) >= status_col else ""
            if current_status != "RESOLVED":
                ws.update_cell(i, status_col, "RESOLVED")
                ws.update_cell(i, resolved_col, today)
                if resolved_notes:
                    ws.update_cell(i, notes_col, resolved_notes)
                return True
    return False


# ---------------------------------------------------------------------------
# Setup: create the Coach Memory Sheet structure
# ---------------------------------------------------------------------------

def setup_memory_sheet() -> None:
    """
    Create all required tabs in the Coach Memory Sheet with headers.
    Safe to run multiple times — skips tabs that already exist.
    """
    sheet = _get_memory_sheet()
    existing = {ws.title for ws in sheet.worksheets()}

    def ensure_tab(name: str, headers: list[str], template_rows: list[list] = None):
        if name in existing:
            print(f"  Tab '{name}' already exists, skipping.")
            return
        ws = sheet.add_worksheet(title=name, rows=1000, cols=10)
        if headers:
            ws.append_row(headers)
        if template_rows:
            ws.append_rows(template_rows)
        print(f"  Created tab '{name}'.")

    print("Setting up Coach Memory Sheet tabs...")

    ensure_tab(TAB_PROFILE, [], [
        ["Name", "Nacho"],
        ["Age", ""],
        ["Training Since", ""],
        ["Health Conditions", "Insulin resistance (carb timing matters). Golfer's elbow (watch pull volume)."],
        ["Background", "Finance professional. Works 14-16h/day. Travels Mon-Thu every 2 weeks. Based in Spain."],
        ["Coaching Preferences", "Direct and honest. Data over motivation. No pandering. Answers questions directly."],
        ["Current Program", "30-Week Strength Program (started 2026-01-13)"],
    ])

    ensure_tab(TAB_GOALS, [], [
        ["Goal", "Notes", "Added Date"],
        ["Reach 120kg squat x5 by Week 30", "Current program target", "2026-01-13"],
        ["Reach 105kg bench x5 by Week 30", "Current program target", "2026-01-13"],
        ["Eventually incorporate Olympic weightlifting", "Long-term, not current focus", "2026-01-13"],
        ["Improve cardio base", "Lost fitness from sedentary work periods", "2026-01-13"],
    ])

    ensure_tab(TAB_LIFT_HISTORY, LIFT_HISTORY_HEADERS)
    ensure_tab(TAB_HEALTH_LOG, HEALTH_LOG_HEADERS)
    ensure_tab(TAB_LIFE_CONTEXT, LIFE_CONTEXT_HEADERS, [
        ["2026-01-13", "Started 30-week strength program. Week 7 current as of 2026-03-05."],
    ])
    ensure_tab(TAB_PROGRAM_HISTORY, PROGRAM_HISTORY_HEADERS, [
        ["30-Week Strength Program", "2026-01-13", "", "7", "In progress as of 2026-03-05"],
    ])
    ensure_tab(TAB_COACH_LOG, COACH_LOG_HEADERS)
    ensure_tab(TAB_SHEET_REGISTRY, SHEET_REGISTRY_HEADERS)
    ensure_tab(TAB_COMMANDS, COMMANDS_HEADERS, [
        ["# How to use: write a command row, set Applied=N. Agent reads on each run.", "", "", ""],
        ["# SKIP_UNTIL: skip emails until this date (inclusive). Applied stays N while active.", "", "", ""],
        ["# EMAIL_HOUR_OVERRIDE: note a desired email hour (informational; update cron manually).", "", "", ""],
        ["# Example row (delete the # to activate):", "", "", ""],
        ["# SKIP_UNTIL", "2026-03-15", "", "N"],
    ])

    ensure_tab(TAB_STRATEGIC_PLAN, STRATEGIC_PLAN_HEADERS, [
        ["# Coach fills this in via --think. Each row = one training phase/block.", "", "", "", "", "", ""],
        ["# Example (delete # to activate):", "", "", "", "", "", ""],
        ["# Strength Peak", "2026-01-13", "2026-04-30", "Max squat/bench", "Squat 120kg, Bench 105kg", "Current block", ""],
    ])

    ensure_tab(TAB_PLANNING_NOTES, PLANNING_NOTES_HEADERS)
    ensure_tab(TAB_TELEGRAM_LOG, TELEGRAM_LOG_HEADERS)
    ensure_tab(TAB_COACH_FOCUS, COACH_FOCUS_HEADERS, [
        ["# Coach writes to this automatically. Category: TRACKING|FOLLOWUP|LANDMARK|CONCERN. Status: OPEN|RESOLVED|STALE. Priority: NORMAL|HIGH|PINNED", "", "", "", "", ""],
    ])
    ensure_tab(TAB_COACH_STATE, COACH_STATE_HEADERS, [
        ["# Coach writes domain summaries here each run. One row per domain — upserted automatically.", "", "", ""],
        ["# Domains: SQUAT | BENCH | DEADLIFT | OHP | HEALTH | SCHEDULE | LIFESTYLE | GOALS | PROGRAM", "", "", ""],
    ])
    ensure_tab(TAB_ATHLETE_PREFS, ATHLETE_PREFS_HEADERS, [
        ["# Explicit preferences from athlete. Category: OUTPUT | TOPICS | STYLE | SCHEDULE", "", "", ""],
        ["# Examples: OUTPUT | no weekly charts | Telegram | 2026-03-07", "", "", ""],
    ])

    ensure_tab(TAB_COMMITMENTS, COMMITMENTS_HEADERS, [
        ["# Explicit coach promises to follow up. Status: OPEN|RESOLVED|DEFERRED.", "", "", "", "", ""],
        ["# Added automatically when coach says 'I'll check X' or 'I'll follow up on Y'.", "", "", "", "", ""],
    ])

    # Tracked Lifts: seed with current KEY_LIFTS if tab doesn't exist yet
    from config import KEY_LIFTS
    if TAB_TRACKED_LIFTS not in existing:
        ws = sheet.add_worksheet(title=TAB_TRACKED_LIFTS, rows=200, cols=8)
        ws.append_row(TRACKED_LIFTS_HEADERS)
        ws.append_row(["# Lift registry. Type: MAIN|AUXILIARY|ACCESSORY. Active: Y|N.", "", "", "", "", "", ""])
        today = str(date.today())
        seed_rows = [
            [lift_name, domain, lift_name, "MAIN", "Y", today, "seeded from config"]
            for domain, lift_name in KEY_LIFTS
        ]
        ws.append_rows(seed_rows)
        print(f"  Created tab '{TAB_TRACKED_LIFTS}' with {len(seed_rows)} seed lifts.")
    else:
        print(f"  Tab '{TAB_TRACKED_LIFTS}' already exists, skipping.")

    print("Done. Review and edit the Athlete Profile and Long-Term Goals tabs directly in Google Sheets.")
    print("Remember to register your current program sheet: python src/memory.py --register-program")


# ---------------------------------------------------------------------------
# V17 Cascade list-domain helpers (WEEKLY_SUMMARIES, MONTHLY_SUMMARIES, etc.)
# ---------------------------------------------------------------------------

def read_summary_list(domain: str, limit: int = 8) -> list[dict]:
    """
    Read a list-valued Coach State domain (JSON array stored in Summary column).
    Used for WEEKLY_SUMMARIES, MONTHLY_SUMMARIES, ANNUAL_SUMMARY, LONGTERM_PLAN.
    Returns list of dicts, most recent last.
    """
    import json as _json
    try:
        cs = read_coach_state()
        raw = cs.get(domain.upper(), {}).get("summary", "")
        if not raw:
            return []
        data = _json.loads(raw)
        if isinstance(data, list):
            return data[-limit:]
        if isinstance(data, dict):
            return [data]
        return []
    except Exception:
        return []


def append_summary(domain: str, summary: dict, max_keep: int = 8) -> None:
    """
    Append a typed JSON summary to a list-valued Coach State domain.
    Trims to max_keep most recent entries.
    Used by close_day(), weekly_eval(), monthly_eval(), etc.
    """
    import json as _json
    existing = read_summary_list(domain, limit=max_keep)
    existing.append(summary)
    if len(existing) > max_keep:
        existing = existing[-max_keep:]
    upsert_coach_state(domain.upper(), _json.dumps(existing, ensure_ascii=False), "HIGH")


def write_single_summary(domain: str, summary: dict) -> None:
    """
    Write a single JSON object to a Coach State domain (for ANNUAL_SUMMARY, LONGTERM_PLAN).
    Overwrites any existing value.
    """
    import json as _json
    upsert_coach_state(domain.upper(), _json.dumps(summary, ensure_ascii=False), "HIGH")


def read_single_summary(domain: str) -> Optional[dict]:
    """
    Read a single JSON object from a Coach State domain.
    Returns None if empty or unparseable.
    """
    import json as _json
    try:
        cs = read_coach_state()
        raw = cs.get(domain.upper(), {}).get("summary", "")
        if not raw:
            return None
        data = _json.loads(raw)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data:
            return data[-1]
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    if "--setup" in sys.argv:
        setup_memory_sheet()
    elif "--register-program" in sys.argv:
        # Usage: python src/memory.py --register-program "Program Name" SHEET_ID
        args = sys.argv[sys.argv.index("--register-program") + 1:]
        prog_name = args[0] if len(args) > 0 else "30-Week Strength Program"
        prog_id = args[1] if len(args) > 1 else ""
        if not prog_id:
            from config import PROGRAM_SHEET_ID
            prog_id = PROGRAM_SHEET_ID
        register_sheet(prog_name, prog_id, "Program", status="active",
                       notes="Registered via --register-program")
        print(f"Registered '{prog_name}' (ID: {prog_id}) as active Program sheet.")
    else:
        print("Reading Coach Memory...")
        data = read_all()
        print(f"\nAthlete Profile:\n{data['athlete_profile']}")
        print(f"\nLong-Term Goals:\n{data['long_term_goals']}")
        print(f"\nLift History: {len(data['lift_history'])} entries")
        print(f"Health Log: {len(data['health_log'])} entries")
        print(f"Life Context: {len(data['life_context'])} entries")
        print(f"Coach Log: {len(data['coach_log'])} entries")
        print(f"Sheet Registry: {len(data['sheet_registry'])} entries")
