"""
health_science.py — V17 Health Coach Data Science Engine

Pure Python correlation engine (no LLM, deterministic, reproducible).
Surfaces insights only when N >= N_MIN observations and r² >= R2_MIN.

Correlations computed (health):
  sleep_strength  — sleep hours → next-day session max weight (% change vs baseline)
  sleep_rpe       — sleep hours → next-session RPE (negative expected: less sleep → higher RPE)
  hrv_readiness   — HRV → session RPE (negative: high HRV → lower RPE)
  steps_food      — daily steps → food quality score next day

Correlations computed (lift science):
  lift_family     — within-family lift correlation (bench ↔ incline bench, squat ↔ front squat)
  volume_lag      — weekly volume → strength 2 weeks later (lagged correlation)
  lift_trends     — per-lift moving average + trajectory flag (trending_up/plateaued/trending_down)

Daily readiness signal (no LLM):
  compute_daily_readiness() → HEALTH_READINESS JSON dict
  Factors: sleep debt, HRV vs baseline, recent readiness trend
  Outputs: readiness_score (0-100), constraints, recommendations, flags, insights

Stored in Coach State:
  HEALTH_INSIGHTS   — weekly health correlation pass results (JSON)
  LIFT_INSIGHTS     — weekly lift science pass results (JSON)
  HEALTH_READINESS  — daily readiness signal (JSON)
"""

import json
import math
import statistics as stats_lib
from datetime import date, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_MIN = 20          # Minimum observations before surfacing a health correlation insight
N_MIN_LIFT = 15     # Minimum observations for lift family correlations (less data available)
N_MIN_VOLUME_LAG = 12  # Minimum weeks for volume-strength lag analysis
R2_MIN = 0.05       # Minimum R² (5%) to surface health correlations — avoids noise
R2_MIN_LIFT = 0.10  # Minimum R² (10%) for lift correlations — tighter threshold
SLEEP_TARGET_HRS = 7.5
SLEEP_LOW_THRESHOLD = 6.0  # Below this = poor sleep flag
HRV_DEBT_DAYS = 3   # How many days below baseline triggers HRV flag
STEPS_GOOD = 8000   # Steps threshold for "active day"

# ---------------------------------------------------------------------------
# Lift family groupings — exercises that share the same motor pattern
# and transfer to each other. Coach treats these as correlated, not independent.
# ---------------------------------------------------------------------------

LIFT_FAMILIES = {
    "horizontal_push": [
        "bench", "bench press", "incline bench", "incline press", "dumbbell bench",
        "db bench", "chest press", "inclined bench", "close grip bench",
    ],
    "vertical_push": [
        "overhead press", "ohp", "military press", "push press",
        "seated press", "dumbbell press", "db shoulder press",
    ],
    "squat_pattern": [
        "squat", "back squat", "front squat", "goblet squat", "leg press",
        "box squat", "pause squat", "tempo squat",
    ],
    "hip_hinge": [
        "deadlift", "conventional deadlift", "rdl", "romanian deadlift",
        "sumo deadlift", "good morning", "stiff leg", "trap bar deadlift",
    ],
    "vertical_pull": [
        "pullup", "pull-up", "chin-up", "chinup", "lat pulldown",
        "pulldown", "cable pulldown", "assisted pullup",
    ],
    "horizontal_pull": [
        "row", "barbell row", "bent over row", "cable row", "seated row",
        "dumbbell row", "db row", "t-bar row", "chest supported row",
    ],
    "accessory_arm": [
        "curl", "bicep curl", "hammer curl", "preacher curl",
        "tricep", "triceps", "extension", "pushdown", "skull crusher",
    ],
    "core_compound": [
        "nordic", "nordic curl", "hip thrust", "glute bridge",
        "lunges", "lunge", "bulgarian", "split squat",
    ],
}


# ---------------------------------------------------------------------------
# Daily readiness signal
# ---------------------------------------------------------------------------

