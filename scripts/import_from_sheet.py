"""
import_from_sheet.py — Import session and health data from Google Sheets into coach.db.

Sheet tabs:
  SESSION_INPUT: Date | Day | Exercise | Sets | Reps | Weight_kg | RPE | should_count | Notes
  HEALTH_INPUT:  Date | Weight_kg | Body_fat_pct | Visceral_fat_index | Notes

Row tracking: state.json stores last_session_row_imported and last_health_row_imported
(1-indexed, 0 = nothing imported yet). Pipeline advances these pointers after each import.

Usage:
  python scripts/import_from_sheet.py
  python scripts/import_from_sheet.py --dry-run
"""

import json
import os
import sqlite3
import sys
from datetime import date as date_cls
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

DEFAULT_DB    = Path(__file__).parent.parent / "data" / "coach.db"
STATE_PATH    = Path(__file__).parent.parent / "system" / "state.json"
PROFILE_PATH  = Path(__file__).parent.parent / "system" / "profile.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _parse_sets_reps(sets_str: str, reps_str: str) -> tuple[int, int]:
    """Parse Sets and Reps columns. Returns (sets, reps). Both must be integers."""
    try:
        s = int(str(sets_str).strip())
    except (ValueError, TypeError):
        s = 1
    try:
        r = int(str(reps_str).strip().rstrip('+'))
    except (ValueError, TypeError):
        r = 0
    return s, r


def _parse_float(val: str) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _is_amrap(reps_str: str) -> bool:
    return str(reps_str).strip().endswith('+')


def import_sessions(db_path: Path, sheet_id: str, state: dict, week_num: int, block_num: int, dry_run: bool = False) -> int:
    """
    Read SESSION_INPUT rows, import new ones to lift_sets.
    Returns number of sets inserted.
    """
    from sheets_reader import read_tab

    rows = read_tab(sheet_id, "SESSION_INPUT", skip_header=True)
    last_imported = state.get("last_session_row_imported", 0)
    new_rows = rows[last_imported:]

    if not new_rows:
        print("[sheet_import] SESSION_INPUT: no new rows")
        return 0

    print(f"[sheet_import] SESSION_INPUT: {len(new_rows)} new rows to import")

    sets_inserted = 0
    conn = sqlite3.connect(str(db_path))

    try:
        for i, row in enumerate(new_rows):
            # Pad row to 9 columns
            row = list(row) + [""] * (9 - len(row))
            date_str, day, exercise, sets_str, reps_str, weight_str, rpe_str, should_count_str, notes = row[:9]

            if not date_str.strip() or not exercise.strip():
                continue  # skip empty rows

            # Parse fields
            sets_count, reps = _parse_sets_reps(sets_str, reps_str)
            weight = _parse_float(weight_str)
            rpe    = _parse_float(rpe_str)
            amrap  = _is_amrap(reps_str)
            should = 0 if str(should_count_str).strip() == "0" else 1

            # Parse day number from "D1", "D2", etc.
            day_num = 1
            if day.strip().upper().startswith("D"):
                try:
                    day_num = int(day.strip()[1:])
                except ValueError:
                    pass

            if dry_run:
                print(f"  [dry] {date_str} {exercise} {sets_count}x{reps} @ {weight}kg RPE={rpe} count={should}")
                sets_inserted += sets_count
                continue

            # Expand sets_count rows
            for set_num in range(1, sets_count + 1):
                conn.execute(
                    """
                    INSERT INTO lift_sets
                      (session_date, week_number, block_number, day_number,
                       exercise, set_number, reps, weight_kg, is_amrap,
                       should_count, rpe, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date_str.strip(), week_num, block_num, day_num,
                        exercise.strip(), set_num, reps, weight, int(amrap),
                        should, rpe, notes.strip() or None,
                    )
                )
                sets_inserted += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    if not dry_run:
        state["last_session_row_imported"] = last_imported + len(new_rows)
        print(f"[sheet_import] Inserted {sets_inserted} sets. Pointer → row {state['last_session_row_imported']}")

    return sets_inserted


def import_health(db_path: Path, sheet_id: str, state: dict, dry_run: bool = False) -> int:
    """
    Read HEALTH_INPUT rows, merge into health_log.
    Returns number of rows inserted/updated.
    """
    from sheets_reader import read_tab

    rows = read_tab(sheet_id, "HEALTH_INPUT", skip_header=True)
    last_imported = state.get("last_health_row_imported", 0)
    new_rows = rows[last_imported:]

    if not new_rows:
        print("[sheet_import] HEALTH_INPUT: no new rows")
        return 0

    print(f"[sheet_import] HEALTH_INPUT: {len(new_rows)} new rows to import")

    imported = 0
    conn = sqlite3.connect(str(db_path))

    try:
        for row in new_rows:
            # Pad to 5 columns: Date | Weight_kg | Body_fat_pct | Visceral_fat_index | Notes
            row = list(row) + [""] * (5 - len(row))
            date_str, weight_str, fat_str, visceral_str, notes = row[:5]

            if not date_str.strip():
                continue

            weight   = _parse_float(weight_str)
            fat_pct  = _parse_float(fat_str)
            visceral = _parse_float(visceral_str)

            if dry_run:
                print(f"  [dry] {date_str} weight={weight} fat={fat_pct}% visceral={visceral}")
                imported += 1
                continue

            conn.execute(
                """
                INSERT INTO health_log (log_date, body_weight_kg, body_fat_pct, visceral_fat_index, source, notes)
                VALUES (?, ?, ?, ?, 'manual', ?)
                ON CONFLICT(log_date) DO UPDATE SET
                  body_weight_kg     = COALESCE(excluded.body_weight_kg,     body_weight_kg),
                  body_fat_pct       = COALESCE(excluded.body_fat_pct,       body_fat_pct),
                  visceral_fat_index = COALESCE(excluded.visceral_fat_index, visceral_fat_index),
                  notes              = COALESCE(excluded.notes,              notes)
                """,
                (date_str.strip(), weight, fat_pct, visceral, notes.strip() or None)
            )
            imported += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    if not dry_run:
        state["last_health_row_imported"] = last_imported + len(new_rows)
        print(f"[sheet_import] Imported {imported} health rows. Pointer → row {state['last_health_row_imported']}")

    return imported


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    args = parser.parse_args()

    profile = _load_json(PROFILE_PATH)
    state   = _load_json(STATE_PATH)

    sheet_id = profile.get("input_sheet_id") or os.environ.get("INPUT_SHEET_ID")
    if not sheet_id:
        print("[sheet_import] No input_sheet_id in profile.json or INPUT_SHEET_ID env var. Skipping.")
        return

    week_num  = state.get("current_week", 1)
    block_num = state.get("current_block", 1)
    db_path   = Path(args.db_path)

    sets   = import_sessions(db_path, sheet_id, state, week_num, block_num, dry_run=args.dry_run)
    health = import_health(db_path, sheet_id, state, dry_run=args.dry_run)

    if not args.dry_run and (sets > 0 or health > 0):
        _save_json(STATE_PATH, state)
        print(f"[sheet_import] state.json updated (session row {state.get('last_session_row_imported')}, health row {state.get('last_health_row_imported')})")


if __name__ == "__main__":
    main()
