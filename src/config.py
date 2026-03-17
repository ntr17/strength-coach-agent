import base64
import math
import os
import re
from datetime import date, datetime
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")


def _extract_sheet_id(value: str) -> str:
    """Accept either a bare Sheet ID or a full Google Sheets URL."""
    if not value:
        return value
    # Extract ID from URL like: .../spreadsheets/d/SHEET_ID/edit...
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", value)
    return match.group(1) if match else value.strip()


def compute_current_week(start_date_str: str, today: date = None) -> int:
    """
    Compute the current training week number from the program start date.
    Week 1 = days 1-7, Week 2 = days 8-14, etc.
    Returns at minimum 1, no upper bound (program may exceed original length).
    """
    if today is None:
        today = date.today()
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 1
    days_elapsed = (today - start).days
    return max(1, math.ceil((days_elapsed + 1) / 7))


ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# PROGRAM_SHEET_ID is optional — agent falls back to Coach Memory registry if absent.
PROGRAM_SHEET_ID = _extract_sheet_id(os.environ.get("PROGRAM_SHEET_ID", ""))
MEMORY_SHEET_ID = _extract_sheet_id(os.environ["MEMORY_SHEET_ID"])
GMAIL_FROM = os.environ["GMAIL_FROM"]
GMAIL_TO = os.environ["GMAIL_TO"]
ATHLETE_NAME = os.environ.get("ATHLETE_NAME", "Nacho")
PROGRAM_START_DATE = os.environ.get("PROGRAM_START_DATE", "")  # optional override
PROGRAM_TOTAL_WEEKS = int(os.environ.get("PROGRAM_TOTAL_WEEKS", "0"))  # 0 = read from registry


def resolve_program_start_date(fallback: str = "2026-01-13") -> str:
    """
    Resolve PROGRAM_START_DATE: env var override → Active Sheets registry → fallback.
    Reads lazily so the registry import only happens when needed.
    """
    if PROGRAM_START_DATE:
        return PROGRAM_START_DATE
    try:
        # Import here to avoid circular imports at module load time
        _sys.path.insert(0, str(Path(__file__).parent))
        from memory import get_active_program_info
        info = get_active_program_info()
        if info and info.get("start_date"):
            return info["start_date"]
    except Exception:
        pass
    return fallback


import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))

# CURRENT_WEEK env var is an optional manual override; normally computed from date.
# Uses resolve_program_start_date() so it reads from the Active Sheets registry first,
# falling back to PROGRAM_START_DATE env var or the hardcoded default — not a bare fallback.
_CURRENT_WEEK_OVERRIDE = os.environ.get("CURRENT_WEEK", "")
CURRENT_WEEK = int(_CURRENT_WEEK_OVERRIDE) if _CURRENT_WEEK_OVERRIDE else compute_current_week(
    resolve_program_start_date()
)
EMAIL_HOUR = int(os.environ.get("EMAIL_HOUR", "22"))  # 10 PM default

# Telegram (optional — bot won't run if these are absent)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

def bootstrap_google_credentials() -> None:
    """
    On Railway (or any environment without local credential files), decode
    GOOGLE_CREDENTIALS_B64 and GOOGLE_TOKEN_B64 env vars and write them to
    the expected file paths. Safe to call multiple times — skips if files exist.
    """
    CONFIG_DIR.mkdir(exist_ok=True)

    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
    if creds_b64 and not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_bytes(base64.b64decode(creds_b64))

    token_b64 = os.environ.get("GOOGLE_TOKEN_B64", "")
    if token_b64 and not TOKEN_FILE.exists():
        TOKEN_FILE.write_bytes(base64.b64decode(token_b64))


# Google API scopes needed
# NOTE: gmail.readonly is NOT listed here so existing tokens continue to work.
# To enable email reply reading, re-auth locally after deleting config/token.json,
# then add gmail.readonly back here and to GOOGLE_SCOPES_FULL below.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

# Full scopes including reply reading — used only when generating a fresh token
GOOGLE_SCOPES_FULL = GOOGLE_SCOPES + [
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Claude model IDs — update here when new models ship, nowhere else
CLAUDE_MODEL = "claude-sonnet-4-6"          # main coaching passes (full emails, planning)
CLAUDE_HAIKU = "claude-haiku-4-5-20251001"  # lightweight passes (brief, proactive, processor)
CLAUDE_OPUS  = "claude-opus-4-6"            # deep thinking (program design, extended thinking)

# ---------------------------------------------------------------------------
# Canonical lift list
# ---------------------------------------------------------------------------

# Each tuple: (domain_name, lift_name_substring)
# domain_name   — used as the Coach State domain key (uppercase, no spaces)
# lift_name     — matched as substring against the "Exercise" column in Lift History
#                 (case-insensitive). Keep broad enough to catch variants:
#                 "Row" → "Barbell Row", "Pendlay Row", "DB Row"
#                 "Pull-up" → "Pull-up", "Pull up", "Chin-up", "Chinup"
KEY_LIFTS: list[tuple[str, str]] = [
    ("SQUAT",    "Squat"),
    ("BENCH",    "Bench Press"),
    ("DEADLIFT", "Deadlift"),
    ("OHP",      "OHP"),
    ("ROW",      "Row"),
    ("PULLUP",   "Pull-up"),
    ("DIP",      "Dip"),
]
