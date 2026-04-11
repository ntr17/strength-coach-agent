"""
db_reader.py — Read from coach.db and return data in analysis_engine format.

The source of truth is coach.db, not Google Sheets.
These functions produce the exact record shapes that analysis_engine.py and drive_export.py expect.
"""

import json
import sqlite3
from datetime import date
from pathlib import Path

DEFAULT_DB   = Path(__file__).parent.parent / "data" / "coach.db"
STATE_PATH   = Path(__file__).parent.parent / "system" / "state.json"
PROFILE_PATH = Path(__file__).parent.parent / "system" / "profile.json"


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _epley(weight_kg, reps):
    """Epley e1RM formula. Returns None for single reps or missing data."""
    if weight_kg is None or reps is None or reps < 2:
        return None
    return round(weight_kg * (1 + reps / 30), 1)


# ---------------------------------------------------------------------------
# Function 1 — load_lift_records
# ---------------------------------------------------------------------------

def load_lift_records(db_path=DEFAULT_DB, weeks=None) -> list[dict]:
    """
    Read all sets from lift_sets. should_count is passed through as a field
    so callers can decide: strength estimates filter should_count=1,
    volume/training-days metrics use all sets.

    Returns one dict per SET. analysis_engine handles aggregation.

    Args:
        db_path: path to coach.db
        weeks:   if given, return only the last N weeks by week_number
    """
    conn = _connect(db_path)
    try:
        # Determine week filter based on all sets
        max_week = None
        if weeks is not None:
            row = conn.execute(
                "SELECT MAX(week_number) FROM lift_sets"
            ).fetchone()
            max_week = row[0] if row and row[0] is not None else None

        query = """
            SELECT
                id, session_date, week_number, block_number, day_number,
                exercise, set_number, reps, weight_kg, is_amrap, should_count,
                rpe, notes, source
            FROM lift_sets
        """
        params: list = []
        if weeks is not None and max_week is not None:
            min_week = max_week - weeks + 1
            query += " WHERE week_number >= ?"
            params.append(min_week)

        query += " ORDER BY session_date, week_number, day_number, exercise, set_number"

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        # Parse session_date
        raw_date = row["session_date"]
        if isinstance(raw_date, str):
            try:
                session_date = date.fromisoformat(raw_date)
            except ValueError:
                session_date = None
        elif isinstance(raw_date, date):
            session_date = raw_date
        else:
            session_date = None

        weight = row["weight_kg"]
        reps   = row["reps"]
        e1rm   = _epley(weight, reps)

        records.append({
            "date":             session_date,
            "program":          "30-Week Strength",
            "week":             row["week_number"],
            "block":            row["block_number"],
            "day":              f"DAY {row['day_number']}",
            "exercise":         row["exercise"],
            "actual_weight_kg": weight,
            "actual_sets":      1,           # one row = one set
            "actual_reps":      float(reps) if reps is not None else None,
            "rpe":              row["rpe"],
            "done":             True,         # all DB records are completed sets
            "should_count":     int(row["should_count"]) if row["should_count"] is not None else 1,
            "source":           row["source"] or "sheet",
            "session_notes":    row["notes"],
            "e1rm":             e1rm,
            "is_amrap":         bool(row["is_amrap"]),
            "planned_weight_kg": None,
            "planned_sets":      None,
            "planned_reps":      None,
        })

    return records


# ---------------------------------------------------------------------------
# Function 2 — compute_personal_records
# ---------------------------------------------------------------------------

def compute_personal_records(records: list[dict]) -> dict:
    """
    Find the best e1RM per exercise from load_lift_records output.

    Returns:
        {exercise: {"e1rm": float, "weight_kg": float, "reps": float,
                    "date": date, "week": int, "program": str}}
    """
    bests: dict[str, dict] = {}

    for r in records:
        if r["e1rm"] is None:
            continue
        ex = r["exercise"]
        if ex not in bests or r["e1rm"] > bests[ex]["e1rm"]:
            bests[ex] = {
                "e1rm":       r["e1rm"],
                "weight_kg":  r["actual_weight_kg"],
                "reps":       r["actual_reps"],
                "date":       r["date"],
                "week":       r["week"],
                "program":    r["program"],
            }

    return bests


# ---------------------------------------------------------------------------
# Function 3 — load_health_records
# ---------------------------------------------------------------------------

def load_health_records(db_path=DEFAULT_DB, days=90) -> list[dict]:
    """
    Read health_log for the last N days.

    Returns dicts matching generate_health_recovery_md and
    analysis_engine.compute_sleep_correlation expectations.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                log_date, body_weight_kg, body_fat_pct,
                sleep_hours, sleep_quality, steps,
                resting_hr, hrv, source, notes
            FROM health_log
            WHERE log_date >= date('now', ? || ' days')
            ORDER BY log_date DESC
            """,
            (f"-{days}",),
        ).fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        records.append({
            "date":               str(row["log_date"]),
            "hrv_ms":             row["hrv"],
            "sleep_hrs":          row["sleep_hours"],
            "sleep_score":        row["sleep_quality"],
            "resting_hr":         row["resting_hr"],
            "steps":              row["steps"],
            "body_weight_kg":     row["body_weight_kg"],
            "body_battery_start": None,   # not tracked by simple garmin_sync
            "body_battery_end":   None,
            "source":             row["source"],
        })

    return records


# ---------------------------------------------------------------------------
# Function 4 — load_latest_estimates
# ---------------------------------------------------------------------------