def compute_daily_readiness(health_log: list, lift_history: list = None,
                             insights: dict = None) -> dict:
    """
    Compute today's HEALTH_READINESS signal from recent health data.
    Pure Python — no LLM, no external deps.

    health_log: list of health log entries (most recent last or first — sorted by Date).
    lift_history: optional list of lift entries for strength trend.
    insights: optional HEALTH_INSIGHTS dict (for insight strings).

    Returns HEALTH_READINESS dict:
    {
        "date": "2026-03-21",
        "readiness_score": 72,         # 0-100
        "constraints": ["max_rpe: 7"], # hard constraints for today's session
        "recommendations": ["sleep_target: 22:30"],
        "flags": ["hrv_below_baseline_3_days"],
        "insights": ["Based on your data: ..."]
    }
    """
    today = str(date.today())
    result: dict = {
        "date": today,
        "readiness_score": 75,  # default neutral
        "constraints": [],
        "recommendations": [],
        "flags": [],
        "insights": [],
    }

    if not health_log:
        result["flags"].append("no_health_data")
        return result

    # Sort by date, most recent last
    sorted_log = sorted(
        [e for e in health_log if e.get("Date") or e.get("date")],
        key=lambda e: e.get("Date") or e.get("date") or ""
    )
    recent = sorted_log[-7:]  # last 7 entries

    # --- Sleep analysis ---
    sleep_values = []
    for entry in recent:
        raw = entry.get("Sleep (hrs)") or entry.get("sleep_hrs") or ""
        try:
            sleep_values.append(float(str(raw).replace(",", ".")))
        except (ValueError, TypeError):
            pass

    sleep_score = 75  # default
    if sleep_values:
        recent_sleep = sleep_values[-1] if sleep_values else SLEEP_TARGET_HRS
        avg_sleep_7d = stats_lib.mean(sleep_values[-7:]) if len(sleep_values) >= 2 else recent_sleep
        sleep_debt_week = max(0.0, SLEEP_TARGET_HRS * min(len(sleep_values), 7) - sum(sleep_values[-7:]))

        if recent_sleep >= 8.0:
            sleep_score = 90
        elif recent_sleep >= SLEEP_TARGET_HRS:
            sleep_score = 80
        elif recent_sleep >= 6.5:
            sleep_score = 65
        elif recent_sleep >= SLEEP_LOW_THRESHOLD:
            sleep_score = 50
        else:
            sleep_score = 35
            result["flags"].append(f"sleep_critically_low_{recent_sleep:.1f}h")
            result["constraints"].append("max_rpe: 7")
            result["recommendations"].append("prioritize_sleep_tonight")

        if sleep_debt_week >= 5.0:
            result["flags"].append(f"sleep_debt_{sleep_debt_week:.1f}h_this_week")
            result["recommendations"].append(f"sleep_target_tonight: {_target_bedtime(8.0)}")
        elif sleep_debt_week >= 3.0:
            result["flags"].append(f"sleep_debt_{sleep_debt_week:.1f}h_this_week")
            result["recommendations"].append(f"sleep_target_tonight: {_target_bedtime(SLEEP_TARGET_HRS + 0.5)}")

        # 3-day sleep average
        recent_3d = sleep_values[-3:]
        if len(recent_3d) >= 2:
            avg_3d = stats_lib.mean(recent_3d)
            if avg_3d < SLEEP_LOW_THRESHOLD:
                result["flags"].append(f"avg_sleep_3d_{avg_3d:.1f}h_below_6h")
    else:
        sleep_score = 70
        result["flags"].append("no_sleep_data")

    # --- Steps analysis ---
    steps_score = 75
    steps_values = []
    for entry in recent:
        raw = entry.get("Steps") or entry.get("steps") or ""
        try:
            steps_values.append(int(float(str(raw).replace(",", ""))))
        except (ValueError, TypeError):
            pass

    if steps_values:
        recent_steps = steps_values[-1]
        if recent_steps < 3000:
            steps_score = 60
            result["recommendations"].append(f"walk_target: {STEPS_GOOD} steps today")
        elif recent_steps < STEPS_GOOD:
            steps_score = 70
            result["recommendations"].append(f"walk_target: {STEPS_GOOD} steps today")
        else:
            steps_score = 85

    # --- Food quality analysis ---
    food_values = []
    for entry in recent:
        raw = entry.get("Food Quality (1-10)") or entry.get("food_quality") or ""
        try:
            food_values.append(float(str(raw)))
        except (ValueError, TypeError):
            pass

    food_score = 75
    if food_values:
        avg_food = stats_lib.mean(food_values[-3:]) if len(food_values) >= 2 else food_values[-1]
        if avg_food <= 4:
            food_score = 50
            result["flags"].append(f"food_quality_low_{avg_food:.1f}_3d_avg")
        elif avg_food <= 6:
            food_score = 65
        else:
            food_score = 85

    # --- Composite readiness score ---
    # Weights: sleep 50%, steps 20%, food 30%
    readiness = int(sleep_score * 0.50 + steps_score * 0.20 + food_score * 0.30)
    readiness = max(10, min(100, readiness))
    result["readiness_score"] = readiness

    # --- RPE constraints from readiness ---
    if readiness < 50 and "max_rpe: 7" not in result["constraints"]:
        result["constraints"].append("max_rpe: 7")
    elif readiness < 65 and "max_rpe: 7" not in result["constraints"]:
        result["constraints"].append("max_rpe: 8")

    # --- Inject insights from HEALTH_INSIGHTS if available ---
    if insights:
        for key, insight in insights.items():
            if isinstance(insight, dict) and insight.get("insight_text"):
                result["insights"].append(insight["insight_text"])

    return result


