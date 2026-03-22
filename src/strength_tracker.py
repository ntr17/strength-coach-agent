"""
strength_tracker.py — Deterministic Strength Analytics Engine

Pure Python (no LLM). Runs weekly (Sunday) and writes STRENGTH_PROJECTIONS
to Coach State. The cascade levels read these computed facts instead of
estimating them from scratch.

What this file computes:
  e1RM          — Multi-formula estimation across all rep ranges (1-15),
                  AMRAP sets get highest confidence. Formula blend per rep range:
                  1-3:  Epley + Brzycki (high accuracy)
                  4-6:  Epley + Brzycki + Wathan (good accuracy)
                  7-10: Epley + Wathan + Mayhew (moderate — all included)
                  11-15: Wathan + Mayhew (low — flagged as estimate)
                  16+:  unreliable — excluded from projections
                  AMRAP: treated as max effort -> high-confidence signal
  RM table      — derives 2RM, 3RM, 5RM, 8RM, 10RM, 12RM from e1RM
  Weekly e1RM   — best accuracy estimate per exercise per ISO week
  Stall detection     — same e1RM ± 1.5% for N+ consecutive weeks
  Regression detection — e1RM going down vs prior moving average
  Overload compliance — did actual weight increase when it should have?
  Rep bucket volume   — weekly sets partitioned into 1-5 / 6-12 / 12+ per motion group
  Volume balance      — push : pull ratio, flag imbalances
  Goal proximity      — how many kg away from stated targets
  Strength report     — combined output -> STRENGTH_PROJECTIONS domain

Entry point: run_weekly_strength_report(lift_history, program_data, goals)
"""

import json
import math
import re
import statistics as stats_lib
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# Rep bucket definitions
# ---------------------------------------------------------------------------

REP_BUCKET_STRENGTH    = (1, 5)    # Strength: 1-5 reps/set
REP_BUCKET_HYPERTROPHY = (6, 12)   # Hypertrophy: 6-12 reps/set
REP_BUCKET_ENDURANCE   = (13, 999) # Endurance: 13+ reps/set

BUCKET_LABELS = {
    "strength":    "1-5 reps (strength)",
    "hypertrophy": "6-12 reps (hypertrophy)",
    "endurance":   "13+ reps (endurance)",
}

# ---------------------------------------------------------------------------
# Motion group mapping — nuanced, with correlation tiers
# ---------------------------------------------------------------------------

# Primary motion group assignment for exercise names (lowercase match)
MOTION_GROUPS = {
    # Push
    "bench":              "push",
    "incline bench":      "push",
    "incline press":      "push",
    "overhead press":     "push",
    "ohp":                "push",
    "military press":     "push",
    "push press":         "push",
    "dips":               "push",
    "weighted dips":      "push",
    "dumbbell bench":     "push",
    "db bench":           "push",
    "chest press":        "push",
    # Pull
    "row":                "pull",
    "barbell row":        "pull",
    "bent over row":      "pull",
    "cable row":          "pull",
    "seated row":         "pull",
    "dumbbell row":       "pull",
    "pullup":             "pull",
    "pull-up":            "pull",
    "chin-up":            "pull",
    "chinup":             "pull",
    "lat pulldown":       "pull",
    "pulldown":           "pull",
    "face pull":          "pull",
    # Squat/legs
    "squat":              "squat",
    "front squat":        "squat",
    "goblet squat":       "squat",
    "leg press":          "squat",
    "box squat":          "squat",
    "pause squat":        "squat",
    # Hip hinge
    "deadlift":           "hinge",
    "rdl":                "hinge",
    "romanian deadlift":  "hinge",
    "good morning":       "hinge",
    "hip thrust":         "hinge",
    "glute bridge":       "hinge",
    # Accessory
    "curl":               "arm_accessory",
    "bicep curl":         "arm_accessory",
    "hammer curl":        "arm_accessory",
    "tricep":             "arm_accessory",
    "extension":          "arm_accessory",
    "pushdown":           "arm_accessory",
    # Core
    "plank":              "core",
    "crunch":             "core",
    "ab":                 "core",
    "nordic":             "core",
    "lunges":             "squat",
    "lunge":              "squat",
    "bulgarian":          "squat",
    "split squat":        "squat",
}


def _classify_motion_group(exercise_name: str) -> str:
    """Return motion group for exercise name. Default 'other'."""
    n = exercise_name.lower().strip()
    # Exact match or substring match — longer keys first to avoid partial matches
    for key in sorted(MOTION_GROUPS.keys(), key=len, reverse=True):
        if key in n:
            return MOTION_GROUPS[key]
    return "other"


