"""
Projection Engine — pure Python math, no LLM calls.

Computes forward projections for key metrics: 1RM per lift, bodyweight,
and program completion. Results are facts injected into the coaching prompt
so Claude interprets them, not hallucinate them.

All functions return structured dicts. format_projections_for_prompt()
converts them into a compact text block ready for prompt injection.
"""

import math
import re
from datetime import date, datetime, timedelta
from typing import Optional

from config import KEY_LIFTS  # fallback when memory not available


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """
    Simple least-squares linear regression. Returns (slope, intercept).
    slope = units-of-y per unit-of-x (e.g. kg per week).
    """
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _parse_date(date_str: str) -> Optional[date]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str[:10], fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def _weeks_since(reference_date: date, target_date: date) -> float:
    return (target_date - reference_date).days / 7.0


# ---------------------------------------------------------------------------
# 1RM Projection
# ---------------------------------------------------------------------------

def _exercise_matches(exercise_name: str, row_exercise: str) -> bool:
    """
    Match exercise_name against a Lift History row exercise name.

    Uses word-boundary matching at the start of the exercise name so that
    "Squat" matches "Squat" and "Squat (Volume)" but NOT "Front Squat".
    The match pattern must appear at the beginning of the exercise name
    (case-insensitive), followed by end-of-string or a non-word character.

    Examples:
        "Squat"      matches "Squat", "Squat (Volume)", "Squat Heavy"
        "Squat"      does NOT match "Front Squat", "Goblet Squat"
        "Bench Press" matches "Bench Press", "Bench Press (Close Grip)"
        "Bench Press" does NOT match "Dumbbell Bench Press"
    """
    pattern = r"(?i)^" + re.escape(exercise_name) + r"(\s|$|\()"
    return bool(re.match(pattern, row_exercise.strip()))


def _collect_1rm_readings(
    exercise_name: str,
    lift_history: list[dict],
    cutoff: Optional[date] = None,
) -> list[tuple[date, float]]:
    """
    Collect (date, est_1rm) pairs for an exercise from lift_history.
    cutoff: if provided, only include readings on/after this date. None = all history.
    """
    raw = []
    for row in lift_history:
        if not _exercise_matches(exercise_name, row.get("Exercise", "")):
            continue
        est = row.get("Est 1RM", "")
        date_str = row.get("Date", "")
        if not est or not date_str:
            continue
        try:
            val = float(str(est).replace(",", "."))
            d = _parse_date(date_str)
            if d and val > 0 and (cutoff is None or d >= cutoff):
                raw.append((d, val))
        except (ValueError, TypeError):
            pass
    return raw


def _extract_raw_weight(s: str) -> Optional[float]:
    """Extract numeric kg from raw strings like '92.5 x5', '90kg', '3x95'."""
    if not s:
        return None
    # Remove everything from first rep separator onward (e.g. "92.5 x5" → "92.5")
    s_clean = re.split(r'\s*[xX×]\s*\d', s)[0]
    m = re.search(r"(\d+(?:[.,]\d+)?)", s_clean)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _extract_reps_from_entry(entry: dict) -> Optional[int]:
    """
    Extract actual rep count from a lift history entry.
    Priority: Actual Weight/Reps field → Sets x Reps field.
    For "92.5 x5" → 5. For "4x5" → 5. For "x3" → 3.
    """
    actual = str(entry.get("Actual Weight/Reps") or entry.get("actual") or "")
    # "x5" or "× 5" at end → reps = 5
    m = re.search(r'[xX×]\s*(\d+)', actual)
    if m:
        return int(m.group(1))
    # "3x" at start (sets x ...) — not helpful for reps; skip
    # Try Sets x Reps field: "4x5" → reps = 5
    sr = str(entry.get("Sets x Reps") or entry.get("sets_reps") or "")
    m = re.search(r'\d+\s*[xX×]\s*(\d+)', sr)
    if m:
        return int(m.group(1))
    return None


def _collect_e1rm_for_rep_range(
    exercise_name: str,
    lift_history: list[dict],
    cutoff: Optional[date] = None,
    target_reps_min: int = 4,
    target_reps_max: int = 6,
) -> list[tuple[date, float]]:
    """
    Collect (date, e1RM) pairs derived from sets in a specific rep range.
    Uses raw weight + reps from lift history, not the pre-computed Est 1RM column.

    Useful for a 5RM track: set target_reps_min=4, target_reps_max=6.
    e1RM is estimated from raw weight+reps via compute_e1rm_multi().
    """
    try:
        from strength_tracker import compute_e1rm_multi
    except ImportError:
        return []

    raw = []
    for row in lift_history:
        if not _exercise_matches(exercise_name, row.get("Exercise", "")):
            continue
        date_str = row.get("Date", "")
        d = _parse_date(date_str)
        if not d or (cutoff is not None and d < cutoff):
            continue

        reps = _extract_reps_from_entry(row)
        if not reps or not (target_reps_min <= reps <= target_reps_max):
            continue

        actual_raw = str(row.get("Actual Weight/Reps") or row.get("actual") or "")
        weight_raw = str(row.get("Weight") or row.get("weight") or
                         row.get("Prescribed Weight") or "")
        weight = _extract_raw_weight(actual_raw) or _parse_weight_kg(weight_raw)
        if not weight or weight <= 0:
            continue

        est = compute_e1rm_multi(weight, reps)
        if est:
            raw.append((d, est["e1rm"]))

    return raw