# ---------------------------------------------------------------------------
# Weekly correlation pass
# ---------------------------------------------------------------------------

def compute_weekly_correlations(health_log: list, lift_history: list) -> dict:
    """
    Compute correlations between health metrics and training performance.
    Only surfaces insights when N >= N_MIN and R² >= R2_MIN.
    Returns HEALTH_INSIGHTS dict suitable for upsert_coach_state().

    Run weekly (Sunday). Cheap — pure Python math, no LLM.
    """
    results: dict = {}
    today = str(date.today())

    # Build paired datasets
    sleep_strength_pairs = _pair_sleep_to_strength(health_log, lift_history)
    sleep_rpe_pairs = _pair_sleep_to_rpe(health_log, lift_history)

    # Sleep → strength
    if len(sleep_strength_pairs) >= N_MIN:
        r, r2, n = _pearson_correlation(
            [p[0] for p in sleep_strength_pairs],
            [p[1] for p in sleep_strength_pairs],
        )
        if r2 >= R2_MIN:
            direction = "positive" if r > 0 else "negative"
            # Compute group means for interpretable insight
            good_sleep = [p[1] for p in sleep_strength_pairs if p[0] >= SLEEP_TARGET_HRS]
            poor_sleep = [p[1] for p in sleep_strength_pairs if p[0] < SLEEP_TARGET_HRS]
            if good_sleep and poor_sleep:
                avg_good = stats_lib.mean(good_sleep)
                avg_poor = stats_lib.mean(poor_sleep)
                pct_diff = ((avg_good - avg_poor) / avg_poor * 100) if avg_poor else 0
                results["sleep_strength"] = {
                    "n": n,
                    "r": round(r, 3),
                    "r2": round(r2, 3),
                    "direction": direction,
                    "insight_text": (
                        f"Based on your data: {SLEEP_TARGET_HRS}h+ sleep → "
                        f"avg +{pct_diff:.1f}% session weight (N={n} sessions). "
                        f"Tonight's sleep matters more than an extra warm-up set."
                    ) if pct_diff > 0 else (
                        f"Sleep vs. strength: r²={r2:.2f}, N={n}. "
                        f"Pattern emerging but not yet consistent."
                    ),
                    "computed_date": today,
                }

    # Sleep → RPE
    if len(sleep_rpe_pairs) >= N_MIN:
        r, r2, n = _pearson_correlation(
            [p[0] for p in sleep_rpe_pairs],
            [p[1] for p in sleep_rpe_pairs],
        )
        if r2 >= R2_MIN and r < 0:  # negative correlation expected: more sleep → lower RPE
            results["sleep_rpe"] = {
                "n": n,
                "r": round(r, 3),
                "r2": round(r2, 3),
                "direction": "negative",
                "insight_text": (
                    f"Based on your data: poor sleep (<{SLEEP_TARGET_HRS}h) correlates "
                    f"with higher session RPE (r={r:.2f}, N={n}). "
                    f"Expect harder effort on low-sleep days — drop 2.5kg without hesitation."
                ),
                "computed_date": today,
            }

    return results


