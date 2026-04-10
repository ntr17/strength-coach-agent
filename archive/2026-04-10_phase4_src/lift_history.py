"""
lift_history.py — Unified lift history builder.

Reads all week tabs from one or more program Google Sheets and returns a flat
list of exercise records. This is the canonical data store for all DS analysis.

Each record:
{
    "date":               date | None,
    "program":            str,
    "week":               int,
    "day":                str,          # "DAY 1: Squat + Bench Heavy"
    "exercise":           str,
    "planned_weight_kg":  float | None,
    "actual_weight_kg":   float | None,
    "planned_sets":       int | None,
    "planned_reps":       float | None, # average if range
    "actual_sets":        int | None,
    "actual_reps":        float | None,
    "rpe":                float | None,
    "done":               bool | None,
    "session_notes":      str | None,
    "e1rm":               float | None, # Epley, from actual if available else planned
}
"""

import re
import sys
import os
from datetime import date
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_weight_kg(s: Optional[str]) -> Optional[float]:
    """
    Parse weight strings like '92.5kg', '80 kg', '80', '92,5kg'.
    Returns None for bodyweight ('BW'), empty, or unparseable values.
    """
    if not s:
        return None
    s = str(s).strip()
    if not s or s in ("-", "—", "BW", "bw", "bodyweight"):
        return None
    # Strip unit suffix
    cleaned = re.sub(r"[kK][gG]?", "", s).replace(",", ".").strip()
    # Handle "BW+10" style
    if "bw" in cleaned.lower():
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_sets_reps(s: Optional[str]) -> tuple[Optional[int], Optional[float]]:
    """
    Parse sets x reps strings.
    Returns (sets, avg_reps).

    Examples:
      "4x4"     → (4, 4.0)
      "3x8-10"  → (3, 9.0)   # midpoint of range
      "4 x 8"   → (4, 8.0)
      "5,4,3"   → (3, 4.0)   # list of reps per set
      "5-4-3"   → (3, 4.0)
      "3"       → (None, 3.0) # just reps, no sets
      ""        → (None, None)
    """
    if not s:
        return None, None
    s = str(s).strip()
    if not s or s in ("-", "—"):
        return None, None

    # NxM or N×M or N x M
    m = re.match(r"(\d+)\s*[xX×]\s*(\d+)(?:\s*[-–]\s*(\d+))?", s)
    if m:
        sets = int(m.group(1))
        rep_lo = int(m.group(2))
        rep_hi = int(m.group(3)) if m.group(3) else rep_lo
        return sets, (rep_lo + rep_hi) / 2.0

    # Comma or dash separated list of reps: "5,4,3" or "5-4-3"
    parts = re.split(r"[,\-–]", s)
    if len(parts) > 1 and all(p.strip().isdigit() for p in parts):
        reps_list = [int(p.strip()) for p in parts]
        return len(reps_list), sum(reps_list) / len(reps_list)

    # Single number — treat as reps, sets unknown
    if s.isdigit():
        return None, float(s)

    return None, None


