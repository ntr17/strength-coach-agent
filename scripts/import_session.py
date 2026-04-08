"""
import_session.py — Parse a session .md file and insert sets into coach.db.

Usage:
    python scripts/import_session.py sessions/2024-04-07_day1.md
    python scripts/import_session.py sessions/2024-04-07_day1.md --db-path data/coach.db
    python scripts/import_session.py sessions/2024-04-07_day1.md --force  # skip confirmation on re-import

Idempotent: if the same (session_date, day_number) already exists, asks the
user to confirm before deleting and re-inserting. Use --force to skip that prompt.
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import date as date_type
from typing import Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "coach.db")

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(lines: list[str]) -> dict:
    """Parse the YAML-like frontmatter block between the first two '---' lines."""
    in_block = False
    meta = {}
    for line in lines:
        line = line.rstrip()
        if line == "---":
            if not in_block:
                in_block = True
                continue
            else:
                break
        if in_block and ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in ("yes", "y", "1", "true")


def _parse_optional_float(val: str) -> Optional[float]:
    val = val.strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _parse_sets(lines: list[str], meta: dict) -> list[dict]:
    """
    Parse exercise blocks from the body of the file.
    Returns a list of set dicts ready for DB insertion.
    """
    sets = []
    current_exercise = None
    header_seen = False
    set_count_per_exercise: dict[str, int] = {}

    session_date = meta["date"]
    week = int(meta["week"])
    block = int(meta["block"])
    day_raw = meta["day"]
    try:
        day = int(day_raw)
    except ValueError:
        # 'travel' or similar — store as 0
        day = 0

    for line in lines:
        stripped = line.strip()

        # Exercise heading
        if stripped.startswith("### "):
            current_exercise = stripped[4:].strip()
            header_seen = False
            continue

        if current_exercise is None:
            continue

        # Table header row
        if stripped.startswith("| set") or stripped.startswith("|--"):
            header_seen = True
            continue

        # Table data row
        if header_seen and stripped.startswith("|") and not stripped.startswith("|--"):
            cols = [c.strip() for c in stripped.split("|")[1:-1]]
            if len(cols) < 5:
                continue  # malformed row

            try:
                set_num = int(cols[0])
                reps = int(cols[1])
                weight_kg = float(cols[2])
                is_amrap = _parse_bool(cols[3])
                should_count = _parse_bool(cols[4])
                rpe = _parse_optional_float(cols[5]) if len(cols) > 5 else None
                notes = cols[6].strip() if len(cols) > 6 else None
                if not notes:
                    notes = None
            except (ValueError, IndexError):
                continue

            sets.append({
                "session_date": session_date,
                "week_number": week,
                "block_number": block,
                "day_number": day,
                "exercise": current_exercise,
                "set_number": set_num,
                "reps": reps,
                "weight_kg": weight_kg,
                "is_amrap": is_amrap,
                "should_count": should_count,
                "rpe": rpe,
                "notes": notes,
            })

    return sets


def parse_session_file(path: str) -> tuple[dict, list[dict]]:
    """Parse a session .md file. Returns (meta, sets)."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    meta = _parse_frontmatter(lines)
    _validate_meta(meta, path)
    sets = _parse_sets(lines, meta)
    return meta, sets


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_meta(meta: dict, path: str) -> None:
    required = ("date", "week", "block", "day", "type")
    for field in required:
        if field not in meta:
            print(f"ERROR: Missing frontmatter field '{field}' in {path}")
            sys.exit(1)

    # Date format check
    try:
        date_type.fromisoformat(meta["date"])
    except ValueError:
        print(f"ERROR: Invalid date '{meta['date']}' — expected YYYY-MM-DD")
        sys.exit(1)

    # Week/block must be positive integers
    for field in ("week", "block"):
        try:
            val = int(meta[field])
            if val < 0:
                raise ValueError
        except ValueError:
            print(f"ERROR: '{field}' must be a non-negative integer, got '{meta[field]}'")
            sys.exit(1)

    # Type must be valid
    valid_types = {"normal", "deload", "travel", "test"}
    if meta.get("type", "").lower() not in valid_types:
        print(f"WARNING: Unrecognized type '{meta['type']}' — expected one of {valid_types}")


