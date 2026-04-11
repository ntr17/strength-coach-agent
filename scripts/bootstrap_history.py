"""
bootstrap_history.py — One-time import of historical training data from the
program sheet (Week 1 through current week) into coach.db.

Uses local OAuth credentials (config/token.json), not service account.

Usage:
    python scripts/bootstrap_history.py                # import weeks 1-11
    python scripts/bootstrap_history.py --weeks 1-7   # specific range
    python scripts/bootstrap_history.py --dry-run      # print what would import
"""

import argparse
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

PROGRAM_SHEET_ID = "1UWTANkiCxWTl0gD1SEZA2dgn4-JukVoUZmxvoDQ0Wo8"
PROGRAM_START    = date(2026, 1, 13)   # Monday, Week 1 Day 1
DB_PATH          = Path(__file__).parent.parent / "data" / "coach.db"

# Approximate day offsets within a week (Mon=0 Tue=1 Thu=3 Fri=4)
DAY_OFFSETS = {1: 0, 2: 1, 3: 3, 4: 4}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_sheets_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token = Path(__file__).parent.parent / "config" / "token.json"
    creds = Credentials.from_authorized_user_file(str(token))
    return build("sheets", "v4", credentials=creds)


def read_week_tab(svc, week_num: int) -> list:
    result = svc.spreadsheets().values().get(
        spreadsheetId=PROGRAM_SHEET_ID,
        range=f"Week {week_num}",
    ).execute()
    return result.get("values", [])


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _is_done(cell: str) -> bool:
    """'Yes', ' Yes', '☑ Yes', '☐ Yes' → True. 'No', '☐' → False."""
    c = cell.strip().lower()
    return "yes" in c


def _parse_sets_reps(s: str):
    """
    '3x5'         → (3, 5, False)
    '2x6/leg'     → (2, 6, False)
    '1xAMRAP'     → (1, None, True)
    '2-3x5 ...'   → (2, 5, False)   ← take lower bound
    '4 min'       → None            ← skip density sets
    '1x failure'  → (1, None, True)
    """
    s = s.strip()
    if not s or "min" in s.lower():
        return None

    amrap = "amrap" in s.lower() or "failure" in s.lower()

    # Match NxM (or N-Mx...)
    m = re.match(r"(\d+)(?:-\d+)?x(\d+)?", s, re.IGNORECASE)
    if not m:
        return None

    sets = int(m.group(1))
    reps = int(m.group(2)) if m.group(2) else None

    return (sets, reps, amrap)