# ---------------------------------------------------------------------------
# Rep accuracy tiers — how reliable is an e1RM estimate at each rep range?
# ---------------------------------------------------------------------------

# Maps rep count to (accuracy_label, confidence_weight 0-1)
# Higher reps = more formula error, lower confidence weight in projections
REP_ACCURACY: dict[tuple, tuple] = {
    (1, 3):   ("high",     1.00),
    (4, 6):   ("good",     0.90),
    (7, 10):  ("moderate", 0.72),
    (11, 15): ("low",      0.50),
    (16, 999):("unreliable", 0.0),  # excluded from projections
}

# RM conversion table: what fraction of 1RM can you lift for N reps?
# Based on Epley formula inverse — used for 2RM, 3RM, 5RM, 8RM, 10RM projections
RM_FRACTIONS: dict[int, float] = {
    1:  1.000,
    2:  0.970,
    3:  0.940,
    4:  0.910,
    5:  0.870,
    6:  0.850,
    7:  0.830,
    8:  0.800,
    10: 0.750,
    12: 0.700,
    15: 0.650,
    20: 0.580,
}

# AMRAP keywords — if any of these appear in notes/sets_reps, treat reps as max effort
AMRAP_KEYWORDS = ("amrap", "max reps", "to failure", "all out", "max effort", "myo", "+ reps")


# ---------------------------------------------------------------------------
# Core e1RM computation — multi-formula, rep-range aware
# ---------------------------------------------------------------------------

def _e1rm_epley(weight: float, reps: int) -> float:
    """Epley: w × (1 + r/30). Accurate 1-10 reps."""
    return weight * (1 + reps / 30)


def _e1rm_brzycki(weight: float, reps: int) -> Optional[float]:
    """Brzycki: w × 36 / (37 - r). Accurate 1-10, breaks at r >= 37."""
    if reps >= 37:
        return None
    return weight * 36 / (37 - reps)


def _e1rm_wathan(weight: float, reps: int) -> float:
    """Wathan: 100w / (48.8 + 53.8 × e^(-0.075r)). Validated wider range."""
    return 100 * weight / (48.8 + 53.8 * math.exp(-0.075 * reps))


def _e1rm_mayhew(weight: float, reps: int) -> float:
    """Mayhew: 100w / (52.2 + 41.9 × e^(-0.055r)). Better for higher reps (8-20)."""
    return 100 * weight / (52.2 + 41.9 * math.exp(-0.055 * reps))


def compute_e1rm_multi(weight_kg: float, reps: int,
                        is_amrap: bool = False) -> Optional[dict]:
    """
    Estimate e1RM using multiple formulas weighted by rep-range accuracy.

    Returns dict:
    {
        "e1rm": float,          # blended estimate
        "e1rm_low": float,      # conservative bound (min of applicable formulas)
        "e1rm_high": float,     # aggressive bound (max of applicable formulas)
        "accuracy": str,        # "high" / "good" / "moderate" / "low" / "unreliable"
        "confidence": float,    # 0.0-1.0
        "formula_note": str,    # which formulas were used
        "is_amrap": bool,
    }

    AMRAP sets are treated as max-effort (full rep range = true capability signal).
    Their accuracy is bumped up one tier because the athlete went to near-failure.

    Returns None if reps < 1 or weight <= 0.
    """
    if reps < 1 or weight_kg <= 0:
        return None

    # Determine base accuracy tier
    accuracy_label = "unreliable"
    confidence = 0.0
    for (lo, hi), (label, conf) in REP_ACCURACY.items():
        if lo <= reps <= hi:
            accuracy_label = label
            confidence = conf
            break

    # AMRAP bump: max-effort set is more informative than a submaximal set at same reps
    # e.g., AMRAP of 8 reps > prescribed 3x8 (the 8 represents true ceiling)
    if is_amrap and accuracy_label in ("moderate", "low"):
        prev_tiers = ["high", "good", "moderate", "low", "unreliable"]
        idx = prev_tiers.index(accuracy_label)
        accuracy_label = prev_tiers[max(0, idx - 1)]
        confidence = min(1.0, confidence + 0.20)

    # Exclude unreliable
    if accuracy_label == "unreliable":
        return None

    # Select formulas by rep range
    estimates: list[float] = []
    notes: list[str] = []

    epley = _e1rm_epley(weight_kg, reps)
    estimates.append(epley)
    notes.append("Epley")

    if reps <= 10:
        brz = _e1rm_brzycki(weight_kg, reps)
        if brz:
            estimates.append(brz)
            notes.append("Brzycki")

    if reps >= 4:
        wat = _e1rm_wathan(weight_kg, reps)
        estimates.append(wat)
        notes.append("Wathan")

    if reps >= 7:
        may = _e1rm_mayhew(weight_kg, reps)
        estimates.append(may)
        notes.append("Mayhew")

    blended = sum(estimates) / len(estimates)

    return {
        "e1rm": round(blended, 1),
        "e1rm_low": round(min(estimates), 1),
        "e1rm_high": round(max(estimates), 1),
        "accuracy": accuracy_label,
        "confidence": round(confidence, 2),
        "formula_note": "+".join(notes),
        "is_amrap": is_amrap,
        "source_weight": weight_kg,
        "source_reps": reps,
    }


