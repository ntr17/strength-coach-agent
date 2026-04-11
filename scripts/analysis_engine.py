"""
analysis_engine.py — DS analysis on top of unified lift history + Garmin data.

All functions are pure Python (no LLM, no API calls). Results are plain dicts
that drive_export.py formats into markdown for the Claude Project.

Functions:
  detect_stalls(records, min_weeks)     → stall status per exercise
  compute_volume_trends(records, weeks) → weekly sets per muscle group
  compute_load_index(records)           → recent load vs baseline ratio
  compute_1rm_trajectory(records, ...)  → actual vs target e1RM by week
  compute_adherence(records)            → sessions done / planned
  compute_sleep_correlation(records, garmin_data) → HRV/sleep vs RPE
  run_all(records, garmin_data, progression, goals) → combined results dict
"""

from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Muscle group mapping (keyword-based, case-insensitive)
# ---------------------------------------------------------------------------

_MUSCLE_MAP = [
    (["squat", "hack squat", "leg press", "lunge", "leg extension", "step up", "goblet"], "Legs"),
    (["deadlift", "rdl", "romanian", "hip thrust", "good morning", "nordic", "leg curl", "hamstring"], "Posterior"),
    (["bench press", "bench", "fly", "chest press", "push up", "pushup", "dumbbell press"], "Chest"),
    (["ohp", "overhead press", "shoulder press", "lateral raise", "front raise", "face pull", "arnold"], "Shoulders"),
    (["row", "pull-up", "pullup", "chin-up", "chinup", "pull up", "lat pulldown", "seated row",
      "cable row", "t-bar", "pendlay", "kroc"], "Back"),
    (["curl", "bicep", "hammer curl", "preacher"], "Arms"),
    (["tricep", "dip", "skull crusher", "close grip", "pushdown", "overhead extension"], "Arms"),
    (["calf", "standing calf", "seated calf"], "Calves"),
    (["plank", "ab ", "crunch", "sit-up", "situp", "cable crunch", "leg raise", "core"], "Core"),
]


def _muscle_group(exercise: str) -> str:
    name = exercise.lower()
    for keywords, group in _MUSCLE_MAP:
        if any(kw in name for kw in keywords):
            return group
    return "Other"


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------

def detect_stalls(records: list[dict], min_weeks: int = 3) -> dict[str, dict]:
    """
    For each exercise with ≥ min_weeks of completed sessions, check if the
    peak e1RM in the last min_weeks of data is not higher than in the
    min_weeks period before that.

    Returns dict keyed by exercise name:
    {
        "status":      "PROGRESSING" | "STALL" | "INSUFFICIENT_DATA",
        "weeks_seen":  int,
        "recent_peak": float | None,   # e1RM in latest min_weeks weeks
        "prior_peak":  float | None,   # e1RM in min_weeks before that
        "delta":       float | None,   # recent_peak - prior_peak
        "last_week":   int | None,
        "last_e1rm":   float | None,
    }
    """
    # Group e1RM by (exercise, week) — take the max e1RM per week
    # Only use should_count=1 sets for strength estimates
    ex_week_e1rm: dict[str, dict[int, float]] = {}
    for r in records:
        if r["done"] is not True or r["e1rm"] is None or r.get("should_count", 1) != 1:
            continue
        ex = r["exercise"]
        w = r["week"]
        ex_week_e1rm.setdefault(ex, {})
        if r["e1rm"] > ex_week_e1rm[ex].get(w, 0):
            ex_week_e1rm[ex][w] = r["e1rm"]

    results: dict[str, dict] = {}
    for ex, week_map in ex_week_e1rm.items():
        weeks_sorted = sorted(week_map.keys())
        n = len(weeks_sorted)

        last_week = weeks_sorted[-1] if weeks_sorted else None
        last_e1rm = week_map[last_week] if last_week else None

        if n < min_weeks:
            results[ex] = {
                "status": "INSUFFICIENT_DATA",
                "weeks_seen": n,
                "recent_peak": last_e1rm,
                "prior_peak": None,
                "delta": None,
                "last_week": last_week,
                "last_e1rm": last_e1rm,
            }
            continue

        recent_weeks = weeks_sorted[-min_weeks:]
        prior_weeks = weeks_sorted[-2 * min_weeks:-min_weeks] if n >= 2 * min_weeks else weeks_sorted[:-min_weeks]

        recent_peak = max(week_map[w] for w in recent_weeks)
        prior_peak = max(week_map[w] for w in prior_weeks) if prior_weeks else None

        if prior_peak is None:
            status = "INSUFFICIENT_DATA"
            delta = None
        elif recent_peak > prior_peak:
            status = "PROGRESSING"
            delta = round(recent_peak - prior_peak, 1)
        else:
            status = "STALL"
            delta = round(recent_peak - prior_peak, 1)

        results[ex] = {
            "status": status,
            "weeks_seen": n,
            "recent_peak": recent_peak,
            "prior_peak": prior_peak,
            "delta": delta,
            "last_week": last_week,
            "last_e1rm": last_e1rm,
        }

    # Sort: stalls first, then by exercise name
    return dict(sorted(results.items(), key=lambda kv: (0 if kv[1]["status"] == "STALL" else 1, kv[0])))