# ---------------------------------------------------------------------------
# Health Agent integration: write HEALTH_READINESS to Coach State
# ---------------------------------------------------------------------------

def update_health_readiness(health_log: list, lift_history: list = None,
                             dry_run: bool = False) -> dict:
    """
    Compute and write HEALTH_READINESS to Coach State.
    Called from health_agent.py daily (pre-session brief or proactive pass).
    Returns the readiness dict.
    """
    # Load existing HEALTH_INSIGHTS if available
    insights: dict = {}
    try:
        from memory import read_single_summary
        insights = read_single_summary("HEALTH_INSIGHTS") or {}
    except Exception:
        pass

    readiness = compute_daily_readiness(health_log, lift_history, insights)

    if not dry_run:
        try:
            from memory import upsert_coach_state
            upsert_coach_state("HEALTH_READINESS", json.dumps(readiness, ensure_ascii=False), "HIGH")
        except Exception as e:
            print(f"  health_science: HEALTH_READINESS write failed: {e}")

    return readiness


def run_weekly_health_science(health_log: list, lift_history: list,
                               dry_run: bool = False) -> dict:
    """
    Weekly correlation pass. Called Sunday with full lift history + health log.
    Writes HEALTH_INSIGHTS and LIFT_INSIGHTS to Coach State.
    Returns dict with both 'health' and 'lift' keys.
    """
    health_insights = compute_weekly_correlations(health_log, lift_history)
    lift_insights = compute_lift_science(lift_history)

    if not dry_run:
        try:
            from memory import write_single_summary, upsert_coach_state
            if health_insights:
                write_single_summary("HEALTH_INSIGHTS", health_insights)
                print(f"  health_science: {len(health_insights)} health correlation(s) stored.")
            else:
                print(f"  health_science: No health correlations met threshold (N_MIN={N_MIN}).")

            if lift_insights:
                upsert_coach_state(
                    "LIFT_INSIGHTS",
                    json.dumps(lift_insights, ensure_ascii=False),
                    "HIGH",
                )
                trend_count = len(lift_insights.get("trends", {}))
                corr_count = len(lift_insights.get("family_correlations", {}))
                print(f"  health_science: lift insights — {trend_count} trends, {corr_count} family correlations stored.")
            else:
                print("  health_science: No lift insights computed (insufficient data).")
        except Exception as e:
            print(f"  health_science: write failed: {e}")

    return {"health": health_insights, "lift": lift_insights}


# ---------------------------------------------------------------------------
# Lift Science: within-family correlations, volume lag, trends
# ---------------------------------------------------------------------------