def compute_e1rm(weight_kg: float, reps: int) -> Optional[float]:
    """
    Simple e1RM estimate — backward-compatible wrapper around compute_e1rm_multi.
    Returns blended estimate or None if unreliable (reps > 15).
    """
    result = compute_e1rm_multi(weight_kg, reps)
    return result["e1rm"] if result else None


# ---------------------------------------------------------------------------
# RM table from e1RM
# ---------------------------------------------------------------------------

def compute_rm_table(e1rm: float, rounding: float = 2.5) -> dict:
    """
    Derive the full RM table from an estimated 1RM.
    Returns realistic targets for 2RM through 20RM.

    rounding: round to nearest value (2.5 = standard plate increment).
    """
    def _round(val: float) -> float:
        return round(round(val / rounding) * rounding, 1)

    return {
        f"{rm}RM": _round(e1rm * frac)
        for rm, frac in RM_FRACTIONS.items()
    }


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _extract_weight(text: str) -> Optional[float]:
    """Extract first numeric weight value from text."""
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(text))
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _detect_amrap(entry: dict) -> bool:
    """Return True if the entry represents an AMRAP / max-effort set."""
    combined = " ".join([
        str(entry.get("Notes") or ""),
        str(entry.get("notes") or ""),
        str(entry.get("Sets x Reps") or ""),
        str(entry.get("sets_reps") or ""),
        str(entry.get("Actual Weight/Reps") or ""),
    ]).lower()
    return any(kw in combined for kw in AMRAP_KEYWORDS)


def _parse_sets_reps_from_entry(entry: dict) -> tuple[int, int]:
    """
    Parse (sets, reps) from a lift history entry.
    Checks sets_reps, actual, notes fields. Returns (0, 0) if unparseable.
    For AMRAP fields like "1xAMRAP" or "AMRAP", reps extracted from context.
    """
    for field in ("Sets x Reps", "sets_reps", "Actual Weight/Reps", "actual", "Notes", "notes"):
        val = entry.get(field) or ""
        if not val:
            continue
        val_str = str(val)
        # "4x5" or "4 x 5" format (standard)
        m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", val_str)
        if m:
            return int(m.group(1)), int(m.group(2))
        # "AMRAP: 12" or "did 12" in notes — single number for AMRAP reps
        if any(kw in val_str.lower() for kw in AMRAP_KEYWORDS):
            m2 = re.search(r"(\d+)", val_str)
            if m2:
                n = int(m2.group(1))
                if 1 <= n <= 50:  # sanity check
                    return 1, n   # treat as 1 set of N reps
    return 0, 0


def _get_week_key(date_str: str) -> Optional[str]:
    """Convert date string to ISO week key 'YYYY-WNN'."""
    try:
        d = date.fromisoformat(str(date_str)[:10])
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    except (ValueError, TypeError, AttributeError):
        return None


def _get_exercise_name(entry: dict) -> str:
    """Extract and lowercase exercise name from entry."""
    name = (
        entry.get("Exercise") or entry.get("exercise") or
        entry.get("Lift") or entry.get("lift") or ""
    )
    return name.lower().strip()


# ---------------------------------------------------------------------------
# Weekly e1RM per exercise
# ---------------------------------------------------------------------------