# ---------------------------------------------------------------------------
# Volume trends
# ---------------------------------------------------------------------------

def compute_volume_trends(records: list[dict], weeks: int = 8) -> dict:
    """
    Weekly working sets per muscle group for the last N weeks.

    Returns:
    {
        "weeks": [w1, w2, ...],                    # sorted week numbers
        "groups": {
            "Chest": [12, 14, 10, 8, ...],         # sets per week
            "Back":  [...],
            ...
        },
        "total_sets_per_week": [40, 44, ...],
    }
    """
    # Only real sessions (source='sheet') count for volume — bootstrap dates are approximate
    real = [r for r in records if r.get("source", "sheet") == "sheet"]
    done_weeks = sorted({r["week"] for r in real if r["done"] is True})
    if not done_weeks:
        return {"weeks": [], "groups": {}, "total_sets_per_week": []}

    latest = done_weeks[-1]
    target_weeks = [w for w in done_weeks if w >= latest - weeks + 1]

    group_sets: dict[str, dict[int, int]] = {}  # {group: {week: count}}
    for r in real:
        if r["done"] is not True or r["week"] not in target_weeks:
            continue
        group = _muscle_group(r["exercise"])
        sets = r["actual_sets"] or r["planned_sets"] or 1
        group_sets.setdefault(group, {})
        group_sets[group][r["week"]] = group_sets[group].get(r["week"], 0) + sets

    # Fill zeros for missing weeks
    all_groups = sorted(group_sets.keys())
    groups_out: dict[str, list[int]] = {}
    for g in all_groups:
        groups_out[g] = [group_sets[g].get(w, 0) for w in target_weeks]

    total = [
        sum(group_sets[g].get(w, 0) for g in all_groups)
        for w in target_weeks
    ]

    return {
        "weeks": target_weeks,
        "groups": groups_out,
        "total_sets_per_week": total,
    }


# ---------------------------------------------------------------------------
# Load index
# ---------------------------------------------------------------------------

def compute_load_index(records: list[dict]) -> dict:
    """
    Compare recent 4-week total volume to 8-week rolling baseline.
    Volume = sum of (sets × reps × weight_kg) for done exercises per week.

    Returns:
    {
        "weeks": [w1, ...],
        "volume_per_week": [float, ...],
        "recent_4w_avg": float | None,
        "baseline_8w_avg": float | None,
        "ratio": float | None,     # recent / baseline; >1.3 = deload signal
        "signal": "HIGH" | "NORMAL" | "LOW" | "INSUFFICIENT_DATA",
    }
    """
    # Only real sessions (source='sheet') count for load — bootstrap dates are approximate
    week_volume: dict[int, float] = {}
    for r in records:
        if r["done"] is not True or r.get("source", "sheet") != "sheet":
            continue
        w_kg = r["actual_weight_kg"] or r["planned_weight_kg"] or 0
        sets = r["actual_sets"] or r["planned_sets"] or 1
        reps = r["actual_reps"] or r["planned_reps"] or 1
        vol = w_kg * sets * reps
        week_volume[r["week"]] = week_volume.get(r["week"], 0) + vol

    if not week_volume:
        return {
            "weeks": [], "volume_per_week": [],
            "recent_4w_avg": None, "baseline_8w_avg": None,
            "ratio": None, "signal": "INSUFFICIENT_DATA",
        }

    weeks_sorted = sorted(week_volume.keys())
    volumes = [week_volume[w] for w in weeks_sorted]

    recent = volumes[-4:] if len(volumes) >= 4 else volumes
    baseline = volumes[-12:-4] if len(volumes) >= 12 else volumes[:-4] if len(volumes) > 4 else volumes

    recent_avg = sum(recent) / len(recent) if recent else None
    baseline_avg = sum(baseline) / len(baseline) if baseline else None

    ratio = None
    signal = "INSUFFICIENT_DATA"
    if recent_avg and baseline_avg and baseline_avg > 0:
        ratio = round(recent_avg / baseline_avg, 2)
        if ratio > 1.3:
            signal = "HIGH"
        elif ratio < 0.7:
            signal = "LOW"
        else:
            signal = "NORMAL"

    return {
        "weeks": weeks_sorted,
        "volume_per_week": [round(v) for v in volumes],
        "recent_4w_avg": round(recent_avg) if recent_avg else None,
        "baseline_8w_avg": round(baseline_avg) if baseline_avg else None,
        "ratio": ratio,
        "signal": signal,
    }


