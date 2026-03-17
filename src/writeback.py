"""
Write-back executor — applies confirmed program change proposals to the Google Sheet.

Called from telegram_bot.py after the athlete confirms a PENDING_PROPOSAL.
Uses Claude Haiku to parse the proposal text into a structured operation,
then gspread to apply the cell changes directly.

Supported operations:
  WEIGHT_CHANGE    — modify weight for one exercise in a specific week
  SETS_REPS_CHANGE — modify sets/reps for one exercise
  EXERCISE_SWAP    — replace one exercise name with another
  NOTE_ADD         — add a note to an exercise row
  WEIGHT_SCALE     — scale all weights by % across one or more weeks (vacation recovery, deload)
  UNKNOWN          — parse failed or confidence too low → no change, human review needed
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

import anthropic
from config import ANTHROPIC_API_KEY, PROGRAM_SHEET_ID, CLAUDE_HAIKU

HAIKU_MODEL = CLAUDE_HAIKU


# ---------------------------------------------------------------------------
# Proposal → structured operation (LLM parse)
# ---------------------------------------------------------------------------

_PARSE_SYSTEM = """You are a structured data extractor for a strength training program spreadsheet.

Given a coaching proposal, extract the intended change as a single JSON object.
Return ONLY valid JSON — no explanation, no markdown.

Schema:
{
  "operation": "WEIGHT_CHANGE" | "SETS_REPS_CHANGE" | "EXERCISE_SWAP" | "NOTE_ADD" | "WEIGHT_SCALE" | "UNKNOWN",
  "week": <int or null>,
  "day": <int or null>,
  "exercise": "<exercise name or null>",
  "old_value": "<old value as string or null>",
  "new_value": "<new value as string or null>",
  "scale_pct": <float or null>,
  "weeks_affected": [<int>, ...],
  "note_text": "<note to add or null>",
  "confidence": "HIGH" | "MEDIUM" | "LOW"
}

Rules:
- WEIGHT_CHANGE: single exercise, new weight. "new_value" is the new weight (just the number, e.g. "82.5").
- SETS_REPS_CHANGE: single exercise, new sets/reps. "new_value" is like "4x4" or "3x5".
- EXERCISE_SWAP: replace one exercise with another. "exercise" = old name, "new_value" = new name.
- NOTE_ADD: add a note to an exercise row. "note_text" = the note.
- WEIGHT_SCALE: scale all weights by a percentage across multiple weeks. "scale_pct" = percentage (90 = 90% of current). "weeks_affected" = list of week numbers.
- UNKNOWN: if you cannot determine the operation or key fields with confidence.

Examples:
  "Reduce squat from 90kg to 82.5kg in Week 9 Day 1"
  → {"operation":"WEIGHT_CHANGE","week":9,"day":1,"exercise":"Squat","old_value":"90","new_value":"82.5","confidence":"HIGH"}

  "Scale all weights down by 10% for weeks 9 and 10 to ease back in after vacation"
  → {"operation":"WEIGHT_SCALE","scale_pct":90.0,"weeks_affected":[9,10],"confidence":"HIGH"}

  "Swap RDL for Romanian Deadlift in Week 9 Day 3"
  → {"operation":"EXERCISE_SWAP","week":9,"day":3,"exercise":"RDL","new_value":"Romanian Deadlift","confidence":"HIGH"}

  "Change bench press sets to 3x5 in Week 9"
  → {"operation":"SETS_REPS_CHANGE","week":9,"exercise":"Bench Press","new_value":"3x5","confidence":"HIGH"}