def compute_weekly_e1rm(lift_history: list) -> dict:
    """
    Compute weekly best e1RM per exercise from lift history.

    Uses all sets with 1-15 reps (not just ≤6). Selects the highest-confidence
    estimate each week. AMRAP sets are treated as max-effort and get a confidence
    boost. Sets >15 reps are excluded as too unreliable for strength projection.

    Returns:
    {
        exercise_name: {
            week_key: {
                "e1rm": float,         # blended estimate
                "e1rm_low": float,     # conservative bound
                "e1rm_high": float,    # aggressive bound
                "accuracy": str,       # "high"/"good"/"moderate"/"low"
                "confidence": float,   # 0-1
                "weight": float,       # source weight
                "reps": int,           # source reps
                "is_amrap": bool,
                "rm_table": dict,      # 2RM, 3RM, 5RM, 8RM, 10RM, 12RM
            }
        }
    }
    """
    result: dict = {}

    for entry in lift_history:
        exercise = _get_exercise_name(entry)
        if not exercise:
            continue

        date_str = entry.get("Date") or entry.get("date") or ""
        week_key = _get_week_key(date_str)
        if not week_key:
            continue

        # Try actual weight first, fall back to prescribed
        actual_raw = entry.get("Actual Weight/Reps") or entry.get("actual") or ""
        weight_raw = entry.get("Weight") or entry.get("weight") or ""
        weight = _extract_weight(str(actual_raw)) or _extract_weight(str(weight_raw))
        if not weight:
            continue

        sets, reps = _parse_sets_reps_from_entry(entry)
        if reps == 0:
            sr_field = entry.get("Sets x Reps") or entry.get("sets_reps") or ""
            m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", str(sr_field))
            if m:
                sets, reps = int(m.group(1)), int(m.group(2))

        if reps < 1:
            continue

        is_amrap = _detect_amrap(entry)
        estimate = compute_e1rm_multi(weight, reps, is_amrap=is_amrap)
        if estimate is None:
            continue  # reps > 15 or unreliable

        result.setdefault(exercise, {})

        # Replace if this estimate has higher confidence, or same confidence + higher e1rm
        existing = result[exercise].get(week_key)
        if (existing is None
                or estimate["confidence"] > existing["confidence"]
                or (estimate["confidence"] == existing["confidence"]
                    and estimate["e1rm"] > existing["e1rm"])):
            result[exercise][week_key] = {
                **estimate,
                "weight": weight,
                "reps": reps,
                "rm_table": compute_rm_table(estimate["e1rm"]),
            }

    return result


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------

def detect_stalls(weekly_e1rm: dict, consecutive_weeks: int = 3,
                  tolerance_pct: float = 1.5) -> list:
    """
    Detect exercises that have stalled (no meaningful e1RM progress).
    A stall = e1RM changed by less than tolerance_pct% over consecutive_weeks.

    Returns list of dicts: [{exercise, weeks_stalled, current_e1rm, stall_since}]
    """
    stalls = []

    for exercise, weekly in weekly_e1rm.items():
        sorted_weeks = sorted(weekly.keys())
        if len(sorted_weeks) < consecutive_weeks:
            continue

        recent = sorted_weeks[-consecutive_weeks:]
        values = [weekly[wk]["e1rm"] for wk in recent]

        if len(values) < consecutive_weeks:
            continue

        # Check if all values are within tolerance_pct% of each other
        min_val = min(values)
        max_val = max(values)
        if min_val > 0 and (max_val - min_val) / min_val * 100 <= tolerance_pct:
            stalls.append({
                "exercise": exercise,
                "weeks_stalled": consecutive_weeks,
                "current_e1rm": round(values[-1], 1),
                "stall_since": recent[0],
                "range_pct": round((max_val - min_val) / min_val * 100, 1),
            })

    return stalls


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------

def detect_regressions(weekly_e1rm: dict, lookback_weeks: int = 4,
                        threshold_pct: float = 3.0) -> list:
    """
    Detect exercises where e1RM has dropped vs prior average.
    Regression = current week e1RM is > threshold_pct% below prior lookback_weeks average.

    Returns list of dicts: [{exercise, current_e1rm, prior_avg, pct_drop}]
    """
    regressions = []

    for exercise, weekly in weekly_e1rm.items():
        sorted_weeks = sorted(weekly.keys())
        if len(sorted_weeks) < lookback_weeks + 1:
            continue

        prior_weeks = sorted_weeks[-(lookback_weeks + 1):-1]
        current_week = sorted_weeks[-1]

        prior_values = [weekly[wk]["e1rm"] for wk in prior_weeks]
        current_val = weekly[current_week]["e1rm"]

        if not prior_values:
            continue

        prior_avg = stats_lib.mean(prior_values)
        if prior_avg > 0:
            pct_change = (current_val - prior_avg) / prior_avg * 100
            if pct_change < -threshold_pct:
                regressions.append({
                    "exercise": exercise,
                    "current_e1rm": round(current_val, 1),
                    "prior_avg_e1rm": round(prior_avg, 1),
                    "pct_drop": round(abs(pct_change), 1),
                    "current_week": current_week,
                })

    return regressions


# ---------------------------------------------------------------------------
# Rep bucket volume
# ---------------------------------------------------------------------------