def epley_e1rm(weight_kg: float, reps: float) -> Optional[float]:
    """
    Epley formula: e1RM = weight × (1 + reps/30).
    Only meaningful for reps >= 2. Returns None for reps < 2 or invalid input.
    """
    if weight_kg is None or reps is None or reps < 2:
        return None
    return round(weight_kg * (1 + reps / 30.0), 1)


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_lift_history(
    program_registry: list[dict],
    max_weeks: int = 35,
) -> list[dict]:
    """
    Read all completed sessions from all program sheets in the registry.

    Args:
        program_registry: list of {"name": str, "sheet_id": str}
        max_weeks: scan up to this many week tabs per sheet

    Returns:
        Flat list of exercise records, sorted by (week, day, exercise) within
        each program. Programs ordered as provided in registry.
    """
    from sheets import get_client, _parse_week_tab
    import gspread

    client = get_client()
    all_records: list[dict] = []

    for prog in program_registry:
        prog_name = prog["name"]
        sheet_id = prog["sheet_id"]

        try:
            sheet = client.open_by_key(sheet_id)
        except Exception as e:
            print(f"  [lift_history] Cannot open sheet {sheet_id}: {e}")
            continue

        for week_num in range(1, max_weeks + 1):
            week_data = None
            for tab_name in (f"Week {week_num}", f"Semana {week_num}", f"W{week_num}"):
                try:
                    ws = sheet.worksheet(tab_name)
                    week_data = _parse_week_tab(ws.get_all_values())
                    break
                except gspread.WorksheetNotFound:
                    continue
                except Exception as e:
                    print(f"  [lift_history] Error reading Week {week_num}: {e}")
                    break

            if week_data is None:
                # Stop scanning once we hit a missing week (assumes sequential tabs)
                break

            for day in week_data.get("days", []):
                day_label = day.get("label", f"DAY ?")
                session_date = day.get("date")  # date | None

                for ex in day.get("exercises", []):
                    name = ex.get("name", "").strip()
                    if not name:
                        continue

                    planned_w = parse_weight_kg(ex.get("weight"))
                    planned_s, planned_r = parse_sets_reps(ex.get("sets_reps"))

                    # Actual: use "actual" field if filled, else use planned when done=True
                    actual_text = ex.get("actual")
                    actual_w: Optional[float] = None
                    actual_s: Optional[int] = None
                    actual_r: Optional[float] = None

                    if actual_text:
                        # Try to parse "4x4 @ 92.5kg" or "92.5kg 4x4" or just "4x4"
                        weight_match = re.search(r"(\d+(?:[.,]\d+)?)\s*kg", actual_text, re.IGNORECASE)
                        if weight_match:
                            actual_w = parse_weight_kg(weight_match.group(0))
                        sets_match = re.search(r"(\d+)\s*[xX×]\s*(\d+(?:\s*[-–]\s*\d+)?)", actual_text)
                        if sets_match:
                            actual_s, actual_r = parse_sets_reps(sets_match.group(0))

                    # Fall back to planned when done and no actual text
                    if ex.get("done") is True and actual_text is None:
                        actual_w = actual_w or planned_w
                        actual_s = actual_s or planned_s
                        actual_r = actual_r or planned_r

                    # RPE
                    rpe: Optional[float] = None
                    rpe_raw = ex.get("rpe")
                    if rpe_raw:
                        try:
                            rpe = float(str(rpe_raw).replace(",", ".").strip())
                        except ValueError:
                            pass

                    # e1RM — prefer actual, fall back to planned
                    e1rm: Optional[float] = None
                    w_for_e1rm = actual_w or planned_w
                    r_for_e1rm = actual_r or planned_r
                    if w_for_e1rm and r_for_e1rm and ex.get("done") is True:
                        e1rm = epley_e1rm(w_for_e1rm, r_for_e1rm)

                    record = {
                        "date": session_date,
                        "program": prog_name,
                        "week": week_num,
                        "day": day_label,
                        "exercise": name,
                        "planned_weight_kg": planned_w,
                        "actual_weight_kg": actual_w,
                        "planned_sets": planned_s,
                        "planned_reps": planned_r,
                        "actual_sets": actual_s,
                        "actual_reps": actual_r,
                        "rpe": rpe,
                        "done": ex.get("done"),
                        "session_notes": ex.get("session_note"),
                        "e1rm": e1rm,
                    }
                    all_records.append(record)

    return all_records


# ---------------------------------------------------------------------------
# Derived aggregates
# ---------------------------------------------------------------------------

def personal_records(records: list[dict]) -> dict[str, dict]:
    """
    For each exercise, find the record with the highest e1RM.
    Returns {exercise_name: {e1rm, weight, reps, date, week, program}}.
    """
    prs: dict[str, dict] = {}
    for r in records:
        if r["e1rm"] is None or r["done"] is not True:
            continue
        ex = r["exercise"]
        if ex not in prs or r["e1rm"] > prs[ex]["e1rm"]:
            prs[ex] = {
                "e1rm": r["e1rm"],
                "weight_kg": r["actual_weight_kg"] or r["planned_weight_kg"],
                "reps": r["actual_reps"] or r["planned_reps"],
                "date": r["date"],
                "week": r["week"],
                "program": r["program"],
            }
    return dict(sorted(prs.items()))


def records_by_week(records: list[dict], exercise_keyword: str = "") -> dict[int, list[dict]]:
    """
    Group records by week. Optionally filter by exercise keyword (case-insensitive).
    """
    out: dict[int, list[dict]] = {}
    kw = exercise_keyword.lower()
    for r in records:
        if kw and kw not in r["exercise"].lower():
            continue
        w = r["week"]
        out.setdefault(w, []).append(r)
    return out