def _slope_recent_wow(readings: list[tuple[date, float]], n_weeks: int = 4) -> Optional[float]:
    """
    Compute mean week-over-week gain from the last n_weeks unique-date readings.
    Returns None if fewer than 2 data points.
    """
    if len(readings) < 2:
        return None
    sorted_r = sorted(readings, key=lambda r: r[0])
    recent = sorted_r[-n_weeks:]
    if len(recent) < 2:
        return None
    gains = []
    for i in range(1, len(recent)):
        d_prev, v_prev = recent[i - 1]
        d_curr, v_curr = recent[i]
        weeks_between = max((d_curr - d_prev).days / 7.0, 0.5)
        gains.append((v_curr - v_prev) / weeks_between)
    return sum(gains) / len(gains) if gains else None


def project_1rm(
    exercise_name: str,
    lift_history: list[dict],
    target_1rm: float = None,
    weeks_remaining: int = None,
    window_days: int = 42,
) -> Optional[dict]:
    """
    Project estimated 1RM forward for a given exercise.

    Args:
        exercise_name: e.g. "Squat", "Bench Press"
        lift_history: rows from Lift History tab (must have Est 1RM + Date fields)
        target_1rm: goal 1RM in kg (optional — for on-track assessment)
        weeks_remaining: weeks left in program (optional)
        window_days: initial window in days (default 42 = 6 weeks).
                     Auto-expands to full history if < 4 unique-date readings found.

    Matching: word-boundary at start — "Squat" matches "Squat (Volume)" but NOT "Front Squat".

    Current 1RM: MAX in window — intentionally lighter sessions don't pull the estimate down.
    Trend (slope): linear regression over deduplicated weekly-max readings in window.

    Returns dict with:
        exercise, current_1rm, rate_per_week, projected_end_1rm,
        on_track (bool|None), weeks_to_target (float|None), data_points, window_expanded
    Returns None if insufficient data (<2 unique-date readings).
    """
    cutoff = date.today() - timedelta(days=window_days)
    raw = _collect_1rm_readings(exercise_name, lift_history, cutoff)

    # Auto-expand: if the recent window is sparse, use full history for a better picture
    window_expanded = False
    if len(raw) < 4:
        full_raw = _collect_1rm_readings(exercise_name, lift_history, cutoff=None)
        if len(full_raw) > len(raw):
            raw = full_raw
            window_expanded = True

    if not raw:
        return None

    # Deduplicate: keep max 1RM per date (multiple sessions same day → take best)
    by_date: dict[date, float] = {}
    for d, val in raw:
        by_date[d] = max(by_date.get(d, 0.0), val)

    readings = sorted(by_date.items())  # [(date, max_val), ...]

    if len(readings) < 2:
        return None

    # Use last 8 readings for trend (recent trajectory matters more)
    recent = readings[-8:]
    reference_date = recent[0][0]
    xs = [_weeks_since(reference_date, d) for d, _ in recent]
    ys = [v for _, v in recent]

    slope_linreg, intercept = _linear_regression(xs, ys)
    # Use MAX in window as current 1RM — intentionally lighter sessions shouldn't pull it down.
    # The regression slope reflects the trend; the max reflects actual capability.
    current_1rm = max(v for _, v in recent)
    latest_date = recent[-1][0]  # most recent date for projection anchor

    # --- Ensemble slope voting (3 methods, equal weight) ---
    # Method 1: linear regression on all e1RM readings (computed above)
    # Method 2: mean week-over-week gain from last 4 readings (robust to sparse data)
    # Method 3: linear regression on 5RM-derived e1RM (4-6 rep sets, separate signal)
    slope_wow = _slope_recent_wow(readings, n_weeks=4)
    _5rm_readings = _collect_e1rm_for_rep_range(
        exercise_name, lift_history, cutoff=None, target_reps_min=4, target_reps_max=6
    )
    slope_5rm: Optional[float] = None
    if len(_5rm_readings) >= 2:
        by_date_5rm: dict[date, float] = {}
        for d, val in _5rm_readings:
            by_date_5rm[d] = max(by_date_5rm.get(d, 0.0), val)
        r5_sorted = sorted(by_date_5rm.items())[-8:]
        if len(r5_sorted) >= 2:
            ref5 = r5_sorted[0][0]
            xs5 = [_weeks_since(ref5, d) for d, _ in r5_sorted]
            ys5 = [v for _, v in r5_sorted]
            slope_5rm, _ = _linear_regression(xs5, ys5)

    candidate_slopes = [s for s in [slope_linreg, slope_wow, slope_5rm] if s is not None]
    slope = sum(candidate_slopes) / len(candidate_slopes)  # equal-weight vote

    # Cap slope at 1.5% of current e1RM per week (physiological limit for trained athletes)
    MAX_WEEKLY_GAIN_PCT = 0.015
    if slope > 0:
        slope = min(slope, current_1rm * MAX_WEEKLY_GAIN_PCT)

    # Physiological ceiling per lift
    MAX_E1RM_KG = {
        "squat": 200, "back squat": 200, "deadlift": 240,
        "bench press": 150, "incline bench": 130,
        "overhead press": 110, "ohp": 110, "barbell row": 130,
    }
    _ex_lower = exercise_name.lower()
    _ceiling = next((v for k, v in MAX_E1RM_KG.items() if k in _ex_lower), None)

    projected_end = None
    if weeks_remaining is not None:
        _current_x = _weeks_since(reference_date, latest_date)
        projected_end = round(slope * (_current_x + weeks_remaining) + intercept, 1)
        if _ceiling is not None:
            projected_end = min(projected_end, _ceiling)

    on_track = None
    weeks_to_target = None
    if target_1rm is not None and slope > 0:
        weeks_to_target = (target_1rm - current_1rm) / slope
        if weeks_to_target is not None:
            on_track = (weeks_remaining is None) or (weeks_to_target <= weeks_remaining)

    return {
        "exercise": exercise_name,
        "current_1rm": round(current_1rm, 1),
        "rate_per_week": round(slope, 2),
        "slope_methods": {
            "linreg": round(slope_linreg, 3),
            "wow": round(slope_wow, 3) if slope_wow is not None else None,
            "5rm": round(slope_5rm, 3) if slope_5rm is not None else None,
            "n_voted": len(candidate_slopes),
        },
        "projected_end_1rm": projected_end,
        "target_1rm": target_1rm,
        "on_track": on_track,
        "weeks_to_target": round(weeks_to_target, 1) if weeks_to_target is not None else None,
        "data_points": len(readings),
        "trend_weeks": len(recent),
        "window_expanded": window_expanded,
    }