def _validate_sets(sets: list[dict]) -> None:
    for s in sets:
        # Weight sanity check
        if s["weight_kg"] < 0 or s["weight_kg"] > 500:
            print(f"ERROR: Suspicious weight {s['weight_kg']}kg on {s['exercise']} set {s['set_number']}")
            sys.exit(1)
        # Reps sanity check
        if s["reps"] < 1 or s["reps"] > 60:
            print(f"ERROR: Suspicious reps {s['reps']} on {s['exercise']} set {s['set_number']}")
            sys.exit(1)
        # RPE range check
        if s["rpe"] is not None and not (1 <= s["rpe"] <= 10):
            print(f"ERROR: RPE {s['rpe']} out of range (1-10) on {s['exercise']} set {s['set_number']}")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _get_existing_count(conn: sqlite3.Connection, session_date: str, day_number: int) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM lift_sets WHERE session_date=? AND day_number=?",
        (session_date, day_number)
    )
    return cur.fetchone()[0]


def _delete_session(conn: sqlite3.Connection, session_date: str, day_number: int) -> None:
    conn.execute(
        "DELETE FROM lift_sets WHERE session_date=? AND day_number=?",
        (session_date, day_number)
    )


def _insert_sets(conn: sqlite3.Connection, sets: list[dict]) -> int:
    sql = """
        INSERT INTO lift_sets
          (session_date, week_number, block_number, day_number, exercise,
           set_number, reps, weight_kg, is_amrap, should_count, rpe, notes)
        VALUES
          (:session_date, :week_number, :block_number, :day_number, :exercise,
           :set_number, :reps, :weight_kg, :is_amrap, :should_count, :rpe, :notes)
    """
    conn.executemany(sql, sets)
    return len(sets)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Import a session .md file into coach.db")
    parser.add_argument("session_file", help="Path to the session markdown file")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--force", action="store_true", help="Re-import without confirmation prompt")
    args = parser.parse_args()

    session_path = args.session_file
    if not os.path.isfile(session_path):
        print(f"ERROR: File not found: {session_path}")
        sys.exit(1)

    db_path = os.path.abspath(args.db_path)
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}")
        print("Run: python scripts/init_db.py")
        sys.exit(1)

    print(f"Parsing: {session_path}")
    meta, sets = parse_session_file(session_path)

    if not sets:
        print("WARNING: No sets found in file. Nothing to import.")
        sys.exit(0)

    _validate_sets(sets)

    session_date = meta["date"]
    day_number = int(meta["day"]) if meta["day"].isdigit() else 0

    conn = sqlite3.connect(db_path)
    try:
        existing = _get_existing_count(conn, session_date, day_number)
        if existing > 0:
            if not args.force:
                ans = input(
                    f"\n  {existing} sets already exist for {session_date} day {day_number}.\n"
                    f"  Re-import will delete and replace them. Continue? [y/N] "
                ).strip().lower()
                if ans not in ("y", "yes"):
                    print("Aborted.")
                    sys.exit(0)
            _delete_session(conn, session_date, day_number)

        count = _insert_sets(conn, sets)
        conn.commit()
    finally:
        conn.close()

    # Summary
    exercises = {}
    for s in sets:
        exercises.setdefault(s["exercise"], []).append(s)

    print(f"\nImported {count} sets — {session_date} week {meta['week']} block {meta['block']} day {meta['day']}")
    for ex, ex_sets in exercises.items():
        counted = sum(1 for s in ex_sets if s["should_count"])
        amraps = sum(1 for s in ex_sets if s["is_amrap"])
        print(f"  {ex}: {len(ex_sets)} sets ({counted} counted, {amraps} amrap)")


if __name__ == "__main__":
    main()