def compute_volume_by_rep_bucket(lift_history: list) -> dict:
    """
    Partition weekly training volume into rep buckets per motion group.

    Volume = sets × reps × weight (kg) — tonnage per bucket.

    Returns:
    {
        week_key: {
            motion_group: {
                "strength":    {"sets": N, "tonnage": float},
                "hypertrophy": {"sets": N, "tonnage": float},
                "endurance":   {"sets": N, "tonnage": float},
            }
        }
    }
    """
    result: dict = {}

    for entry in lift_history:
        exercise = _get_exercise_name(entry)
        date_str = entry.get("Date") or entry.get("date") or ""
        week_key = _get_week_key(date_str)
        if not week_key or not exercise:
            continue

        # Weight
        actual_raw = entry.get("Actual Weight/Reps") or entry.get("actual") or ""
        weight_raw = entry.get("Weight") or entry.get("weight") or ""
        weight = _extract_weight(str(actual_raw)) or _extract_weight(str(weight_raw))
        if not weight:
            continue

        # Sets and reps
        sets, reps = _parse_sets_reps_from_entry(entry)
        if sets == 0:
            sr_field = entry.get("Sets x Reps") or entry.get("sets_reps") or ""
            m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", str(sr_field))
            if m:
                sets, reps = int(m.group(1)), int(m.group(2))
        if sets == 0 or reps == 0:
            continue

        tonnage = sets * reps * weight
        motion = _classify_motion_group(exercise)

        # Classify bucket
        if REP_BUCKET_STRENGTH[0] <= reps <= REP_BUCKET_STRENGTH[1]:
            bucket = "strength"
        elif REP_BUCKET_HYPERTROPHY[0] <= reps <= REP_BUCKET_HYPERTROPHY[1]:
            bucket = "hypertrophy"
        else:
            bucket = "endurance"

        result.setdefault(week_key, {})
        result[week_key].setdefault(motion, {})
        result[week_key][motion].setdefault(bucket, {"sets": 0, "tonnage": 0.0})
        result[week_key][motion][bucket]["sets"] += sets
        result[week_key][motion][bucket]["tonnage"] += tonnage

    return result


# ---------------------------------------------------------------------------
# Push:Pull volume balance
# ---------------------------------------------------------------------------

def compute_push_pull_balance(volume_by_bucket: dict) -> list:
    """
    Compute push:pull ratio per week from bucketed volume.
    Industry standard: 1:1 to 1:1.3 (slightly more pull) is healthy.
    Flags weeks with ratio > 1.5:1 (push dominant) or > 1.5:1 pull dominant as imbalanced.

    Returns list of dicts per week:
    [{week, push_tonnage, pull_tonnage, ratio, flag, note}]
    """
    results = []

    for week_key in sorted(volume_by_bucket.keys()):
        week_data = volume_by_bucket[week_key]

        push_tonnage = sum(
            week_data.get("push", {}).get(b, {}).get("tonnage", 0)
            for b in ("strength", "hypertrophy", "endurance")
        )
        pull_tonnage = sum(
            week_data.get("pull", {}).get(b, {}).get("tonnage", 0)
            for b in ("strength", "hypertrophy", "endurance")
        )

        if push_tonnage == 0 and pull_tonnage == 0:
            continue

        if pull_tonnage > 0:
            ratio = round(push_tonnage / pull_tonnage, 2)
        else:
            ratio = None

        flag = None
        note = ""
        if ratio is not None:
            if ratio > 1.5:
                flag = "push_dominant"
                note = f"Push tonnage {ratio:.1f}x pull this week. Add row/pull volume to protect shoulder health."
            elif ratio < 0.6:
                flag = "pull_dominant"
                note = f"Pull tonnage dominates ({1/ratio:.1f}x push). Check if chest/shoulder work is being skipped."
            else:
                flag = "balanced"
                note = f"Push:pull ratio {ratio:.2f} — within healthy range (0.7-1.4)."

        results.append({
            "week": week_key,
            "push_tonnage": round(push_tonnage, 0),
            "pull_tonnage": round(pull_tonnage, 0),
            "ratio": ratio,
            "flag": flag,
            "note": note,
        })

    return results


# ---------------------------------------------------------------------------
# Overload compliance
# ---------------------------------------------------------------------------