def load_latest_estimates(db_path=DEFAULT_DB) -> dict:
    """
    Return the most recent strength estimate per exercise.

    Returns:
        {exercise: {"e1rm_kg": float, "e5rm_kg": float,
                    "confidence_low": float, "confidence_high": float,
                    "estimated_at": str}}
    """
    conn = _connect(db_path)
    try:
        # SQLite window function to get max estimated_at per exercise
        rows = conn.execute(
            """
            SELECT exercise, e1rm_kg, e5rm_kg, confidence_low, confidence_high, estimated_at
            FROM strength_estimates
            WHERE (exercise, estimated_at) IN (
                SELECT exercise, MAX(estimated_at)
                FROM strength_estimates
                GROUP BY exercise
            )
            """
        ).fetchall()
    except sqlite3.OperationalError:
        # Table may not exist yet
        return {}
    finally:
        conn.close()

    return {
        row["exercise"]: {
            "e1rm_kg":         row["e1rm_kg"],
            "e5rm_kg":         row["e5rm_kg"],
            "confidence_low":  row["confidence_low"],
            "confidence_high": row["confidence_high"],
            "estimated_at":    str(row["estimated_at"]),
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# Function 5 — load_state
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Read system/state.json. Returns {} if file doesn't exist."""
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Function 6 — load_profile
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    """Read system/profile.json. Returns {} if file doesn't exist."""
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Function 6b — load_medical_records
# ---------------------------------------------------------------------------

def load_medical_records(db_path=DEFAULT_DB, days=365) -> list[dict]:
    """
    Read medical_records for the last N days, most recent first.

    Returns latest value per test_name (for briefing) plus full history.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT test_date, category, test_name, value, value_text,
                   unit, ref_low, ref_high, flag, notes, source
            FROM medical_records
            WHERE test_date >= date('now', ? || ' days')
            ORDER BY test_date DESC
            """,
            (f"-{days}",),
        ).fetchall()
    except Exception:
        # Table may not exist on old DBs
        return []
    finally:
        conn.close()

    return [
        {
            "test_date":  str(row["test_date"]),
            "category":   row["category"],
            "test_name":  row["test_name"],
            "value":      row["value"],
            "value_text": row["value_text"],
            "unit":       row["unit"],
            "ref_low":    row["ref_low"],
            "ref_high":   row["ref_high"],
            "flag":       row["flag"],
            "notes":      row["notes"],
            "source":     row["source"],
        }
        for row in rows
    ]


def load_latest_medical(db_path=DEFAULT_DB) -> dict:
    """
    Return the most recent record per test_name.

    Returns:
        {test_name: {test_date, category, value, value_text, unit, ref_low, ref_high, flag}}
    """
    records = load_medical_records(db_path, days=3650)  # all time
    latest: dict[str, dict] = {}
    for r in records:
        name = r["test_name"]
        if name not in latest:  # already sorted DESC, first = latest
            latest[name] = r
    return latest


# ---------------------------------------------------------------------------
# Function 7 — compute_progression_targets
# ---------------------------------------------------------------------------

def compute_progression_targets(profile: dict, state: dict) -> dict:
    """
    Compute linear progression targets for main lifts from current week to end
    of program.

    Returns a dict in the format analysis_engine.compute_1rm_trajectory expects:
        {week_num: {"Squat": "120", "Bench Press": "105", ...}}

    String values match the old progression dict format from the Sheet parser.
    """
    current_week = state.get("current_week", 1)
    program_info = profile.get("current_program", {})
    total_weeks  = program_info.get("total_weeks", 30)

    goals_section  = profile.get("goals", {})
    current_e1rms  = goals_section.get("current_e1rm", {})
    target_e5rms   = goals_section.get("current_e5rm_targets", {})

    if not current_e1rms or not target_e5rms:
        return {}

    result: dict[int, dict] = {}

    for week in range(current_week, total_weeks + 1):
        week_targets: dict[str, str] = {}
        weeks_remaining = total_weeks - current_week

        for lift, end_val in target_e5rms.items():
            start_val = current_e1rms.get(lift)
            if start_val is None or end_val is None or weeks_remaining <= 0:
                continue
            try:
                start = float(start_val)
                end   = float(end_val)
            except (TypeError, ValueError):
                continue

            fraction = (week - current_week) / weeks_remaining
            target   = start + (end - start) * fraction
            week_targets[lift] = str(round(target, 1))

        if week_targets:
            result[week] = week_targets

    return result


# ---------------------------------------------------------------------------
# Function 8 — compute_goals
# ---------------------------------------------------------------------------

def compute_goals(profile: dict) -> dict:
    """
    Extract goals in the format analysis_engine.compute_1rm_trajectory expects.

    Returns:
        {lift_name: {"start": "str_kg", "goal": "str_kg", "gain": "str_kg"}}
    """
    goals_section = profile.get("goals", {})
    current_e1rms = goals_section.get("current_e1rm", {})
    target_e5rms  = goals_section.get("current_e5rm_targets", {})

    result: dict[str, dict] = {}

    for lift, goal_val in target_e5rms.items():
        start_val = current_e1rms.get(lift)
        try:
            start = float(start_val) if start_val is not None else None
            goal  = float(goal_val)  if goal_val  is not None else None
        except (TypeError, ValueError):
            start = None
            goal  = None

        gain = round(goal - start, 1) if (start is not None and goal is not None) else None

        result[lift] = {
            "start": str(round(start, 1)) if start is not None else "—",
            "goal":  str(round(goal, 1))  if goal  is not None else "—",
            "gain":  str(gain)            if gain  is not None else "—",
        }

    return result