"""


def parse_proposal(proposal_text: str, current_week: int = None) -> dict:
    """
    Parse a proposal text string into a structured operation dict.
    Optionally provide current_week to help LLM resolve relative references.
    """
    context = ""
    if current_week:
        context = f"Current week: {current_week}.\n"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=300,
        system=_PARSE_SYSTEM,
        messages=[{"role": "user", "content": f"{context}Proposal: {proposal_text}"}]
    )
    raw = response.content[0].text.strip()

    # Extract JSON from response (may be wrapped in ```json ... ```)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {"operation": "UNKNOWN", "confidence": "LOW", "_raw": raw}


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _get_week_tab(sheet, week_num: int):
    """Try standard week tab name formats."""
    for name in [f"Week {week_num}", f"Semana {week_num}", f"W{week_num}"]:
        try:
            return sheet.worksheet(name)
        except Exception:
            pass
    return None


_NOTE_KEYWORDS = (
    "note", "comment", "obs", "remark", "feedback", "session", "athlete",
    "anotaci", "comentar", "notas", "remarks", "annotation",
)
_DATA_KEYWORDS = ("weight", "load", "set", "rep", "done", "complet", "actual", "status", "exercise")


def _build_col_map_from_header(header_row: list) -> dict:
    """
    Build a field → 1-indexed column number map from a header row.
    Flexible matching — works regardless of exact column names or language.
    Falls back to the last non-data column as the notes column if nothing explicit found.
    """
    col_map = {}
    last_text_col = None  # rightmost column that looks like free-text

    for i, cell in enumerate(header_row):
        label = str(cell).strip().lower()
        if not label:
            continue
        col = i + 1
        if any(kw in label for kw in ("exercise", "exercis", "ejercicio", "movimiento")):
            col_map.setdefault("exercise", col)
        elif any(kw in label for kw in ("weight", "load", "peso", "carga", "kg")):
            col_map.setdefault("weight", col)
        elif any(kw in label for kw in ("set", "rep", "x rep", "series", "repeticion")):
            col_map.setdefault("sets_reps", col)
        elif any(kw in label for kw in ("done", "complet", "status", "hecho", "realiz")):
            col_map.setdefault("done", col)
        elif "actual" in label or "performed" in label or "realizado" in label:
            col_map.setdefault("actual", col)
        elif any(kw in label for kw in ("rpe", "effort", "exertion", "rpe/rir")):
            col_map.setdefault("rpe", col)
        elif any(kw in label for kw in _NOTE_KEYWORDS):
            # Prefer "session note" / "athlete note" variants as session_note
            if any(kw in label for kw in ("session", "athlete", "my note", "anotaci")):
                col_map.setdefault("session_note", col)
            else:
                col_map.setdefault("notes", col)
            last_text_col = col  # any note column qualifies

        # Track rightmost column that doesn't look like structured data
        if not any(kw in label for kw in _DATA_KEYWORDS):
            last_text_col = col

    # Fallback: if no notes column found at all, use the last non-data column
    if "notes" not in col_map and "session_note" not in col_map and last_text_col:
        col_map["notes"] = last_text_col

    return col_map


def _find_exercise_row(all_values: list, exercise_name: str, day_num: int = None) -> tuple:
    """
    Find the row index (1-based) and col_map for an exercise in a week tab.
    If day_num is specified, only match within that day's section.
    Returns (row_1based, col_map) or (None, None).
    """
    exercise_lower = exercise_name.lower().strip()
    current_day = None
    col_map = None

    for i, row in enumerate(all_values):
        if not row:
            continue
        col0 = str(row[0]).strip() if row[0] else ""
        col0_lower = col0.lower()

        # Detect day/session section headers (flexible: "DAY 1", "Session 2", "Día 3", "Block A")
        day_match = re.match(r"(?:day|session|dia|d[íi]a|block|session)\s*(\d+)", col0_lower)
        if day_match:
            current_day = int(day_match.group(1))
            col_map = None  # reset col_map for new day
            continue

        # Detect the Exercise column header row (flexible: "exercise", "exercises", "lift", etc.)
        is_header = (
            any(kw in col0_lower for kw in ("exercise", "lift", "movimiento", "ejercicio"))
            and len([c for c in row if str(c).strip()]) >= 2
        )
        if is_header:
            col_map = _build_col_map_from_header(row)
            continue

        # Skip if we're in the wrong day
        if day_num is not None and current_day != day_num:
            continue

        # Skip if no col_map yet (haven't seen header)
        if col_map is None:
            continue

        # Check if this row is the exercise we want
        if col0_lower and exercise_lower in col0_lower:
            return i + 1, col_map  # gspread 1-indexed row

    return None, None


# ---------------------------------------------------------------------------
# Operation implementations
# ---------------------------------------------------------------------------

def _apply_weight_change(sheet, op: dict) -> tuple:
    week = op.get("week")
    exercise = op.get("exercise")
    new_val = op.get("new_value")

    if not all([week, exercise, new_val]):
        return False, "Missing week, exercise, or new_value for WEIGHT_CHANGE"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found in sheet"

    all_values = ws.get_all_values()
    row_idx, col_map = _find_exercise_row(all_values, exercise, op.get("day"))
    if not row_idx:
        return False, f"Exercise '{exercise}' not found in Week {week}"

    weight_col = col_map.get("weight")
    if not weight_col:
        return False, "Weight column not found"

    ws.update_cell(row_idx, weight_col, new_val)
    return True, f"Updated {exercise} weight to {new_val}kg in Week {week}"


def _apply_sets_reps_change(sheet, op: dict) -> tuple:
    week = op.get("week")
    exercise = op.get("exercise")
    new_val = op.get("new_value")

    if not all([week, exercise, new_val]):
        return False, "Missing week, exercise, or new_value for SETS_REPS_CHANGE"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found"

    all_values = ws.get_all_values()
    row_idx, col_map = _find_exercise_row(all_values, exercise, op.get("day"))
    if not row_idx:
        return False, f"Exercise '{exercise}' not found in Week {week}"

    sets_col = col_map.get("sets_reps")
    if not sets_col:
        return False, "Sets/Reps column not found"

    ws.update_cell(row_idx, sets_col, new_val)
    return True, f"Updated {exercise} to {new_val} in Week {week}"


def _apply_exercise_swap(sheet, op: dict) -> tuple:
    week = op.get("week")
    old_exercise = op.get("exercise")
    new_exercise = op.get("new_value")

    if not all([week, old_exercise, new_exercise]):
        return False, "Missing week, exercise, or new_value for EXERCISE_SWAP"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found"

    all_values = ws.get_all_values()
    row_idx, col_map = _find_exercise_row(all_values, old_exercise, op.get("day"))
    if not row_idx:
        return False, f"Exercise '{old_exercise}' not found in Week {week}"

    exercise_col = col_map.get("exercise", 1)
    ws.update_cell(row_idx, exercise_col, new_exercise)
    return True, f"Swapped '{old_exercise}' → '{new_exercise}' in Week {week}"


def _find_notes_row_in_tab(all_values: list) -> tuple:
    """
    Find a general notes/session row in a week tab (e.g. 'Weekly Notes', 'Session Notes').
    Returns (row_1based, col_1based) or (None, None).
    """
    _weekly_kw = ("weekly note", "session note", "week note", "notas semana",
                  "general note", "coach note", "overall note")
    for i, row in enumerate(all_values):
        label = str(row[0]).strip().lower() if row else ""
        if any(kw in label for kw in _weekly_kw):
            # Use column 2 (the value column next to the label) if it exists
            return i + 1, 2
    return None, None


def _apply_note_add(sheet, op: dict) -> tuple:
    week = op.get("week")
    exercise = op.get("exercise")
    note = op.get("note_text") or op.get("new_value")

    if not all([week, note]):
        return False, "Missing week or note_text for NOTE_ADD"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found"

    all_values = ws.get_all_values()

    # --- Strategy 1: specific exercise row ---
    if exercise:
        row_idx, col_map = _find_exercise_row(all_values, exercise, op.get("day"))
        if row_idx:
            notes_col = col_map.get("session_note") or col_map.get("notes")
            if notes_col:
                ws.update_cell(row_idx, notes_col, note)
                return True, f"Added note to '{exercise}' in Week {week}"
            # Exercise found but no notes column detected — use the next empty column after the last filled cell
            row_data = all_values[row_idx - 1]
            last_filled = max((j + 1 for j, c in enumerate(row_data) if str(c).strip()), default=1)
            ws.update_cell(row_idx, last_filled + 1, note)
            return True, f"Added note to '{exercise}' (col {last_filled + 1}) in Week {week}"

    # --- Strategy 2: look for a weekly notes row in the tab ---
    notes_row, notes_col = _find_notes_row_in_tab(all_values)
    if notes_row:
        # Append to existing value if any
        existing = ""
        try:
            existing = str(all_values[notes_row - 1][notes_col - 1]).strip()
        except (IndexError, TypeError):
            pass
        combined = f"{existing} | {note}".lstrip(" |") if existing else note
        ws.update_cell(notes_row, notes_col, combined)
        return True, f"Added note to weekly notes section in Week {week}"

    # --- Strategy 3: append a new row at the bottom of the tab ---
    next_row = len(all_values) + 1
    ws.update_cell(next_row, 1, "Coach note")
    ws.update_cell(next_row, 2, note)
    return True, f"Added coach note to Week {week} (row {next_row})"


def _apply_weight_scale(sheet, op: dict) -> tuple:
    """
    Scale all weights by scale_pct% across specified weeks.
    Weights are rounded to the nearest 2.5kg (standard plate increment).
    Used for vacation recovery, deloads, easing back in.
    """
    scale_pct = op.get("scale_pct")
    weeks = op.get("weeks_affected") or []
    if op.get("week") and not weeks:
        weeks = [op["week"]]

    if not scale_pct or not weeks:
        return False, "Missing scale_pct or weeks_affected for WEIGHT_SCALE"

    scale = scale_pct / 100.0
    total_updated = 0
    errors = []

    for week_num in weeks:
        ws = _get_week_tab(sheet, week_num)
        if not ws:
            errors.append(f"Week {week_num} tab not found")
            continue

        all_values = ws.get_all_values()
        col_map = None

        for i, row in enumerate(all_values):
            if not row:
                continue
            col0 = str(row[0]).strip().lower() if row[0] else ""

            # Detect exercise header row (flexible)
            if any(kw in col0 for kw in ("exercise", "lift", "movimiento", "ejercicio")) and len([c for c in row if str(c).strip()]) >= 2:
                col_map = _build_col_map_from_header(row)
                continue

            # Skip non-exercise rows
            if col_map is None or not col0:
                continue

            # Skip section headers (day labels, notes section)
            if re.match(r"(?:day|session|dia|block)\s*\d", col0) or "weekly notes" in col0 or "bodyweight" in col0 or "bodyweight" in col0:
                continue

            weight_col = col_map.get("weight")
            if not weight_col:
                continue

            # Get current weight value
            weight_cell = row[weight_col - 1] if len(row) >= weight_col else ""
            weight_str = str(weight_cell).strip()
            if not weight_str:
                continue

            # Extract numeric weight (handles "90kg", "90.0", "90")
            weight_match = re.search(r"(\d+(?:[.,]\d+)?)", weight_str)
            if not weight_match:
                continue

            try:
                weight_val = float(weight_match.group(1).replace(",", "."))
                if weight_val <= 0:
                    continue
                # Round to nearest 2.5kg
                new_weight = round(round(weight_val * scale / 2.5) * 2.5, 1)
                ws.update_cell(i + 1, weight_col, str(new_weight))
                total_updated += 1
            except (ValueError, TypeError):
                continue

    msg = f"Scaled {total_updated} weights to {scale_pct}% across weeks {weeks}"
    if errors:
        msg += f" (warnings: {'; '.join(errors)})"
    return total_updated > 0, msg


def _apply_rpe_log(sheet, op: dict) -> tuple:
    """
    Write RPE (and optionally RIR) to the RPE column cell for a specific exercise.
    Low-stakes write — no confirmation needed.

    op fields:
      week:     int
      exercise: str
      rpe:      str | float   (the value to write, e.g. "8" or "8 / RIR 2")
      day:      int | None    (optional — narrows search to a specific day section)

    Fallback strategy when no RPE column exists:
      - Appends "RPE X" to the session_note / notes cell so the existing
        has_rpe regex (@?RPE \\s*\\d) still fires on the next read pass.
      - If no notes column exists either, returns (False, ...) — silent skip.
    """
    week = op.get("week")
    exercise = op.get("exercise")
    rpe_value = op.get("rpe")

    if not all([week, exercise, rpe_value is not None]):
        return False, "Missing week, exercise, or rpe for RPE_LOG"

    ws = _get_week_tab(sheet, week)
    if not ws:
        return False, f"Week {week} tab not found"

    all_values = ws.get_all_values()
    row_idx, col_map = _find_exercise_row(all_values, exercise, op.get("day"))
    if not row_idx:
        return False, f"Exercise '{exercise}' not found in Week {week}"

    rpe_col = col_map.get("rpe")

    if rpe_col:
        # Dedicated RPE column found — write directly
        ws.update_cell(row_idx, rpe_col, str(rpe_value))
        return True, f"Logged RPE {rpe_value} for '{exercise}' in Week {week}"

    # No RPE column — fall back to appending to session_note / notes cell
    notes_col = col_map.get("session_note") or col_map.get("notes")
    if not notes_col:
        return False, f"No RPE or notes column found in Week {week} — RPE not written to sheet"

    row_data = all_values[row_idx - 1]
    existing = str(row_data[notes_col - 1]).strip() if len(row_data) >= notes_col else ""

    # Avoid duplicate RPE entries
    if re.search(r"@?RPE\s*\d", existing, re.IGNORECASE):
        return True, f"RPE already recorded for '{exercise}' in Week {week} (skipped duplicate)"

    combined = f"{existing} | RPE {rpe_value}".lstrip(" |") if existing else f"RPE {rpe_value}"
    ws.update_cell(row_idx, notes_col, combined)
    return True, f"Logged RPE {rpe_value} for '{exercise}' in Week {week} (appended to notes)"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_writeback(proposal_text: str, current_week: int = None,
                    program_sheet_id: str = None) -> tuple:
    """
    Parse a proposal text and apply the change to the program sheet.

    Returns (success: bool, message: str).
    The message is human-readable and safe to send to the athlete via Telegram.
    """
    if not program_sheet_id:
        program_sheet_id = PROGRAM_SHEET_ID

    if not program_sheet_id:
        return False, "Program sheet not configured (PROGRAM_SHEET_ID missing)"

    # Step 1: Parse the proposal
    operation = parse_proposal(proposal_text, current_week=current_week)
    op_type = operation.get("operation", "UNKNOWN")
    confidence = operation.get("confidence", "LOW")

    print(f"  [WriteBack] Parsed operation: {op_type} (confidence: {confidence})")

    if op_type == "UNKNOWN" or confidence == "LOW":
        return False, (
            f"Couldn't map this proposal to a specific cell change "
            f"(op={op_type}, confidence={confidence}). "
            f"I've marked it confirmed in the log — please update the sheet manually if needed."
        )

    # Step 2: Open program sheet
    try:
        from sheets import get_client
        client = get_client()
        sheet = client.open_by_key(program_sheet_id)
    except Exception as e:
        return False, f"Could not open program sheet: {e}"

    # Step 3: Apply operation
    try:
        if op_type == "WEIGHT_CHANGE":
            return _apply_weight_change(sheet, operation)
        elif op_type == "SETS_REPS_CHANGE":
            return _apply_sets_reps_change(sheet, operation)
        elif op_type == "EXERCISE_SWAP":
            return _apply_exercise_swap(sheet, operation)
        elif op_type == "NOTE_ADD":
            return _apply_note_add(sheet, operation)
        elif op_type == "WEIGHT_SCALE":
            return _apply_weight_scale(sheet, operation)
        elif op_type == "RPE_LOG":
            return _apply_rpe_log(sheet, operation)
        else:
            return False, f"Unsupported operation type: {op_type}"
    except Exception as e:
        return False, f"Write-back error ({op_type}): {e}"