# ---------------------------------------------------------------------------
# 1RM trajectory vs program targets
# ---------------------------------------------------------------------------

def compute_1rm_trajectory(
    records: list[dict],
    progression: dict,     # {week_num: {"Squat": "92.5kg", ...}}
    goals: dict,           # {"Squat": {"start": "70kg", "goal": "120kg"}, ...}
    key_lifts: Optional[list[str]] = None,
) -> dict:
    """
    For each key lift, compare actual weekly peak e1RM against program targets.

    Returns:
    {
        "Squat": {
            "start_kg": 70.0,
            "goal_kg": 120.0,
            "by_week": {
                1: {"target_kg": 72.0, "actual_e1rm": 74.5},
                2: {...},
            },
            "current_e1rm": float | None,
            "current_target": float | None,
            "gap_to_goal": float | None,   # goal - current_e1rm
            "on_track": bool | None,       # current_e1rm >= current_target
        },
        ...
    }
    """
    from lift_history import parse_weight_kg

    if key_lifts is None:
        # Use lifts that appear in both goals and progression
        key_lifts = list(goals.keys())

    # Build actual e1RM per week per lift (max e1RM across exercises matching the lift name)
    actual: dict[str, dict[int, float]] = {}
    for r in records:
        if r["done"] is not True or r["e1rm"] is None:
            continue
        for lift in key_lifts:
            if lift.lower() in r["exercise"].lower():
                actual.setdefault(lift, {})
                w = r["week"]
                if r["e1rm"] > actual[lift].get(w, 0):
                    actual[lift][w] = r["e1rm"]

    result = {}
    for lift in key_lifts:
        goal_info = goals.get(lift, {})
        start_kg = parse_weight_kg(goal_info.get("start"))
        goal_kg = parse_weight_kg(goal_info.get("goal"))

        # Build per-week view
        all_weeks = sorted(set(
            list(actual.get(lift, {}).keys()) +
            [w for w in progression.keys() if lift in progression[w]]
        ))

        by_week = {}
        for w in all_weeks:
            target_str = progression.get(w, {}).get(lift)
            target_kg = parse_weight_kg(target_str)
            actual_e1rm = actual.get(lift, {}).get(w)
            by_week[w] = {"target_kg": target_kg, "actual_e1rm": actual_e1rm}

        # Current values = latest week with actual data
        actual_weeks = sorted(actual.get(lift, {}).keys())
        current_e1rm = actual[lift][actual_weeks[-1]] if actual_weeks else None
        current_week_num = actual_weeks[-1] if actual_weeks else None

        # Current target = target for current week (or next available)
        current_target = None
        if current_week_num:
            for w in range(current_week_num, current_week_num + 5):
                tgt = progression.get(w, {}).get(lift)
                if tgt:
                    current_target = parse_weight_kg(tgt)
                    break

        gap = round(goal_kg - current_e1rm, 1) if (goal_kg and current_e1rm) else None
        on_track = (current_e1rm >= current_target) if (current_e1rm and current_target) else None

        result[lift] = {
            "start_kg": start_kg,
            "goal_kg": goal_kg,
            "by_week": by_week,
            "current_e1rm": current_e1rm,
            "current_target": current_target,
            "gap_to_goal": gap,
            "on_track": on_track,
        }

    return result