# ---------------------------------------------------------------------------
# Bodyweight Projection
# ---------------------------------------------------------------------------

def project_bodyweight(
    health_log: list[dict],
    target_bw: float = None,
) -> Optional[dict]:
    """
    Project bodyweight trend forward.

    Returns dict with:
        current_bw, rate_per_week, trend_direction, target_date (if target set),
        weeks_to_target (if target set + meaningful trend), data_points
    Returns None if insufficient data.
    """
    readings = []
    for row in health_log:
        bw = row.get("Bodyweight (kg)", "")
        date_str = row.get("Date", "")
        if not bw or not date_str:
            continue
        try:
            val = float(str(bw).replace(",", "."))
            d = _parse_date(date_str)
            if d and val > 0:
                readings.append((d, val))
        except (ValueError, TypeError):
            pass

    if len(readings) < 3:
        return None

    readings.sort(key=lambda r: r[0])
    recent = readings[-30:]  # last 30 data points
    reference_date = recent[0][0]
    xs = [_weeks_since(reference_date, d) for d, _ in recent]
    ys = [v for _, v in recent]

    slope, _ = _linear_regression(xs, ys)
    current_bw = recent[-1][1]

    if abs(slope) < 0.05:
        trend_direction = "stable"
    elif slope > 0:
        trend_direction = "increasing"
    else:
        trend_direction = "decreasing"

    target_date = None
    weeks_to_target = None
    if target_bw is not None and slope != 0:
        current_x = _weeks_since(reference_date, recent[-1][0])
        wks = (target_bw - current_bw) / slope
        if 0 < wks < 104:  # only meaningful if within 2 years
            weeks_to_target = round(wks, 1)
            target_date = str(recent[-1][0] + timedelta(weeks=wks))

    return {
        "current_bw": round(current_bw, 1),
        "rate_per_week": round(slope, 2),
        "trend_direction": trend_direction,
        "target_bw": target_bw,
        "target_date": target_date,
        "weeks_to_target": weeks_to_target,
        "data_points": len(readings),
        "2wk_avg": round(sum(v for _, v in readings[-14:]) / min(len(readings), 14), 1),
        "4wk_avg": round(sum(v for _, v in readings[-28:]) / min(len(readings), 28), 1),
    }


# ---------------------------------------------------------------------------
# Program Completion
# ---------------------------------------------------------------------------

def project_program_completion(
    start_date: str,
    total_weeks: int,
    today: date = None,
) -> Optional[dict]:
    """
    Compute program completion status and project end date.

    Returns dict with:
        week_num, total_weeks, pct_complete, weeks_remaining,
        estimated_end_date, days_to_end
    Returns None if start_date invalid or total_weeks <= 0.
    """
    if not start_date or not total_weeks or total_weeks <= 0:
        return None

    start = _parse_date(start_date)
    if not start:
        return None

    if today is None:
        today = date.today()

    days_elapsed = (today - start).days
    import math
    week_num = max(1, math.ceil((days_elapsed + 1) / 7))
    weeks_remaining = max(0, total_weeks - week_num)
    pct_complete = round(min(week_num / total_weeks * 100, 100), 1)
    end_date = start + timedelta(weeks=total_weeks)
    days_to_end = (end_date - today).days

    return {
        "week_num": week_num,
        "total_weeks": total_weeks,
        "pct_complete": pct_complete,
        "weeks_remaining": weeks_remaining,
        "estimated_end_date": str(end_date),
        "days_to_end": days_to_end,
    }