def compute_overload_compliance(lift_history: list) -> dict:
    """
    Check whether weight increased week-over-week for each exercise.
    Overload compliance = did the athlete lift heavier this week than last week?

    Returns:
    {
        exercise: {
            "compliant_weeks": N,
            "total_weeks": N,
            "compliance_rate": 0.0-1.0,
            "missed_increases": [{week, expected_increase, actual_change}]
        }
    }
    """
    # Build weekly max weight per exercise
    weekly_max: dict = {}
    for entry in lift_history:
        exercise = _get_exercise_name(entry)
        date_str = entry.get("Date") or entry.get("date") or ""
        week_key = _get_week_key(date_str)
        if not exercise or not week_key:
            continue

        actual_raw = entry.get("Actual Weight/Reps") or entry.get("actual") or ""
        weight_raw = entry.get("Weight") or entry.get("weight") or ""
        weight = _extract_weight(str(actual_raw)) or _extract_weight(str(weight_raw))
        if not weight:
            continue

        weekly_max.setdefault(exercise, {})
        weekly_max[exercise][week_key] = max(weekly_max[exercise].get(week_key, 0.0), weight)

    result: dict = {}
    for exercise, weekly in weekly_max.items():
        sorted_weeks = sorted(weekly.keys())
        if len(sorted_weeks) < 2:
            continue

        compliant = 0
        missed: list = []

        for i in range(1, len(sorted_weeks)):
            prev_wk = sorted_weeks[i - 1]
            curr_wk = sorted_weeks[i]
            prev_w = weekly[prev_wk]
            curr_w = weekly[curr_wk]
            change = curr_w - prev_w

            if change >= 0:
                compliant += 1
            else:
                missed.append({
                    "week": curr_wk,
                    "prev_weight": prev_w,
                    "actual_weight": curr_w,
                    "change_kg": round(change, 1),
                })

        total = len(sorted_weeks) - 1
        result[exercise] = {
            "compliant_weeks": compliant,
            "total_weeks": total,
            "compliance_rate": round(compliant / total, 3) if total > 0 else 0.0,
            "missed_increases": missed[-3:],  # last 3 missed only
        }

    return result


# ---------------------------------------------------------------------------
# Goal proximity
# ---------------------------------------------------------------------------

def compute_goal_proximity(weekly_e1rm: dict, goals: dict) -> list:
    """
    Compute how close each lift is to its goal e1RM.

    goals: {"squat": 120.0, "bench press": 105.0} (lowercase keys)

    Returns list of dicts: [{exercise, current_e1rm, goal, gap_kg, pct_remaining, status}]
    """
    results = []

    for exercise, weekly in weekly_e1rm.items():
        sorted_weeks = sorted(weekly.keys())
        if not sorted_weeks:
            continue

        current_e1rm = weekly[sorted_weeks[-1]]["e1rm"]

        # Find matching goal
        goal = None
        for goal_key, goal_val in goals.items():
            if goal_key in exercise or exercise in goal_key:
                goal = goal_val
                break

        if goal is None:
            continue

        gap = round(goal - current_e1rm, 1)
        pct_remaining = round(gap / goal * 100, 1) if goal > 0 else 0.0

        if gap <= 0:
            status = "achieved"
        elif gap <= 5.0:
            status = "close"
        elif pct_remaining <= 10.0:
            status = "on_track"
        else:
            status = "in_progress"

        results.append({
            "exercise": exercise,
            "current_e1rm": round(current_e1rm, 1),
            "goal": goal,
            "gap_kg": gap,
            "pct_remaining": pct_remaining,
            "status": status,
        })

    return results


# ---------------------------------------------------------------------------
# Overload timing prediction — when should the next weight increase happen?
# ---------------------------------------------------------------------------

def predict_next_increase(weekly_e1rm: dict) -> dict:
    """
    Based on recent e1RM trend, predict when each lift should next increase weight.
    Uses rate of gain to compute expected week of next meaningful jump.

    Returns: {exercise: {"expected_increase_week": week_key, "expected_gain_kg": float}}
    """
    results: dict = {}

    for exercise, weekly in weekly_e1rm.items():
        sorted_weeks = sorted(weekly.keys())
        if len(sorted_weeks) < 3:
            continue

        # Linear regression on recent e1RM
        recent = sorted_weeks[-6:]  # last 6 weeks
        values = [weekly[wk]["e1rm"] for wk in recent]
        xs = list(range(len(values)))

        if len(xs) < 2:
            continue

        n = len(xs)
        sum_x = sum(xs)
        sum_y = sum(values)
        sum_xx = sum(x * x for x in xs)
        sum_xy = sum(x * y for x, y in zip(xs, values))
        denom = n * sum_xx - sum_x * sum_x
        if denom == 0:
            continue

        slope = (n * sum_xy - sum_x * sum_y) / denom  # e1RM gain per week

        if slope <= 0:
            continue

        # A meaningful increase = 2.5kg (smallest weight plate increment)
        weeks_to_increase = round(2.5 / slope, 1)

        results[exercise] = {
            "rate_per_week": round(slope, 2),
            "weeks_to_next_increase": weeks_to_increase,
            "current_e1rm": round(values[-1], 1),
        }

    return results


# ---------------------------------------------------------------------------
# Weekly volume summary per rep bucket and motion group
# ---------------------------------------------------------------------------