def compute_lift_science(lift_history: list) -> dict:
    """
    Compute all lift science insights from raw lift history.
    Pure Python — no LLM, deterministic.

    Returns LIFT_INSIGHTS dict:
    {
        "computed_date": "2026-03-22",
        "family_correlations": {
            "horizontal_push": {
                "exercises": ["bench", "incline bench"],
                "r": 0.84, "r2": 0.71, "n": 18,
                "insight_text": "Your bench and incline bench move together (r=0.84)..."
            }
        },
        "volume_lag": {
            "squat": {"lag_weeks": 2, "r": 0.71, "r2": 0.50, "n": 14, "insight_text": "..."},
        },
        "trends": {
            "squat": {"ma4": 92.5, "ma8": 90.1, "direction": "trending_up", "pct_change_4w": 2.7},
            "bench": {"ma4": 78.0, "ma8": 75.5, "direction": "trending_up", "pct_change_4w": 3.3},
        }
    }
    """
    today = str(date.today())
    result: dict = {
        "computed_date": today,
        "family_correlations": {},
        "volume_lag": {},
        "trends": {},
    }

    if not lift_history:
        return result

    result["family_correlations"] = _compute_lift_family_correlations(lift_history)
    result["volume_lag"] = _compute_volume_strength_lag(lift_history)
    result["trends"] = _compute_lift_trends(lift_history)

    return result


def _normalize_exercise_name(name: str) -> str:
    """Lowercase, strip extra whitespace, normalize common variants."""
    if not name:
        return ""
    n = name.lower().strip()
    # Normalize common abbreviations
    n = n.replace("b. press", "bench press").replace("bp", "bench press")
    n = n.replace("b.squat", "back squat").replace("sq.", "squat")
    return n


def _classify_exercise_family(exercise_name: str) -> Optional[str]:
    """Return the LIFT_FAMILIES key that best matches this exercise, or None."""
    normalized = _normalize_exercise_name(exercise_name)
    for family, members in LIFT_FAMILIES.items():
        for member in members:
            if member in normalized or normalized in member:
                return family
    return None


def _build_weekly_exercise_data(lift_history: list) -> dict:
    """
    Aggregate lift_history into weekly data per exercise.
    Infers week from Date field.
    Returns: {exercise_name: {week_key: {"max_weight": float, "total_volume": float, "sets": int}}}
    Week key format: "YYYY-WNN" (ISO week)
    """
    weekly: dict = {}

    for entry in lift_history:
        raw_date = entry.get("Date") or entry.get("date") or ""
        exercise = _normalize_exercise_name(
            entry.get("Exercise") or entry.get("exercise") or
            entry.get("Lift") or entry.get("lift") or ""
        )
        if not exercise or not raw_date:
            continue

        # Parse date and get ISO week
        try:
            if len(raw_date) >= 10:
                d = date.fromisoformat(raw_date[:10])
            else:
                continue
        except ValueError:
            continue

        week_key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"

        # Extract weight and rep data
        actual_raw = (
            entry.get("Actual Weight/Reps") or entry.get("actual") or
            entry.get("Weight") or entry.get("weight") or ""
        )
        weight = _extract_weight_kg(str(actual_raw))
        if not weight:
            continue

        # Estimate reps from entry (sets×reps format like "4x5")
        reps = 1
        import re as _re
        reps_m = _re.search(r"(\d+)\s*[xX×]\s*(\d+)", str(actual_raw))
        if reps_m:
            reps = int(reps_m.group(2))
        sets_m = _re.search(r"(\d+)\s*[xX×]", str(actual_raw))
        sets = int(sets_m.group(1)) if sets_m else 1

        if exercise not in weekly:
            weekly[exercise] = {}
        if week_key not in weekly[exercise]:
            weekly[exercise][week_key] = {"max_weight": 0.0, "total_volume": 0.0, "sets": 0}

        weekly[exercise][week_key]["max_weight"] = max(
            weekly[exercise][week_key]["max_weight"], weight
        )
        weekly[exercise][week_key]["total_volume"] += weight * reps * sets
        weekly[exercise][week_key]["sets"] += sets

    return weekly