# ---------------------------------------------------------------------------
# Adherence
# ---------------------------------------------------------------------------

def compute_adherence(records: list[dict], planned_days_per_week: int = 4) -> dict:
    """
    Training sessions done vs planned (planned_days_per_week per week).
    Counts distinct session dates per week — not individual sets.

    Returns:
    {
        "overall":        {"done": int, "planned": int, "rate": float},
        "last_4_weeks":   {"done": int, "planned": int, "rate": float},
        "by_week": {week: {"done": int, "planned": int, "rate": float}},
    }
    """
    from collections import defaultdict
    # Only real sessions (source='sheet') — bootstrap dates are approximate
    week_dates: dict[int, set] = defaultdict(set)
    for r in records:
        if r["done"] is True and r.get("date") is not None and r.get("source", "sheet") == "sheet":
            week_dates[r["week"]].add(r["date"])

    def _rate(done: int, planned: int) -> float:
        return 0.0 if planned == 0 else round(done / planned, 3)

    by_week = {w: {"done": len(dates), "planned": planned_days_per_week,
                   "rate": _rate(len(dates), planned_days_per_week)}
               for w, dates in sorted(week_dates.items())}

    total_done = sum(v["done"] for v in by_week.values())
    total_planned = sum(v["planned"] for v in by_week.values())

    recent_weeks = sorted(by_week.keys())[-4:]
    r4_done = sum(by_week[w]["done"] for w in recent_weeks)
    r4_planned = sum(by_week[w]["planned"] for w in recent_weeks)

    return {
        "overall": {"done": total_done, "planned": total_planned, "rate": _rate(total_done, total_planned)},
        "last_4_weeks": {"done": r4_done, "planned": r4_planned, "rate": _rate(r4_done, r4_planned)},
        "by_week": by_week,
    }


# ---------------------------------------------------------------------------
# Sleep / HRV → performance correlation
# ---------------------------------------------------------------------------