def summarize_volume_buckets(volume_by_bucket: dict, last_n_weeks: int = 4) -> dict:
    """
    Summarize volume by rep bucket and motion group for the last N weeks.
    Returns averages and trends.

    Returns:
    {
        motion_group: {
            bucket: {
                "avg_sets_per_week": float,
                "avg_tonnage_per_week": float,
                "trend": "increasing"|"stable"|"decreasing",
            }
        }
    }
    """
    sorted_weeks = sorted(volume_by_bucket.keys())[-last_n_weeks:]
    if not sorted_weeks:
        return {}

    # Accumulate per motion group per bucket
    agg: dict = {}
    for wk in sorted_weeks:
        for motion, buckets in volume_by_bucket.get(wk, {}).items():
            agg.setdefault(motion, {})
            for bucket, data in buckets.items():
                agg[motion].setdefault(bucket, {"sets_history": [], "tonnage_history": []})
                agg[motion][bucket]["sets_history"].append(data.get("sets", 0))
                agg[motion][bucket]["tonnage_history"].append(data.get("tonnage", 0.0))

    results: dict = {}
    for motion, buckets in agg.items():
        results[motion] = {}
        for bucket, data in buckets.items():
            sets_hist = data["sets_history"]
            tonnage_hist = data["tonnage_history"]
            avg_sets = stats_lib.mean(sets_hist) if sets_hist else 0.0
            avg_tonnage = stats_lib.mean(tonnage_hist) if tonnage_hist else 0.0

            # Trend: compare last half vs first half
            if len(sets_hist) >= 3:
                mid = len(sets_hist) // 2
                first_half = stats_lib.mean(sets_hist[:mid])
                second_half = stats_lib.mean(sets_hist[mid:])
                if first_half > 0:
                    change = (second_half - first_half) / first_half
                    if change > 0.1:
                        trend = "increasing"
                    elif change < -0.1:
                        trend = "decreasing"
                    else:
                        trend = "stable"
                else:
                    trend = "stable"
            else:
                trend = "insufficient_data"

            results[motion][bucket] = {
                "avg_sets_per_week": round(avg_sets, 1),
                "avg_tonnage_per_week": round(avg_tonnage, 0),
                "trend": trend,
            }

    return results


# ---------------------------------------------------------------------------
# Stall/regression coach insight text generator
# ---------------------------------------------------------------------------

def generate_stall_insights(stalls: list, regressions: list) -> list:
    """
    Convert stall and regression detections into coach-readable insight strings.
    """
    insights = []

    for s in stalls:
        insights.append(
            f"STALL — {s['exercise']}: e1RM stuck at ~{s['current_e1rm']}kg "
            f"for {s['weeks_stalled']} consecutive weeks (since {s['stall_since']}). "
            f"Variation needed: deload, rep scheme change, or technique cue."
        )

    for r in regressions:
        insights.append(
            f"REGRESSION — {r['exercise']}: current e1RM {r['current_e1rm']}kg "
            f"is {r['pct_drop']:.1f}% below prior {r['prior_avg_e1rm']}kg average. "
            f"Possible causes: fatigue accumulation, schedule disruption, or technique drift."
        )

    return insights


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_weekly_strength_report(
    lift_history: list,
    goals: dict = None,
    dry_run: bool = False,
) -> dict:
    """
    Run all strength analytics. Called Sunday after weekly_eval().
    Writes STRENGTH_PROJECTIONS to Coach State.

    goals: {exercise_name_lower: target_e1rm_kg}  e.g. {"squat": 120.0, "bench": 105.0}

    Returns the full report dict.
    """
    if goals is None:
        goals = {}

    today = str(date.today())

    # Compute all analytics
    weekly_e1rm = compute_weekly_e1rm(lift_history)
    stalls = detect_stalls(weekly_e1rm, consecutive_weeks=3)
    regressions = detect_regressions(weekly_e1rm)
    overload = compute_overload_compliance(lift_history)
    volume_buckets = compute_volume_by_rep_bucket(lift_history)
    push_pull = compute_push_pull_balance(volume_buckets)
    goal_proximity = compute_goal_proximity(weekly_e1rm, goals)
    next_increase = predict_next_increase(weekly_e1rm)
    volume_summary = summarize_volume_buckets(volume_buckets, last_n_weeks=4)
    stall_insights = generate_stall_insights(stalls, regressions)

    # Current e1RM snapshot (latest week per lift) — with RM table and accuracy
    current_snapshot: dict = {}
    for exercise, weekly in weekly_e1rm.items():
        sorted_weeks = sorted(weekly.keys())
        if sorted_weeks:
            latest = weekly[sorted_weeks[-1]]
            current_snapshot[exercise] = {
                "e1rm": latest["e1rm"],
                "e1rm_low": latest.get("e1rm_low"),
                "e1rm_high": latest.get("e1rm_high"),
                "accuracy": latest.get("accuracy", "unknown"),
                "confidence": latest.get("confidence", 0.0),
                "week": sorted_weeks[-1],
                "rm_table": latest.get("rm_table", {}),
            }

    report = {
        "computed_date": today,
        "current_e1rm_snapshot": current_snapshot,
        "stalls": stalls,
        "regressions": regressions,
        "overload_compliance": overload,
        "volume_by_rep_bucket": volume_buckets,
        "push_pull_balance": push_pull,
        "goal_proximity": goal_proximity,
        "next_increase_predictions": next_increase,
        "volume_summary": volume_summary,
        "stall_insights": stall_insights,
    }

    if not dry_run:
        try:
            from memory import upsert_coach_state
            upsert_coach_state(
                "STRENGTH_PROJECTIONS",
                json.dumps(report, ensure_ascii=False, default=str),
                "HIGH",
            )
            stall_count = len(stalls)
            regression_count = len(regressions)
            balance_flags = [w for w in push_pull if w.get("flag") != "balanced"]
            print(
                f"  strength_tracker: report written — "
                f"{stall_count} stall(s), {regression_count} regression(s), "
                f"{len(balance_flags)} balance flag(s)."
            )
        except Exception as e:
            print(f"  strength_tracker: STRENGTH_PROJECTIONS write failed: {e}")

    return report