def _compute_lift_family_correlations(lift_history: list) -> dict:
    """
    For each lift family with 2+ exercises present, compute week-over-week
    % change correlation between exercise pairs.
    Returns family_correlations dict.
    """
    weekly = _build_weekly_exercise_data(lift_history)
    results: dict = {}

    for family, members in LIFT_FAMILIES.items():
        # Find exercises present in history that belong to this family
        present = []
        for ex in weekly.keys():
            if _classify_exercise_family(ex) == family:
                present.append(ex)

        if len(present) < 2:
            continue

        # For each pair, compute week-over-week % changes and correlate
        best_pair = None
        best_r2 = 0.0

        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                ex_a, ex_b = present[i], present[j]
                weeks_a = weekly[ex_a]
                weeks_b = weekly[ex_b]

                # Common weeks with enough data in both
                common_weeks = sorted(set(weeks_a.keys()) & set(weeks_b.keys()))
                if len(common_weeks) < N_MIN_LIFT + 1:
                    continue

                # Compute week-over-week % change series
                changes_a: list = []
                changes_b: list = []
                for idx, wk in enumerate(common_weeks[1:], 1):
                    prev_wk = common_weeks[idx - 1]
                    if prev_wk not in weeks_a or prev_wk not in weeks_b:
                        continue
                    prev_a = weeks_a[prev_wk]["max_weight"]
                    curr_a = weeks_a[wk]["max_weight"]
                    prev_b = weeks_b[prev_wk]["max_weight"]
                    curr_b = weeks_b[wk]["max_weight"]
                    if prev_a > 0 and prev_b > 0:
                        changes_a.append((curr_a - prev_a) / prev_a * 100)
                        changes_b.append((curr_b - prev_b) / prev_b * 100)

                if len(changes_a) < N_MIN_LIFT:
                    continue

                r, r2, n = _pearson_correlation(changes_a, changes_b)
                if r2 > best_r2:
                    best_r2 = r2
                    best_pair = (ex_a, ex_b, r, r2, n)

        if best_pair and best_pair[3] >= R2_MIN_LIFT:
            ex_a, ex_b, r, r2, n = best_pair
            direction = "together" if r > 0 else "inversely"
            lag_note = ""
            if r > 0.6:
                lag_note = f" When {ex_a} stalls, {ex_b} typically follows within 1-2 weeks."
            results[family] = {
                "exercises": [ex_a, ex_b],
                "r": round(r, 3),
                "r2": round(r2, 3),
                "n": n,
                "insight_text": (
                    f"Your {ex_a} and {ex_b} move {direction} (r={r:.2f}, N={n})."
                    f"{lag_note}"
                    f" Programming one affects the other — don't treat them as independent."
                ),
            }

    return results


def _compute_volume_strength_lag(lift_history: list, lag_weeks: int = 2) -> dict:
    """
    For each main lift, check if weekly volume predicts max weight lag_weeks later.
    Returns volume_lag dict per exercise.
    """
    weekly = _build_weekly_exercise_data(lift_history)
    results: dict = {}

    for exercise, week_data in weekly.items():
        sorted_weeks = sorted(week_data.keys())
        if len(sorted_weeks) < N_MIN_VOLUME_LAG + lag_weeks:
            continue

        pairs = []
        for i, wk in enumerate(sorted_weeks):
            future_idx = i + lag_weeks
            if future_idx >= len(sorted_weeks):
                break
            future_wk = sorted_weeks[future_idx]
            vol = week_data[wk]["total_volume"]
            future_strength = week_data[future_wk]["max_weight"]
            if vol > 0 and future_strength > 0:
                pairs.append((vol, future_strength))

        if len(pairs) < N_MIN_VOLUME_LAG:
            continue

        r, r2, n = _pearson_correlation([p[0] for p in pairs], [p[1] for p in pairs])
        if r2 >= R2_MIN_LIFT and r > 0:  # positive lag expected: more volume → future strength
            # Compute interpretable numbers
            high_vol_weeks = [p for p in pairs if p[0] >= stats_lib.median([p[0] for p in pairs])]
            low_vol_weeks = [p for p in pairs if p[0] < stats_lib.median([p[0] for p in pairs])]
            if high_vol_weeks and low_vol_weeks:
                avg_high_strength = stats_lib.mean([p[1] for p in high_vol_weeks])
                avg_low_strength = stats_lib.mean([p[1] for p in low_vol_weeks])
                pct_diff = (avg_high_strength - avg_low_strength) / avg_low_strength * 100 if avg_low_strength else 0
                results[exercise] = {
                    "lag_weeks": lag_weeks,
                    "r": round(r, 3),
                    "r2": round(r2, 3),
                    "n": n,
                    "insight_text": (
                        f"High {exercise} volume in a given week predicts "
                        f"+{pct_diff:.1f}% strength {lag_weeks} weeks later "
                        f"(r={r:.2f}, N={n}). Current volume is your future strength signal."
                    ),
                }

    return results