def _parse_weight(weight_str: str) -> float | None:
    """
    '85.0kg'      → 85.0
    '30.0kg'      → 30.0
    'BW'          → 0.0
    '+10.0kg'     → None   (relative — skip)
    'EZ+15.0kg'   → None   (relative — skip)
    ''            → None
    """
    s = weight_str.strip()
    if not s:
        return None
    if s.upper() == "BW":
        return 0.0
    if s.startswith("+") or ("+" in s and not s.startswith("-")):
        return None  # relative to bodyweight, can't resolve without BW
    m = re.search(r"([\d.]+)\s*kg", s, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _parse_actual(actual_str: str):
    """
    Parse the 'Actual' cell. Returns (weight_kg, reps) overrides.
    '30 reps'   → (None, 30)   — reps override only
    '95kg x 5'  → (95.0, 5)
    '95kg'      → (95.0, None)
    '5'         → (None, 5)    — treat as reps if small number
    ''          → (None, None) — use planned
    """
    s = actual_str.strip() if actual_str else ""
    if not s:
        return None, None

    # "Nkg x M" or "Nkg xM"
    m = re.match(r"([\d.]+)\s*kg\s*[xX×]\s*(\d+)", s)
    if m:
        return float(m.group(1)), int(m.group(2))

    # "Nkg"
    m = re.match(r"([\d.]+)\s*kg", s)
    if m:
        return float(m.group(1)), None

    # "N reps"
    m = re.match(r"(\d+)\s*rep", s, re.IGNORECASE)
    if m:
        return None, int(m.group(1))

    # bare number — treat as reps
    m = re.match(r"^(\d+)$", s)
    if m:
        return None, int(m.group(1))

    return None, None


# ---------------------------------------------------------------------------
# Parse a full week tab
# ---------------------------------------------------------------------------

def parse_week(rows: list, week_num: int, block_num: int) -> list[dict]:
    """
    Parse raw rows from a week tab into a list of set dicts ready for DB insert.
    Returns only rows where Done=Yes.
    """
    sets_out = []
    current_day = None

    for row in rows:
        if not row:
            continue

        first = row[0].strip() if row else ""

        # Day header: "DAY 1: ..."
        day_m = re.match(r"DAY\s+(\d+)", first, re.IGNORECASE)
        if day_m:
            current_day = int(day_m.group(1))
            continue

        # Skip header rows and week-level metadata
        if first in ("Exercise", "WEEKLY NOTES:", "Bodyweight:", "Sleep (avg hrs):",
                     "Energy (1-10):", "Notes:") or re.match(r"WEEK\s+\d+", first):
            continue

        # Exercise row needs at least 4 cells (Exercise, Weight, Sets×Reps, Done)
        if len(row) < 4 or current_day is None:
            continue

        exercise   = row[0].strip()
        weight_str = row[1].strip() if len(row) > 1 else ""
        sets_reps  = row[2].strip() if len(row) > 2 else ""
        done_str   = row[3].strip() if len(row) > 3 else ""
        actual_str = row[4].strip() if len(row) > 4 else ""

        if not exercise:
            continue

        # Skip optional exercises not done (Done = ☐ with no Yes)
        done = _is_done(done_str)
        if not done:
            continue

        parsed = _parse_sets_reps(sets_reps)
        if parsed is None:
            continue  # density sets etc.

        planned_sets, planned_reps, is_amrap = parsed
        planned_weight = _parse_weight(weight_str)

        # Actual overrides
        actual_weight, actual_reps = _parse_actual(actual_str)
        weight_kg = actual_weight if actual_weight is not None else planned_weight
        reps      = actual_reps   if actual_reps   is not None else planned_reps

        if weight_kg is None or reps is None:
            # Can't insert without both weight and reps
            continue

        # Session date
        week_start = PROGRAM_START + timedelta(weeks=week_num - 1)
        day_offset = DAY_OFFSETS.get(current_day, current_day - 1)
        session_date = week_start + timedelta(days=day_offset)

        # Expand sets into individual rows
        for set_num in range(1, planned_sets + 1):
            sets_out.append({
                "session_date": session_date.isoformat(),
                "week_number":  week_num,
                "block_number": block_num,
                "day_number":   current_day,
                "exercise":     exercise,
                "set_number":   set_num,
                "reps":         reps,
                "weight_kg":    weight_kg,
                "is_amrap":     1 if is_amrap else 0,
                "should_count": 1,
                "rpe":          None,
                "notes":        None,
                "source":       "bootstrap",  # approximate dates — excluded from volume/adherence
            })

    return sets_out


def _infer_block(week_num: int) -> int:
    """Infer block number from week (6-week blocks, 5-week working + 1 deload)."""
    return ((week_num - 1) // 5) + 1


# ---------------------------------------------------------------------------
# Insert into DB
# ---------------------------------------------------------------------------

def insert_sets(db_path: Path, sets: list[dict], dry_run: bool = False) -> int:
    if dry_run:
        return len(sets)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            """
            INSERT INTO lift_sets
              (session_date, week_number, block_number, day_number, exercise,
               set_number, reps, weight_kg, is_amrap, should_count, rpe, notes, source)
            VALUES
              (:session_date, :week_number, :block_number, :day_number, :exercise,
               :set_number, :reps, :weight_kg, :is_amrap, :should_count, :rpe, :notes, :source)
            """,
            sets,
        )
        conn.commit()
    finally:
        conn.close()
    return len(sets)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bootstrap historical lift data from program sheet")
    parser.add_argument("--weeks", default="1-11",
                        help="Week range to import, e.g. '1-11' or '5-5' (default: 1-11)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print, don't write to DB")
    parser.add_argument("--db-path", default=str(DB_PATH))
    args = parser.parse_args()

    # Parse week range
    m = re.match(r"(\d+)(?:-(\d+))?$", args.weeks)
    if not m:
        print("Invalid --weeks format. Use e.g. '1-11' or '5'"); sys.exit(1)
    w_start = int(m.group(1))
    w_end   = int(m.group(2)) if m.group(2) else w_start

    db_path = Path(args.db_path)
    if not db_path.exists() and not args.dry_run:
        print(f"DB not found: {db_path}. Run init_db.py first."); sys.exit(1)

    print(f"[bootstrap] Importing weeks {w_start}–{w_end} from program sheet")
    print(f"[bootstrap] DB: {db_path}")
    if args.dry_run:
        print("[bootstrap] DRY RUN — no DB writes")

    svc = _get_sheets_service()

    total_sets = 0
    for week_num in range(w_start, w_end + 1):
        block_num = _infer_block(week_num)
        try:
            rows = read_week_tab(svc, week_num)
        except Exception as e:
            print(f"  Week {week_num}: FAILED to read sheet — {e}")
            continue

        sets = parse_week(rows, week_num, block_num)
        total_sets += len(sets)

        if args.dry_run:
            print(f"  Week {week_num} (Block {block_num}): {len(sets)} sets")
            for s in sets:
                print(f"    {s['session_date']} D{s['day_number']} {s['exercise']:30s} "
                      f"{s['set_number']}/{s['reps']}r @ {s['weight_kg']}kg"
                      f"{'  AMRAP' if s['is_amrap'] else ''}")
        else:
            insert_sets(db_path, sets)
            print(f"  Week {week_num} (Block {block_num}): {len(sets)} sets inserted")

    suffix = "would insert" if args.dry_run else "inserted"
    print(f"\n[bootstrap] Done — {total_sets} sets {suffix}")


if __name__ == "__main__":
    main()