# ---------------------------------------------------------------------------
# Tonnage tracking — weekly volume per lift
# ---------------------------------------------------------------------------

def _parse_weight_kg(text: str) -> Optional[float]:
    """Extract numeric kg value from strings like '90kg', '90', '90.5 kg'."""
    if not text:
        return None
    cleaned = re.sub(r'\s*kg\s*$', '', str(text).strip(), flags=re.I).strip()
    try:
        return float(cleaned.replace(',', '.'))
    except (ValueError, TypeError):
        return None


def _parse_sets_reps(text: str) -> Optional[tuple]:
    """
    Parse (sets, reps) from strings like '4x5', '4 x 5', '3x8-10', '5 sets of 3'.
    For ranges like '8-10', returns the lower number.
    Returns None if unparseable.
    """
    if not text:
        return None
    text = str(text).strip()
    # "4x5" or "4 x 5" or "4X5" — optionally with range on reps side
    m = re.match(r'(\d+)\s*[xX×]\s*(\d+)', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    # "5 sets of 3"
    m = re.search(r'(\d+)\s*sets?\s*(?:of\s*)?(\d+)', text, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def compute_weekly_tonnage(
    program_data: dict,
    tracked_lifts: list[dict] = None,
) -> dict:
    """
    Compute total tonnage (kg lifted) per lift per week from recent program data.
    Only counts completed exercises (done=True) with parseable weight and sets/reps.

    Returns: {lift_name: {week_label: tonnage_kg, ...}, ...}
    """
    if tracked_lifts:
        lift_patterns = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                         if tl.get("lift_type", "MAIN") in ("MAIN", "AUXILIARY")]
    else:
        lift_patterns = KEY_LIFTS

    result: dict[str, dict[str, float]] = {}

    all_weeks = []
    recent_weeks = program_data.get("recent_weeks", [])
    current_week = program_data.get("current_week")
    if recent_weeks:
        all_weeks.extend(recent_weeks)
    if current_week:
        all_weeks.append(current_week)

    for week in all_weeks:
        week_label = f"Wk{week.get('week_num', '?')}"
        for day in week.get("days", []):
            for ex in day.get("exercises", []):
                if ex.get("done") is not True:
                    continue
                name = (ex.get("name") or "").strip()
                weight_str = ex.get("weight") or ex.get("actual") or ""
                sets_reps_str = ex.get("sets_reps") or ""

                weight = _parse_weight_kg(weight_str)
                sr = _parse_sets_reps(sets_reps_str)
                if weight is None or sr is None or weight <= 0:
                    continue
                sets, reps = sr
                tonnage = weight * sets * reps

                # Match against tracked lifts
                for _domain, pattern in lift_patterns:
                    if re.search(r'(?i)\b' + re.escape(pattern) + r'\b', name):
                        result.setdefault(pattern, {})
                        result[pattern][week_label] = result[pattern].get(week_label, 0) + tonnage
                        break

    return result


def detect_volume_spikes(
    tonnage_by_lift: dict,
    threshold: float = 0.15,
) -> list[dict]:
    """
    Detect week-over-week tonnage spikes > threshold (default 15%).
    Returns list of {lift, from_week, to_week, from_tonnage, to_tonnage, pct_increase}.
    """
    spikes = []
    for lift, weekly in tonnage_by_lift.items():
        weeks = sorted(weekly.keys())
        if len(weeks) < 2:
            continue
        prev_label = weeks[-2]
        curr_label = weeks[-1]
        prev_t = weekly[prev_label]
        curr_t = weekly[curr_label]
        if prev_t > 0:
            pct = (curr_t - prev_t) / prev_t
            if pct > threshold:
                spikes.append({
                    "lift": lift,
                    "from_week": prev_label,
                    "to_week": curr_label,
                    "from_tonnage": round(prev_t),
                    "to_tonnage": round(curr_t),
                    "pct_increase": round(pct * 100, 1),
                })
    return spikes


def format_tonnage_for_prompt(tonnage_by_lift: dict) -> str:
    """Format weekly tonnage as a compact table for prompt injection."""
    if not tonnage_by_lift:
        return ""
    lines = []
    for lift, weekly in sorted(tonnage_by_lift.items()):
        weeks = sorted(weekly.keys())
        pts = " | ".join(f"{w}: {weekly[w]:.0f}kg" for w in weeks[-4:])
        lines.append(f"  {lift}: {pts}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fatigue model — ATL / CTL / TSB (Banister impulse-response)
# ---------------------------------------------------------------------------

def compute_fatigue_model(lift_history: list[dict]) -> Optional[dict]:
    """
    Compute ATL/CTL/TSB from lift history using daily Est 1RM sum as training load proxy.

    ATL (Acute Training Load)   — exponential moving avg, tau=7 days → fatigue
    CTL (Chronic Training Load) — exponential moving avg, tau=42 days → fitness
    TSB (Training Stress Balance) = CTL - ATL → readiness

    TSB > 10:    Fresh (performance potential high)
    TSB 0-10:    Optimal (balanced)
    TSB -10 to 0: Fatigued (manageable)
    TSB < -10:   Overtrained (deload recommended)

    Returns None if insufficient data (< 14 days of history).
    """
    daily_load: dict[date, float] = {}
    for row in lift_history:
        d = _parse_date(row.get("Date", ""))
        est = row.get("Est 1RM", "")
        if not d or not est:
            continue
        try:
            load = float(str(est).replace(",", "."))
            if load > 0:
                daily_load[d] = daily_load.get(d, 0.0) + load
        except (ValueError, TypeError):
            pass

    if len(daily_load) < 7:
        return None

    # Normalize loads to 0-100 scale to make ATL/CTL/TSB interpretable
    max_load = max(daily_load.values()) or 1.0
    normalized = {d: v / max_load * 100 for d, v in daily_load.items()}

    today = date.today()
    start = min(normalized.keys())
    alpha_atl = 1 - math.exp(-1 / 7)   # 7-day decay
    alpha_ctl = 1 - math.exp(-1 / 42)  # 42-day decay

    ATL = 0.0
    CTL = 0.0
    current = start
    while current <= today:
        load = normalized.get(current, 0.0)
        ATL = ATL * (1 - alpha_atl) + load * alpha_atl
        CTL = CTL * (1 - alpha_ctl) + load * alpha_ctl
        current += timedelta(days=1)

    TSB = CTL - ATL

    if TSB > 10:
        readiness = "Fresh — high performance potential"
    elif TSB > 0:
        readiness = "Optimal — balanced load"
    elif TSB > -10:
        readiness = "Fatigued — manageable, watch recovery"
    else:
        readiness = "Overtrained — deload recommended"

    return {
        "ATL": round(ATL, 1),
        "CTL": round(CTL, 1),
        "TSB": round(TSB, 1),
        "readiness": readiness,
        "deload_recommended": TSB < -10,
    }


# ---------------------------------------------------------------------------
# Cross-program analytics
# ---------------------------------------------------------------------------

def compare_program_progress(
    lift_history: list[dict],
    program_registry: list[dict],
    current_program_info: dict = None,
) -> str:
    """
    Compare 1RM gains across historical programs vs current program.
    For each completed program: find peak 1RM in first 4 weeks vs last 4 weeks → gain.
    For current program: compare same way vs historical average at same position.

    Returns formatted text, or "" if insufficient data.
    """
    if not lift_history or not program_registry:
        return ""

    # Parse lift_history once → {date: {exercise: max_1rm}}
    by_date: dict[date, dict[str, float]] = {}
    for row in lift_history:
        d = _parse_date(row.get("Date", ""))
        ex = row.get("Exercise", "").strip()
        est = row.get("Est 1RM", "")
        if not d or not ex or not est:
            continue
        try:
            val = float(str(est).replace(",", "."))
            by_date.setdefault(d, {})
            by_date[d][ex] = max(by_date[d].get(ex, 0.0), val)
        except (ValueError, TypeError):
            pass

    if not by_date:
        return ""

    def _get_peak_1rm_in_range(start: date, end: date, lift_pattern: str) -> Optional[float]:
        vals = []
        for d, exercises in by_date.items():
            if start <= d <= end:
                for ex, val in exercises.items():
                    if re.search(r'(?i)\b' + re.escape(lift_pattern) + r'\b', ex):
                        vals.append(val)
        return max(vals) if vals else None

    key_patterns = [("Squat", "SQUAT"), ("Bench Press", "BENCH"), ("Deadlift", "DEADLIFT")]

    # Build historical program summaries
    completed = [p for p in program_registry if p.get("Status", "").lower() in ("completed", "done")]
    history_lines = []

    for prog in completed:
        start_str = prog.get("Start Date") or prog.get("start_date") or ""
        total_w = prog.get("Total Weeks") or prog.get("total_weeks") or ""
        name = prog.get("Name") or prog.get("name") or "Unknown"
        start_d = _parse_date(start_str)
        try:
            total_w = int(total_w)
        except (ValueError, TypeError):
            continue
        if not start_d or total_w <= 0:
            continue

        end_d = start_d + timedelta(weeks=total_w)
        prog_start_window = (start_d, start_d + timedelta(weeks=4))
        prog_end_window = (end_d - timedelta(weeks=4), end_d)

        gains = []
        for pattern, _domain in key_patterns:
            start_rm = _get_peak_1rm_in_range(*prog_start_window, pattern)
            end_rm = _get_peak_1rm_in_range(*prog_end_window, pattern)
            if start_rm and end_rm:
                gains.append(f"{pattern} {start_rm:.0f}→{end_rm:.0f}kg ({end_rm-start_rm:+.0f})")

        if gains:
            history_lines.append(f"  {name} ({total_w}wk): " + " | ".join(gains))

    # Current program comparison
    current_lines = []
    if current_program_info:
        curr_start = _parse_date(current_program_info.get("start_date", ""))
        curr_total = current_program_info.get("total_weeks", 0)
        if curr_start and curr_total:
            curr_start_window = (curr_start, curr_start + timedelta(weeks=4))
            today = date.today()
            for pattern, _domain in key_patterns:
                start_rm = _get_peak_1rm_in_range(*curr_start_window, pattern)
                curr_rm = _get_peak_1rm_in_range(today - timedelta(weeks=2), today, pattern)
                if start_rm and curr_rm:
                    weeks_in = (today - curr_start).days // 7
                    current_lines.append(
                        f"  {pattern}: {start_rm:.0f}→{curr_rm:.0f}kg ({curr_rm-start_rm:+.0f}kg in {weeks_in}wk)"
                    )

    lines = []
    if current_lines:
        lines.append("Current program gains so far:")
        lines.extend(current_lines)
    if history_lines:
        lines.append("Historical program gains:")
        lines.extend(history_lines)

    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Format for prompt injection
# ---------------------------------------------------------------------------

def format_projections_for_prompt(
    lift_projections: list[dict],
    bw_projection: Optional[dict],
    program_projection: Optional[dict],
    fatigue: Optional[dict] = None,
) -> str:
    """
    Convert computed projections into a compact text block for prompt injection.
    These are factual numbers — Claude interprets what they mean.
    """
    lines = []

    if program_projection:
        p = program_projection
        lines.append(
            f"Program: Week {p['week_num']}/{p['total_weeks']} "
            f"({p['pct_complete']}% complete, {p['weeks_remaining']} weeks left, "
            f"ends {p['estimated_end_date']})"
        )

    for proj in lift_projections:
        if not proj:
            continue
        ex = proj["exercise"]
        curr = proj["current_1rm"]
        rate = proj["rate_per_week"]
        data_pts = proj.get("data_points", 0)
        reliable = data_pts >= 4

        expanded = proj.get("window_expanded", False)
        scope = " (full history)" if expanded else ""
        if reliable:
            rate_str = f"{rate:+.2f}kg/wk"
            line = f"{ex}: {curr}kg est. 1RM | trend {rate_str}{scope}"
        else:
            line = f"{ex}: {curr}kg est. 1RM | trend unreliable ({data_pts} sessions{scope}, need 4+)"

        if proj.get("target_1rm"):
            line += f" | target {proj['target_1rm']}kg"

        if reliable and proj.get("projected_end_1rm") is not None:
            weeks_remaining_val = (program_projection or {}).get("weeks_remaining")
            if weeks_remaining_val is not None and weeks_remaining_val > 4:
                line += f" | projected at end: {proj['projected_end_1rm']}kg (low confidence — {weeks_remaining_val} weeks out)"
            else:
                line += f" | projected at end: {proj['projected_end_1rm']}kg"

        if reliable:
            if proj.get("on_track") is True:
                line += " | ON TRACK"
            elif proj.get("on_track") is False:
                wtt = proj.get("weeks_to_target")
                wr = program_projection["weeks_remaining"] if program_projection else None
                if wtt and wr:
                    line += f" | BEHIND ({wtt:.0f}wk needed, {wr}wk left)"
                else:
                    line += " | BEHIND TARGET"

        lines.append(line)

    if bw_projection:
        bw = bw_projection
        line = (
            f"Bodyweight: {bw['current_bw']}kg | "
            f"trend {bw['rate_per_week']:+.2f}kg/wk ({bw['trend_direction']}) | "
            f"2wk avg {bw['2wk_avg']}kg vs 4wk avg {bw['4wk_avg']}kg"
        )
        if bw.get("target_bw") and bw.get("target_date"):
            line += f" | target {bw['target_bw']}kg projected {bw['target_date']}"
        lines.append(line)

    if fatigue:
        f = fatigue
        flag = " ⚠ DELOAD RECOMMENDED" if f.get("deload_recommended") else ""
        lines.append(
            f"Fatigue: ATL={f['ATL']} CTL={f['CTL']} TSB={f['TSB']:+.1f} — {f['readiness']}{flag}"
        )

    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Convenience: run all projections from memory data
# ---------------------------------------------------------------------------

def run_all_projections(
    memory_data: dict,
    program_info: dict = None,
    program_data: dict = None,
) -> dict:
    """
    Run all projections from memory_data dict (output of memory.read_all()).
    program_info: optional dict with {start_date, total_weeks} from registry.
    program_data: optional output of sheets.read_program_data() for tonnage computation.
    Returns: {lift_projections, bw_projection, program_projection, fatigue,
              tonnage_by_lift, volume_spikes, cross_program, formatted_text}
    """
    lift_history = memory_data.get("lift_history", [])
    health_log = memory_data.get("health_log", [])
    tracked_lifts = memory_data.get("tracked_lifts")

    # Determine program info
    if program_info is None:
        registry = memory_data.get("sheet_registry", [])
        for entry in registry:
            if entry.get("Type") == "Program" and entry.get("Status", "").lower() == "active":
                program_info = {
                    "start_date": entry.get("Start Date", ""),
                    "total_weeks": int(entry.get("Total Weeks", 0) or 0),
                }
                break

    # Program completion
    program_proj = None
    weeks_remaining = None
    if program_info:
        program_proj = project_program_completion(
            program_info.get("start_date", ""),
            program_info.get("total_weeks", 0),
        )
        if program_proj:
            weeks_remaining = program_proj["weeks_remaining"]

    # Read goals from ANNUAL_ARC structured domain (avoids false regex matches on free text)
    goal_map: dict = {}
    try:
        import json as _json
        _arc_raw = memory_data.get("coach_state", {}).get("ANNUAL_ARC", {})
        _arc_summary = _arc_raw.get("Summary", _arc_raw.get("summary", "")) if isinstance(_arc_raw, dict) else str(_arc_raw)
        if _arc_summary:
            _arc = _json.loads(_arc_summary) if _arc_summary.strip().startswith("{") else {}
            _medium = _arc.get("medium_goals", {})
            _raw_goals = {
                "squat": _medium.get("squat_goal_kg"),
                "bench press": _medium.get("bench_goal_kg"),
                "deadlift": _medium.get("deadlift_goal_kg"),
                "overhead press": _medium.get("ohp_goal_kg"),
            }
            goal_map = {k: float(v) for k, v in _raw_goals.items() if v}
    except Exception:
        pass

    # Fallback: parse from free-text goals if ANNUAL_ARC not populated
    if not goal_map:
        goals_text = memory_data.get("long_term_goals", "")
        goal_map = _parse_lift_targets(goals_text)

    # Get tracked lifts from memory_data (dynamic registry) — MAIN lifts only for projections
    if tracked_lifts:
        lifts_for_proj = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                          if tl.get("lift_type", "MAIN") == "MAIN"]
    else:
        lifts_for_proj = KEY_LIFTS

    lift_projections = []
    for _domain, lift_name in lifts_for_proj:
        target = goal_map.get(lift_name.lower())
        proj = project_1rm(lift_name, lift_history, target_1rm=target,
                           weeks_remaining=weeks_remaining)
        if proj:
            lift_projections.append(proj)

    # Bodyweight
    bw_proj = project_bodyweight(health_log)

    # Fatigue model (ATL/CTL/TSB)
    fatigue = None
    try:
        fatigue = compute_fatigue_model(lift_history)
    except Exception:
        pass

    # Weekly tonnage + volume spike detection
    tonnage_by_lift = {}
    volume_spikes = []
    if program_data:
        try:
            tonnage_by_lift = compute_weekly_tonnage(program_data, tracked_lifts=tracked_lifts)
            volume_spikes = detect_volume_spikes(tonnage_by_lift)
        except Exception:
            pass

    # Cross-program analytics
    cross_program = ""
    try:
        program_registry = memory_data.get("sheet_registry", [])
        cross_program = compare_program_progress(lift_history, program_registry, program_info)
    except Exception:
        pass

    # Goal proximity alerts (within 5kg of target 1RM)
    goal_proximity = []
    try:
        goal_proximity = detect_goal_proximity(lift_projections)
    except Exception:
        pass

    formatted = format_projections_for_prompt(lift_projections, bw_proj, program_proj, fatigue)

    # Long-term projections (1yr, 2yr) — only computed if lift data exists
    long_term = {}
    try:
        long_term = project_long_term(lift_projections, weeks_remaining or 0)
    except Exception:
        pass

    return {
        "lift_projections": lift_projections,
        "bw_projection": bw_proj,
        "program_projection": program_proj,
        "fatigue": fatigue,
        "tonnage_by_lift": tonnage_by_lift,
        "volume_spikes": volume_spikes,
        "cross_program": cross_program,
        "goal_proximity": goal_proximity,
        "formatted": formatted,
        "long_term": long_term,
    }


def project_long_term(lift_projections: list[dict], weeks_remaining: int = 0) -> dict:
    """
    Project 1RM to 6 months, 1 year, and 2 years beyond current program end.

    Uses the current trend rate but applies a diminishing-returns decay:
    - Years 1-2: adaptation is still strong, ~15% decay per year on weekly rate
    - Beyond 2 years: rate slows significantly (~30% per year)

    Also projects what the athlete needs to sustain to hit long-term goals like
    Olympic lifting (snatch ~80% of BW, clean & jerk ~100% of BW for intermediate).

    Returns: {lift_name: {end_of_program, 6mo, 1yr, 2yr, olympic_readiness?}}
    """
    if not lift_projections:
        return {}

    results: dict[str, dict] = {}

    for proj in lift_projections:
        if not proj or proj.get("rate_per_week") is None or not proj.get("current_1rm"):
            continue

        curr_1rm = proj["current_1rm"]
        rate = proj["rate_per_week"]  # kg/week — can be 0 or negative
        exercise = proj.get("exercise", "?")
        target = proj.get("target_1rm")

        # Project end of current program
        end_of_prog = round(curr_1rm + rate * weeks_remaining, 1) if weeks_remaining > 0 else curr_1rm

        # Beyond program: diminishing returns decay on weekly rate
        # 6 months post-program = ~26 weeks; 1yr = ~52wk; 2yr = ~104wk
        def _project(weeks_after_program: int, annual_decay: float = 0.15) -> float:
            """Project 1RM N weeks after end of program with given annual decay."""
            years = weeks_after_program / 52.0
            decay_factor = (1 - annual_decay) ** years
            gain = rate * decay_factor * weeks_after_program
            return round(end_of_prog + gain, 1)

        six_mo = _project(26)
        one_yr = _project(52)
        two_yr = _project(104)

        entry: dict = {
            "end_of_program": end_of_prog,
            "6mo": six_mo,
            "1yr": one_yr,
            "2yr": two_yr,
            "current_rate": rate,
            "target": target,
        }

        # Olympic lifting readiness estimate (for squat/deadlift)
        # Intermediate Olympic lifter benchmarks: Snatch ~80% BW, C&J ~100% BW
        # These are rough correlates: competitive snatch ≈ 50-60% of back squat 1RM
        if "squat" in exercise.lower() and one_yr > 0:
            est_snatch_1yr = round(one_yr * 0.55, 1)
            est_cj_1yr = round(one_yr * 0.70, 1)
            entry["olympic_note"] = (
                f"At 1yr squat of {one_yr}kg: est. snatch ~{est_snatch_1yr}kg, "
                f"clean & jerk ~{est_cj_1yr}kg (rough transfer heuristic)"
            )

        results[exercise] = entry

    return results


def format_long_term_projections(long_term: dict) -> str:
    """
    Format long-term projections for prompt injection.
    Returns empty string if no data.
    """
    if not long_term:
        return ""
    lines = []
    for exercise, proj in long_term.items():
        parts = [
            f"end of program: {proj.get('end_of_program', '?')}kg",
            f"6mo: {proj.get('6mo', '?')}kg",
            f"1yr: {proj.get('1yr', '?')}kg",
            f"2yr: {proj.get('2yr', '?')}kg",
        ]
        if proj.get("target"):
            parts.append(f"program target: {proj['target']}kg")
        line = f"  {exercise}: {' | '.join(parts)}"
        if proj.get("olympic_note"):
            line += f"\n    → {proj['olympic_note']}"
        lines.append(line)
    return "\n".join(lines)


def detect_goal_proximity(lift_projections: list, threshold_kg: float = 5.0) -> list:
    """
    Return a list of {lift, current_1rm, target, gap, urgent} dicts for lifts
    within threshold_kg of their target 1RM.

    urgent=True when current_1rm >= target (goal reached or exceeded).
    urgent=False when within threshold but not yet there.
    """
    alerts = []
    for proj in lift_projections:
        if not proj or not proj.get("target_1rm"):
            continue
        current = proj.get("current_1rm", 0.0)
        target = proj["target_1rm"]
        gap = target - current
        if abs(gap) <= threshold_kg:
            alerts.append({
                "lift": proj["exercise"],
                "current_1rm": current,
                "target": target,
                "gap": round(gap, 1),
                "urgent": gap <= 0,  # at or past goal
            })
    return alerts


def _parse_lift_targets(goals_text: str) -> dict:
    """
    Extract target 1RM values from free-form goals text.
    E.g. "120kg squat" → {"squat": 120.0}
    Simple heuristic — not exhaustive.
    """
    targets = {}
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*kg\s+squat", "squat"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+bench", "bench press"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+deadlift", "deadlift"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+ohp", "ohp"),
        (r"(\d+(?:\.\d+)?)\s*kg\s+overhead", "ohp"),
        (r"squat[^\d]{0,10}(\d+(?:\.\d+)?)\s*kg", "squat"),
        (r"bench[^\d]{0,10}(\d+(?:\.\d+)?)\s*kg", "bench press"),
        (r"deadlift[^\d]{0,10}(\d+(?:\.\d+)?)\s*kg", "deadlift"),
    ]
    for pattern, lift in patterns:
        m = re.search(pattern, goals_text, re.IGNORECASE)
        if m and lift not in targets:
            try:
                targets[lift] = float(m.group(1))
            except (ValueError, IndexError):
                pass
    return targets


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    # Quick self-test with synthetic data
    from datetime import timedelta

    today = date.today()
    mock_history = []
    for i in range(12):
        d = today - timedelta(weeks=11 - i)
        mock_history.append({
            "Date": str(d),
            "Exercise": "Squat",
            "Est 1RM": str(80.0 + i * 1.5),
        })
        mock_history.append({
            "Date": str(d),
            "Exercise": "Bench Press",
            "Est 1RM": str(70.0 + i * 0.8),
        })

    mock_health = []
    for i in range(30):
        d = today - timedelta(days=29 - i)
        mock_health.append({
            "Date": str(d),
            "Bodyweight (kg)": str(82.0 + i * 0.02),
        })

    squat = project_1rm("Squat", mock_history, target_1rm=120.0, weeks_remaining=22)
    bench = project_1rm("Bench Press", mock_history, target_1rm=105.0, weeks_remaining=22)
    bw = project_bodyweight(mock_health)
    program = project_program_completion("2026-01-13", 30)

    formatted = format_projections_for_prompt([squat, bench], bw, program)
    print("=== PROJECTIONS ===")
    print(formatted)
    print("\nRaw Squat projection:", squat)
    print("Raw BW projection:", bw)
    print("Program completion:", program)