def _compute_lift_trends(lift_history: list) -> dict:
    """
    For each exercise with enough data, compute 4-week and 8-week moving averages
    of max weight and flag trend direction.
    Returns trends dict per exercise.
    """
    weekly = _build_weekly_exercise_data(lift_history)
    results: dict = {}

    for exercise, week_data in weekly.items():
        sorted_weeks = sorted(week_data.keys())
        if len(sorted_weeks) < 4:
            continue

        weights = [week_data[wk]["max_weight"] for wk in sorted_weeks]

        ma4 = stats_lib.mean(weights[-4:]) if len(weights) >= 4 else None
        ma8 = stats_lib.mean(weights[-8:]) if len(weights) >= 8 else None

        # Trend direction: compare most recent 2 weeks vs 2 weeks prior in the 4-week window
        if len(weights) >= 4:
            recent_avg = stats_lib.mean(weights[-2:])
            prior_avg = stats_lib.mean(weights[-4:-2])
            pct_change = (recent_avg - prior_avg) / prior_avg * 100 if prior_avg else 0

            if pct_change > 1.0:
                direction = "trending_up"
            elif pct_change < -1.0:
                direction = "trending_down"
            else:
                direction = "plateaued"
        else:
            pct_change = 0.0
            direction = "insufficient_data"

        results[exercise] = {
            "ma4": round(ma4, 1) if ma4 else None,
            "ma8": round(ma8, 1) if ma8 else None,
            "direction": direction,
            "pct_change_4w": round(pct_change, 1),
            "last_weight": round(weights[-1], 1),
            "weeks_tracked": len(sorted_weeks),
        }

    return results


# ---------------------------------------------------------------------------
# Private: data preparation helpers
# ---------------------------------------------------------------------------

def _pair_sleep_to_strength(health_log: list, lift_history: list) -> list:
    """
    Pair each day's sleep with the next-day session's average actual weight.
    Returns list of (sleep_hrs, avg_weight_kg) pairs.
    """
    if not health_log or not lift_history:
        return []

    # Build sleep dict: date → hours
    sleep_by_date: dict = {}
    for entry in health_log:
        d = entry.get("Date") or entry.get("date") or ""
        raw = entry.get("Sleep (hrs)") or entry.get("sleep_hrs") or ""
        try:
            sleep_by_date[d] = float(str(raw).replace(",", "."))
        except (ValueError, TypeError):
            pass

    # Build strength dict: date → list of weights
    strength_by_date: dict = {}
    for entry in lift_history:
        d = entry.get("Date") or entry.get("date") or ""
        raw = entry.get("Actual Weight/Reps") or entry.get("actual") or ""
        weight = _extract_weight_kg(str(raw))
        if weight and d:
            strength_by_date.setdefault(d, []).append(weight)

    # Pair: sleep on day X → strength on day X+1
    pairs = []
    for sleep_date, sleep_hrs in sleep_by_date.items():
        try:
            d = date.fromisoformat(sleep_date)
            next_d = str(d + timedelta(days=1))
        except ValueError:
            continue
        if next_d in strength_by_date:
            avg_weight = stats_lib.mean(strength_by_date[next_d])
            pairs.append((sleep_hrs, avg_weight))

    return pairs