def compute_sleep_correlation(
    records: list[dict],
    garmin_data: list[dict],
    min_n: int = 20,
) -> Optional[dict]:
    """
    Pearson correlation between prior-night HRV (or sleep hours) and next-day RPE.

    Only computed when N ≥ min_n paired observations exist.

    Returns None if insufficient data, else:
    {
        "n":              int,
        "hrv_vs_rpe":     {"r": float, "direction": "inverse" | "positive", "strength": str} | None,
        "sleep_vs_rpe":   {"r": float, ...} | None,
        "note":           str,
    }
    """
    import math

    # Build date → garmin lookup
    garmin_by_date: dict[str, dict] = {g["date"]: g for g in garmin_data}

    # Build pairs: (hrv, sleep_hrs, rpe) for sessions where all three are available
    pairs_hrv: list[tuple[float, float]] = []  # (hrv, rpe)
    pairs_sleep: list[tuple[float, float]] = []  # (sleep_hrs, rpe)

    for r in records:
        if r["done"] is not True or r["rpe"] is None:
            continue
        session_date = r["date"]
        if session_date is None:
            continue
        # Look for garmin data from the night before the session
        date_str = str(session_date)
        g = garmin_by_date.get(date_str)
        if g is None:
            continue
        rpe = r["rpe"]
        if g.get("hrv_ms") is not None:
            pairs_hrv.append((float(g["hrv_ms"]), float(rpe)))
        if g.get("sleep_hrs") is not None:
            pairs_sleep.append((float(g["sleep_hrs"]), float(rpe)))

    def pearson(pairs: list[tuple[float, float]]) -> Optional[float]:
        if len(pairs) < min_n:
            return None
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if sx == 0 or sy == 0:
            return None
        return round(cov / (sx * sy), 3)

    def _describe(r: float) -> dict:
        direction = "inverse" if r < 0 else "positive"
        abs_r = abs(r)
        if abs_r >= 0.5:
            strength = "strong"
        elif abs_r >= 0.3:
            strength = "moderate"
        else:
            strength = "weak"
        return {"r": r, "direction": direction, "strength": strength}

    hrv_r = pearson(pairs_hrv)
    sleep_r = pearson(pairs_sleep)

    if hrv_r is None and sleep_r is None:
        return None

    n = max(len(pairs_hrv), len(pairs_sleep))
    note = (
        "Higher HRV before a session correlates with lower RPE (better performance)."
        if (hrv_r and hrv_r < -0.2) else
        "No strong HRV-performance relationship detected yet."
    )

    return {
        "n": n,
        "hrv_vs_rpe": _describe(hrv_r) if hrv_r is not None else None,
        "sleep_vs_rpe": _describe(sleep_r) if sleep_r is not None else None,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Strength trends (week-by-week e1RM history + rate of gain + projection)
# ---------------------------------------------------------------------------

def compute_strength_trends(records: list[dict], key_lifts: list[str] = None, history_weeks: int = 12) -> dict:
    """
    For each key lift: week-by-week e1RM, rate of gain (kg/week from linear regression
    on last 4-6 weeks), and projection (weeks to hit goal at current rate).

    Returns:
    {
        "Squat": {
            "history": [(week, e1rm), ...],   # sorted by week, last history_weeks
            "rate_kg_per_week": float | None, # from linear regression last 4-6 data points
            "projection_weeks": float | None, # weeks to hit goal at current rate
            "goal_kg": float | None,
            "current_e1rm": float | None,
        },
        ...
    }
    """
    if key_lifts is None:
        key_lifts = ["Squat", "Bench Press", "Deadlift", "OHP"]

    # Build max e1RM per week per lift — only should_count=1 sets for estimates
    ex_week: dict[str, dict[int, float]] = {}
    for r in records:
        if r["done"] is not True or r["e1rm"] is None or r.get("should_count", 1) != 1:
            continue
        for lift in key_lifts:
            if lift.lower() in r["exercise"].lower():
                ex_week.setdefault(lift, {})
                w = r["week"]
                if r["e1rm"] > ex_week[lift].get(w, 0):
                    ex_week[lift][w] = r["e1rm"]

    def _linear_rate(points: list[tuple[int, float]]) -> float | None:
        """Simple linear regression slope (y per x unit)."""
        if len(points) < 2:
            return None
        n = len(points)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            return None
        slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
        return round(slope, 2)

    result = {}
    for lift in key_lifts:
        week_map = ex_week.get(lift, {})
        all_weeks = sorted(week_map.keys())

        # Keep last history_weeks
        recent_weeks = all_weeks[-history_weeks:] if len(all_weeks) > history_weeks else all_weeks
        history = [(w, week_map[w]) for w in recent_weeks]

        current_e1rm = week_map[all_weeks[-1]] if all_weeks else None

        # Rate from last 4-6 data points
        rate_points = history[-6:] if len(history) >= 6 else history
        rate = _linear_rate(rate_points)

        result[lift] = {
            "history": history,
            "rate_kg_per_week": rate,
            "projection_weeks": None,  # filled in by caller with goal info
            "goal_kg": None,
            "current_e1rm": current_e1rm,
        }

    return result


# ---------------------------------------------------------------------------
# Body composition trends
# ---------------------------------------------------------------------------

def compute_body_comp_trends(health_data: list[dict], weeks: int = 12) -> dict:
    """
    Weekly body weight, fat %, visceral fat trends.

    health_data: from load_health_records() — sorted date desc, has body_weight_kg, body_fat_pct, visceral_fat_index.

    Returns:
    {
        "weight": {
            "history": [(date_str, kg), ...],   # sorted date asc, last N entries with data
            "rate_kg_per_week": float | None,   # positive = gaining, negative = losing
            "current": float | None,
            "flag": str | None,                 # e.g. "GAINING" | "LOSING" | "STABLE"
        },
        "body_fat_pct": {"history": [...], "current": float | None, "flag": str | None},
        "visceral_fat": {"history": [...], "current": float | None, "flag": str | None},
    }
    """
    from datetime import datetime, timedelta

    def _trend(points):
        """Points: list of (date_str, value). Returns rate per week."""
        if len(points) < 2:
            return None
        # Convert date to day offset from first
        try:
            t0 = datetime.fromisoformat(points[0][0])
            xs = [(datetime.fromisoformat(p[0]) - t0).days / 7.0 for p in points]
            ys = [p[1] for p in points]
        except Exception:
            return None
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            return None
        slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
        return round(slope, 3)

    def _flag_weight(rate):
        if rate is None:
            return None
        if rate > 0.3:
            return "GAINING_FAST"
        if rate > 0.05:
            return "GAINING"
        if rate < -0.3:
            return "LOSING_FAST"
        if rate < -0.05:
            return "LOSING"
        return "STABLE"

    # Sort ascending by date, filter last N weeks
    sorted_data = sorted(health_data, key=lambda r: r["date"])

    def _extract(field, max_entries=None):
        pts = [(r["date"], r[field]) for r in sorted_data if r.get(field) is not None]
        if max_entries:
            pts = pts[-max_entries:]
        return pts

    w_pts = _extract("body_weight_kg", 4 * weeks)
    f_pts = _extract("body_fat_pct", 4 * weeks)
    v_pts = _extract("visceral_fat_index", 4 * weeks)

    w_rate = _trend(w_pts)
    f_rate = _trend(f_pts)
    v_rate = _trend(v_pts)

    return {
        "weight": {
            "history":         w_pts,
            "rate_kg_per_week": w_rate,
            "current":         w_pts[-1][1] if w_pts else None,
            "flag":            _flag_weight(w_rate),
        },
        "body_fat_pct": {
            "history": f_pts,
            "rate_per_week": f_rate,
            "current": f_pts[-1][1] if f_pts else None,
        },
        "visceral_fat": {
            "history": v_pts,
            "rate_per_week": v_rate,
            "current": v_pts[-1][1] if v_pts else None,
        },
    }


# ---------------------------------------------------------------------------
# Health / recovery trends
# ---------------------------------------------------------------------------

def compute_health_trends(health_data: list[dict]) -> dict:
    """
    HRV, RHR, sleep: absolute values + vs 8-week personal baseline + trend direction.

    Returns:
    {
        "hrv": {
            "current": float | None,
            "baseline_8w": float | None,    # 8-week rolling mean
            "pct_vs_baseline": float | None, # (current - baseline) / baseline * 100
            "recent_7": [float, ...],        # last 7 days sorted oldest->newest
            "trend": "UP" | "DOWN" | "STABLE" | None,
            "flag": str | None,             # "SUPPRESSED" if >15% below baseline
        },
        "rhr": { same structure },
        "sleep": {
            "current": float | None,
            "recent_7": [float, ...],
            "avg_7d": float | None,
            "baseline_8w": float | None,
            "trend": str | None,
            "flag": str | None,  # "LOW" if avg < 6, "MARGINAL" if 6-7, "OK" if 7+
        },
    }
    """
    sorted_data = sorted(health_data, key=lambda r: r["date"])

    def _vals(field, n=None):
        pts = [r.get(field) for r in sorted_data if r.get(field) is not None]
        return pts[-n:] if n else pts

    def _avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else None

    def _trend_dir(vals, window=7):
        """Returns UP/DOWN/STABLE based on simple slope of last `window` points."""
        pts = vals[-window:]
        if len(pts) < 3:
            return None
        mid = len(pts) // 2
        first_half = _avg(pts[:mid])
        second_half = _avg(pts[mid:])
        if first_half is None or second_half is None:
            return None
        diff = second_half - first_half
        threshold = first_half * 0.03 if first_half else 0.5
        if diff > threshold:
            return "UP"
        if diff < -threshold:
            return "DOWN"
        return "STABLE"

    def _metric(field, flag_fn=None):
        all_vals = _vals(field)
        baseline_vals = all_vals[-56:] if len(all_vals) > 7 else all_vals  # ~8 weeks daily
        recent_7 = all_vals[-7:]
        current = all_vals[-1] if all_vals else None
        baseline = _avg(baseline_vals[:-7]) if len(baseline_vals) > 7 else _avg(baseline_vals)
        pct = round((current - baseline) / baseline * 100, 1) if (current and baseline and baseline != 0) else None
        trend = _trend_dir(all_vals)
        flag = flag_fn(current, baseline, pct, _avg(recent_7)) if flag_fn else None
        return {
            "current": current,
            "baseline_8w": baseline,
            "pct_vs_baseline": pct,
            "recent_7": recent_7,
            "avg_7d": _avg(recent_7),
            "trend": trend,
            "flag": flag,
        }

    def _hrv_flag(current, baseline, pct, avg7):
        if pct is not None and pct < -15:
            return "SUPPRESSED"
        if pct is not None and pct < -10:
            return "LOW"
        return None

    def _rhr_flag(current, baseline, pct, avg7):
        if current and current > 65:
            return "ELEVATED"
        return None

    def _sleep_flag(current, baseline, pct, avg7):
        if avg7 is None:
            return None
        if avg7 < 6.0:
            return "CRITICALLY_LOW"
        if avg7 < 7.0:
            return "MARGINAL"
        return "OK"

    return {
        "hrv":   _metric("hrv_ms", _hrv_flag),
        "rhr":   _metric("resting_hr", _rhr_flag),
        "sleep": _metric("sleep_hrs", _sleep_flag),
    }


# ---------------------------------------------------------------------------
# Training days per week
# ---------------------------------------------------------------------------

def compute_training_days(records: list[dict], weeks: int = 8) -> dict:
    """
    Count distinct training days per week (all sets, regardless of should_count).

    Returns:
    {
        "weeks": [w1, w2, ...],
        "days_per_week": [int, ...],   # distinct session dates per week
        "avg_days": float | None,      # average over the window
    }
    """
    from collections import defaultdict
    # Only real sessions (source='sheet') — bootstrap dates are approximate
    week_dates: dict[int, set] = defaultdict(set)
    for r in records:
        if r["done"] is not True or r["date"] is None or r.get("source", "sheet") != "sheet":
            continue
        week_dates[r["week"]].add(r["date"])

    if not week_dates:
        return {"weeks": [], "days_per_week": [], "avg_days": None}

    all_weeks = sorted(week_dates.keys())
    latest = all_weeks[-1]
    target_weeks = [w for w in all_weeks if w >= latest - weeks + 1]
    days_per_week = [len(week_dates[w]) for w in target_weeks]
    avg = round(sum(days_per_week) / len(days_per_week), 1) if days_per_week else None

    return {
        "weeks": target_weeks,
        "days_per_week": days_per_week,
        "avg_days": avg,
    }


# ---------------------------------------------------------------------------
# Combined runner
# ---------------------------------------------------------------------------

def run_all(
    records: list[dict],
    garmin_data: list[dict],
    progression: dict,
    goals: dict,
) -> dict:
    results = {}

    try:
        results["stalls"] = detect_stalls(records)
    except Exception as e:
        results["stalls"] = None
        results["stalls_error"] = str(e)

    try:
        results["volume"] = compute_volume_trends(records)
    except Exception as e:
        results["volume"] = None
        results["volume_error"] = str(e)

    try:
        results["load_index"] = compute_load_index(records)
    except Exception as e:
        results["load_index"] = None
        results["load_index_error"] = str(e)

    try:
        results["trajectory"] = compute_1rm_trajectory(records, progression, goals)
    except Exception as e:
        results["trajectory"] = None
        results["trajectory_error"] = str(e)

    try:
        results["strength_trends"] = compute_strength_trends(records)
    except Exception as e:
        results["strength_trends"] = None
        results["strength_trends_error"] = str(e)

    try:
        results["body_comp"] = compute_body_comp_trends(garmin_data)
    except Exception as e:
        results["body_comp"] = None
        results["body_comp_error"] = str(e)

    try:
        results["health_trends"] = compute_health_trends(garmin_data)
    except Exception as e:
        results["health_trends"] = None
        results["health_trends_error"] = str(e)

    try:
        results["adherence"] = compute_adherence(records)
    except Exception as e:
        results["adherence"] = None
        results["adherence_error"] = str(e)

    try:
        results["training_days"] = compute_training_days(records)
    except Exception as e:
        results["training_days"] = None
        results["training_days_error"] = str(e)

    try:
        results["sleep_correlation"] = compute_sleep_correlation(records, garmin_data)
    except Exception as e:
        results["sleep_correlation"] = None
        results["sleep_correlation_error"] = str(e)

    return results