# ---------------------------------------------------------------------------
# Format for prompt injection
# ---------------------------------------------------------------------------

def format_strength_report_for_prompt(report: dict) -> str:
    """
    Compact text block summarizing strength tracker output for LLM prompt injection.
    Includes e1RM estimates, RM table for main lifts, accuracy tags, and alerts.
    """
    if not report:
        return ""

    lines = []

    # Current e1RM snapshot with RM table (main lifts only — squat, bench, deadlift, OHP)
    snapshot = report.get("current_e1rm_snapshot", {})
    main_lifts = [ex for ex in snapshot if any(
        k in ex for k in ("squat", "bench", "deadlift", "overhead", "ohp")
    )]
    if main_lifts:
        lines.append("Current strength estimates (e1RM):")
        for ex in sorted(main_lifts):
            s = snapshot[ex]
            acc = s.get("accuracy", "?")
            conf_note = f" [{acc} accuracy]" if acc not in ("high", "good") else ""
            e1rm = s["e1rm"]
            # Key RM targets from table
            rm_table = s.get("rm_table", {})
            rm_parts = []
            for rm_key in ("5RM", "3RM", "8RM", "10RM"):
                if rm_key in rm_table:
                    rm_parts.append(f"{rm_key}: {rm_table[rm_key]}kg")
            rm_str = " | ".join(rm_parts[:3])  # show 3 most useful
            lines.append(
                f"  {ex}: {e1rm}kg e1RM{conf_note}"
                + (f" -> {rm_str}" if rm_str else "")
            )

    # Goal proximity
    if report.get("goal_proximity"):
        lines.append("Goal proximity:")
        for gp in report["goal_proximity"]:
            status_icon = {
                "achieved": "ACHIEVED",
                "close": "CLOSE (<5kg away)",
                "on_track": "on track",
                "in_progress": "in progress",
            }.get(gp["status"], gp["status"])
            lines.append(
                f"  {gp['exercise']}: {gp['current_e1rm']}kg / {gp['goal']}kg goal "
                f"({gp['gap_kg']:+.1f}kg) - {status_icon}"
            )

    # Stalls and regressions
    if report.get("stall_insights"):
        lines.append("Strength signals:")
        for insight in report["stall_insights"]:
            lines.append(f"  {insight}")

    # Push/pull balance flags
    if report.get("push_pull_balance"):
        flagged = [w for w in report["push_pull_balance"] if w.get("flag") not in ("balanced", None)]
        if flagged:
            lines.append("Volume balance alerts:")
            for w in flagged[-2:]:
                lines.append(f"  {w['week']}: {w['note']}")

    # Next increase predictions
    if report.get("next_increase_predictions"):
        lines.append("Predicted next weight increases:")
        for ex, pred in report["next_increase_predictions"].items():
            wks = pred.get("weeks_to_next_increase", "?")
            rate = pred.get("rate_per_week", 0)
            lines.append(
                f"  {ex}: +{rate:.2f}kg/wk -> next 2.5kg increase in ~{wks} weeks"
            )

    return "\n".join(lines) if lines else ""