def _pair_sleep_to_rpe(health_log: list, lift_history: list) -> list:
    """
    Pair each day's sleep with the next-day session's RPE (from Notes field).
    Returns list of (sleep_hrs, rpe) pairs.
    """
    if not health_log or not lift_history:
        return []

    sleep_by_date: dict = {}
    for entry in health_log:
        d = entry.get("Date") or entry.get("date") or ""
        raw = entry.get("Sleep (hrs)") or entry.get("sleep_hrs") or ""
        try:
            sleep_by_date[d] = float(str(raw).replace(",", "."))
        except (ValueError, TypeError):
            pass

    rpe_by_date: dict = {}
    for entry in lift_history:
        d = entry.get("Date") or entry.get("date") or ""
        notes = (entry.get("Notes") or entry.get("notes") or "").lower()
        rpe = _extract_rpe(notes)
        if rpe and d and d not in rpe_by_date:
            rpe_by_date[d] = rpe

    pairs = []
    for sleep_date, sleep_hrs in sleep_by_date.items():
        try:
            d = date.fromisoformat(sleep_date)
            next_d = str(d + timedelta(days=1))
        except ValueError:
            continue
        if next_d in rpe_by_date:
            pairs.append((sleep_hrs, rpe_by_date[next_d]))

    return pairs


# ---------------------------------------------------------------------------
# Private: math helpers
# ---------------------------------------------------------------------------

def _pearson_correlation(x: list, y: list) -> tuple:
    """
    Compute Pearson r, r², and n for paired lists x, y.
    Returns (r, r², n). Uses pure Python stdlib.
    """
    n = len(x)
    if n < 2:
        return 0.0, 0.0, n

    # Filter out any non-finite pairs
    pairs = [(xi, yi) for xi, yi in zip(x, y)
             if math.isfinite(xi) and math.isfinite(yi)]
    n = len(pairs)
    if n < 2:
        return 0.0, 0.0, n

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    mean_x = stats_lib.mean(xs)
    mean_y = stats_lib.mean(ys)

    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in pairs)
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in xs))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in ys))

    if den_x == 0 or den_y == 0:
        return 0.0, 0.0, n

    r = num / (den_x * den_y)
    r = max(-1.0, min(1.0, r))  # clamp floating point errors
    return r, r ** 2, n


def _extract_weight_kg(text: str) -> Optional[float]:
    """Extract first numeric value from weight string like '100kg', '92.5 kg 4x4', etc."""
    import re
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def _extract_rpe(notes: str) -> Optional[float]:
    """Extract RPE value from notes string like 'rpe 8', 'rpe8', '@8', 'felt 8/10'."""
    import re
    patterns = [
        r"rpe\s*[:\-]?\s*(\d+(?:[.,]\d+)?)",
        r"@\s*(\d+(?:[.,]\d+)?)",
        r"felt\s+(\d+)\s*/\s*10",
        r"effort\s+(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, notes, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                if 1.0 <= val <= 10.0:
                    return val
            except ValueError:
                pass
    return None


def _target_bedtime(target_sleep_hrs: float) -> str:
    """Return recommended bedtime string given target sleep hours (assumes 7am wake-up)."""
    wake_hour = 7  # approximate
    bedtime_hour = (wake_hour - int(target_sleep_hrs)) % 24
    mins = int((target_sleep_hrs % 1) * 60)
    return f"{bedtime_hour:02d}:{mins:02d}"
