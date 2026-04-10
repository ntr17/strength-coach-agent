"""
estimate_strength.py — Multi-formula strength estimation engine.

Estimates e1RM and e5RM for each tracked exercise using:
  - Three time windows (short/medium/long), weighted toward recent data
  - Three established 1RM formulas per window, voted into a consensus
  - AMRAP flag and RPE to weight individual sets
  - Correlated lift fallback when primary lift has sparse data
  - Olympic lift section with a technique-adjusted estimate and clear labeling
  - Per-lift e5RM factors (not a single fixed %)

Only sets with should_count=1 are used.

Usage:
    python scripts/estimate_strength.py                    # all exercises
    python scripts/estimate_strength.py --exercise bench   # fuzzy match
    python scripts/estimate_strength.py --db-path data/coach.db
    python scripts/estimate_strength.py --write            # save to strength_estimates table
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "coach.db")

# ---------------------------------------------------------------------------
# Exercise catalogue
# ---------------------------------------------------------------------------

# Canonical names. Keys are lowercase lookup strings (partial match allowed).
MAIN_LIFTS = {
    # Lower body
    "squat":             "Squat",
    "front squat":       "Front Squat",
    "deadlift":          "Deadlift",
    "romanian deadlift": "Romanian Deadlift",
    "rdl":               "Romanian Deadlift",

    # Upper push
    "bench press":       "Bench Press",
    "bench":             "Bench Press",
    "incline bench 30":  "Incline Bench 30°",
    "incline 30":        "Incline Bench 30°",
    "incline bench 45":  "Incline Bench 45°",
    "incline 45":        "Incline Bench 45°",
    "overhead press":    "Overhead Press",
    "ohp":               "Overhead Press",
    "push press":        "Push Press",
    "weighted dips":     "Weighted Dips",
    "dips":              "Weighted Dips",

    # Upper pull
    "barbell row power": "BB Row (Power)",
    "bb row power":      "BB Row (Power)",
    "power row":         "BB Row (Power)",
    "barbell row strict":"BB Row (Strict)",
    "bb row strict":     "BB Row (Strict)",
    "strict row":        "BB Row (Strict)",
    "pull-up wide":      "Pull-up (Wide)",
    "pullup wide":       "Pull-up (Wide)",
    "pull-up neutral":   "Pull-up (Neutral)",
    "pullup neutral":    "Pull-up (Neutral)",
    "pull-up close":     "Pull-up (Close)",
    "pullup close":      "Pull-up (Close)",
    "pull-up":           "Pull-up (Wide)",      # default grip
    "pullup":            "Pull-up (Wide)",
    "chin-up":           "Chin-up",
    "chinup":            "Chin-up",
    "barbell curl":      "Barbell Curl",
    "bb curl":           "Barbell Curl",
}

OLYMPIC_LIFTS = {
    "power clean":       "Power Clean",
    "hang power clean":  "Hang Power Clean",
    "clean":             "Clean",
    "clean & jerk":      "Clean & Jerk",
    "power snatch":      "Power Snatch",
    "snatch":            "Snatch",
    "hang snatch":       "Hang Snatch",
}

# ---------------------------------------------------------------------------
# e5RM factors — lift-specific (not a fixed %)
# Justification: neurally demanding compound lifts (squat, DL) fatigue faster,
# so 5RM is a smaller fraction of 1RM than upper body lifts.
# Epley's formula implies ~85.7% generically; these are calibrated per movement.
# ---------------------------------------------------------------------------

E5RM_FACTORS = {
    "Squat":             0.845,  # high neural demand, 5RM is a grind
    "Front Squat":       0.848,  # slightly better rep repeatability than back squat
    "Deadlift":          0.840,  # highest neural demand — 5 DLs is brutal
    "Romanian Deadlift": 0.858,  # less peak neural, better stamina curve
    "Bench Press":       0.860,  # upper body recovers faster between reps
    "Incline Bench 30°": 0.858,
    "Incline Bench 45°": 0.855,
    "Overhead Press":    0.850,  # stabilizer fatigue hits harder
    "Push Press":        0.852,
    "Weighted Dips":     0.855,
    "BB Row (Power)":    0.855,
    "BB Row (Strict)":   0.860,
    "Pull-up (Wide)":    0.848,
    "Pull-up (Neutral)": 0.850,
    "Pull-up (Close)":   0.852,
    "Chin-up":           0.853,
    "Barbell Curl":      0.865,  # isolation — good rep repeatability
}

DEFAULT_E5RM_FACTOR = 0.855

# ---------------------------------------------------------------------------
# Olympic lift technique discount
# A technical failure ≠ strength ceiling. We apply a discount to e1RM
# estimates for Olympic lifts, and label outputs clearly.
# Power variants get a smaller discount (less technique demand at limit).
# ---------------------------------------------------------------------------

OLY_TECHNIQUE_DISCOUNT = {
    "Power Clean":       0.95,   # power variations: mostly strength-limited
    "Hang Power Clean":  0.93,
    "Power Snatch":      0.92,
    "Clean":             0.88,   # full lifts: more technique-sensitive
    "Clean & Jerk":      0.86,
    "Snatch":            0.84,
    "Hang Snatch":       0.87,
}

# ---------------------------------------------------------------------------
# Correlation map — (source_canonical, discount)
# If target lift has <MIN_SESSIONS_FOR_DIRECT data, estimate via source / discount.
# discount = typical ratio of target / source (e.g. incline ≈ 88% of bench → 0.88).
# ---------------------------------------------------------------------------

# Format: target_canonical → [(source_canonical, ratio), ...]
# ratio = target / source (so source estimate × ratio = target estimate)
CORRELATIONS: dict[str, list[tuple[str, float]]] = {
    "Incline Bench 30°":  [("Bench Press",        0.88)],
    "Incline Bench 45°":  [("Bench Press",        0.83), ("Incline Bench 30°", 0.94)],
    "Front Squat":        [("Squat",              0.82)],
    "Romanian Deadlift":  [("Deadlift",           0.85)],
    "Overhead Press":     [("Push Press",          0.88)],
    "Push Press":         [("Overhead Press",     1.14)],  # push press > OHP
    "BB Row (Strict)":    [("BB Row (Power)",      0.85)],
    "BB Row (Power)":     [("BB Row (Strict)",     1.18)],
    "Pull-up (Neutral)":  [("Pull-up (Wide)",      1.03)],
    "Pull-up (Close)":    [("Pull-up (Wide)",      0.97)],
    "Chin-up":            [("Pull-up (Wide)",      1.05)],  # chin-ups easier for most
    "Weighted Dips":      [("Bench Press",         0.70)],
    # Olympic ↔ barbell correlation (very rough — labeled accordingly)
    "Power Clean":        [("Deadlift",            0.55)],
    "Hang Power Clean":   [("Deadlift",            0.50)],
}

MIN_SESSIONS_FOR_DIRECT = 3  # use correlations below this threshold

# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------

WINDOWS = [
    ("short",  3,   0.50),   # last 3 sessions  → 50% weight
    ("medium", 8,   0.30),   # last 8 sessions  → 30% weight
    ("long",   9999, 0.20),  # all sessions     → 20% weight
]

# ---------------------------------------------------------------------------
# 1RM formulas
# Inputs: weight_kg (float), reps (int)
# All formulas degrade at high rep counts; we exclude reps > 15.
# ---------------------------------------------------------------------------

def epley(w: float, r: int) -> float:
    """Epley (1985). Accurate 1-10 reps. Tends high at 1-3."""
    if r == 1:
        return w
    return w * (1 + r / 30)


def brzycki(w: float, r: int) -> float:
    """Brzycki (1993). Conservative, most accurate 1-6 reps."""
    if r >= 37:
        return w  # formula breaks down
    if r == 1:
        return w
    return w * 36 / (37 - r)


def wathan(w: float, r: int) -> float:
    """Wathan (1994). Best accuracy 7-15 reps. Based on athletes."""
    if r == 1:
        return w
    return 100 * w / (48.8 + 53.8 * math.exp(-0.075 * r))


# Formula selection by rep range — not all formulas are reliable at all ranges
def _formulas_for_reps(r: int) -> list[tuple[str, callable]]:
    if r <= 3:
        return [("Epley", epley), ("Brzycki", brzycki)]
    elif r <= 6:
        return [("Epley", epley), ("Brzycki", brzycki), ("Wathan", wathan)]
    elif r <= 15:
        return [("Epley", epley), ("Wathan", wathan)]
    else:
        return []  # unreliable above 15 reps


# ---------------------------------------------------------------------------
# Set confidence weight
# ---------------------------------------------------------------------------

def _set_confidence(is_amrap: bool, rpe: Optional[float]) -> float:
    """
    AMRAP at high RPE = strongest signal (weight 1.5×).
    Non-AMRAP without RPE = baseline (1.0).
    """
    if is_amrap:
        if rpe is not None:
            # RPE 10 → 1.5, RPE 6 → 1.0, linear
            return 1.0 + max(0, (rpe - 6) / 8)
        return 1.35  # AMRAP without RPE: assume near-maximal
    else:
        if rpe is not None:
            # Non-AMRAP: high RPE still increases signal quality slightly
            return 0.9 + max(0, (rpe - 6) / 20)
        return 1.0


# ---------------------------------------------------------------------------
# Core estimation for a window of sets
# ---------------------------------------------------------------------------

def _estimate_window(sets: list[dict]) -> Optional[dict]:
    """
    Given a list of set dicts for one exercise, compute a weighted e1RM estimate.
    Returns None if no usable sets.
    """
    formula_estimates: dict[str, list[float]] = defaultdict(list)
    weights: dict[str, list[float]] = defaultdict(list)

    for s in sets:
        r = s["reps"]
        w = s["weight_kg"]
        formulas = _formulas_for_reps(r)
        if not formulas:
            continue  # skip sets > 15 reps

        confidence = _set_confidence(s["is_amrap"], s["rpe"])

        for fname, fn in formulas:
            est = fn(w, r)
            formula_estimates[fname].append(est)
            weights[fname].append(confidence)

    if not formula_estimates:
        return None

    # Weighted average per formula, then consensus across formulas
    formula_results = {}
    for fname, estimates in formula_estimates.items():
        ws = weights[fname]
        total_w = sum(ws)
        formula_results[fname] = sum(e * w for e, w in zip(estimates, ws)) / total_w

    values = list(formula_results.values())
    consensus = sum(values) / len(values)
    low = min(values)
    high = max(values)

    return {
        "e1rm":           consensus,
        "formula_low":    low,
        "formula_high":   high,
        "formula_detail": formula_results,
        "set_count":      len(sets),
    }


# ---------------------------------------------------------------------------
# Per-session e1RM (for session variance in CI)
# ---------------------------------------------------------------------------

def _per_session_e1rms(sets: list[dict]) -> list[float]:
    """
    For each distinct session, compute the best e1RM estimate from its sets.
    Returns a list of one float per session (only from countable sets).
    Used to incorporate session-to-session variance into the CI.
    """
    by_session: dict[str, list[float]] = defaultdict(list)
    for s in sets:
        formulas = _formulas_for_reps(s["reps"])
        if not formulas:
            continue
        for _, fn in formulas:
            est = fn(s["weight_kg"], s["reps"])
            by_session[s["session_date"]].append(est)

    return [max(estimates) for estimates in by_session.values() if estimates]


# ---------------------------------------------------------------------------
# Main estimation logic
# ---------------------------------------------------------------------------

def estimate_exercise(
    exercise_name: str,
    all_sets: list[dict],
    is_olympic: bool = False,
) -> Optional[dict]:
    """
    Full estimation for a single exercise.
    all_sets: counted sets for this exercise, sorted newest-first by session_date.
    """
    if not all_sets:
        return None

    # Group by session to count distinct sessions
    sessions_seen: set[str] = set()
    for s in all_sets:
        sessions_seen.add(s["session_date"])
    session_dates = sorted(sessions_seen, reverse=True)

    window_results = []
    for wname, n_sessions, wweight in WINDOWS:
        window_dates = set(session_dates[:n_sessions])
        window_sets = [s for s in all_sets if s["session_date"] in window_dates]
        result = _estimate_window(window_sets)
        if result:
            window_results.append((wname, n_sessions, wweight, result))

    if not window_results:
        return None

    # Weighted final e1RM
    total_w = sum(r[2] for r in window_results)
    e1rm_final = sum(r[2] * r[3]["e1rm"] for r in window_results) / total_w

    # Confidence interval: formula range (inter-formula disagreement) + session variance
    # Formula range: weighted spread between highest and lowest formula per window
    ci_low  = sum(r[2] * r[3]["formula_low"]  for r in window_results) / total_w
    ci_high = sum(r[2] * r[3]["formula_high"] for r in window_results) / total_w

    # Session variance: compute per-session best e1RM, add its std to the CI
    # This makes the CI wider when sessions are inconsistent (bad weeks, plateaus, etc.)
    per_session_e1rms = _per_session_e1rms(all_sets)
    if len(per_session_e1rms) >= 2:
        mean_s = sum(per_session_e1rms) / len(per_session_e1rms)
        variance = sum((x - mean_s) ** 2 for x in per_session_e1rms) / len(per_session_e1rms)
        session_std = math.sqrt(variance)
        ci_low  = min(ci_low,  e1rm_final - session_std)
        ci_high = max(ci_high, e1rm_final + session_std)

    # Apply Olympic technique discount
    technique_note = None
    if is_olympic:
        discount = OLY_TECHNIQUE_DISCOUNT.get(exercise_name, 0.90)
        e1rm_final *= discount
        ci_low     *= discount
        ci_high    *= discount
        technique_note = (
            f"Technique-adjusted (×{discount:.2f}). Olympic lift e1RM reflects "
            f"strength+technique combined — not a pure strength ceiling."
        )

    # e5RM
    canonical = _resolve_canonical(exercise_name)
    e5rm_factor = E5RM_FACTORS.get(canonical, DEFAULT_E5RM_FACTOR)
    e5rm_final = e1rm_final * e5rm_factor
    e5rm_low   = ci_low    * e5rm_factor
    e5rm_high  = ci_high   * e5rm_factor

    # Round to 0.5kg
    e1rm_final = _round_half(e1rm_final)
    ci_low     = _round_half(ci_low)
    ci_high    = _round_half(ci_high)
    e5rm_final = _round_half(e5rm_final)
    e5rm_low   = _round_half(e5rm_low)
    e5rm_high  = _round_half(e5rm_high)

    # Confidence label
    n_counted = len(all_sets)
    oldest_date = min(s["session_date"] for s in all_sets)
    weeks_of_data = (date.today() - date.fromisoformat(oldest_date)).days / 7
    n_sessions_total = len(sessions_seen)

    if n_counted >= 10 and n_sessions_total >= 5:
        confidence = "high"
    elif n_counted >= 5 or n_sessions_total >= 3:
        confidence = "medium"
    elif n_counted >= 2:
        confidence = "low"
    else:
        confidence = "very low"

    return {
        "exercise":     exercise_name,
        "e1rm":         e1rm_final,
        "e1rm_low":     ci_low,
        "e1rm_high":    ci_high,
        "e5rm":         e5rm_final,
        "e5rm_low":     e5rm_low,
        "e5rm_high":    e5rm_high,
        "confidence":   confidence,
        "n_sets":       n_counted,
        "n_sessions":   n_sessions_total,
        "window_detail": [
            {
                "window":    r[0],
                "sessions":  min(r[1], n_sessions_total),
                "set_count": r[3]["set_count"],
                "formulas":  {k: _round_half(v) for k, v in r[3]["formula_detail"].items()},
                "consensus": _round_half(r[3]["e1rm"]),
            }
            for r in window_results
        ],
        "technique_note": technique_note,
        "is_correlated":  False,
    }


def estimate_via_correlation(
    target_canonical: str,
    all_sets_by_exercise: dict[str, list[dict]],
) -> Optional[dict]:
    """Try to estimate target from a correlated source lift."""
    sources = CORRELATIONS.get(target_canonical, [])
    for source_canonical, ratio in sources:
        # Find sets for source by matching canonical name in data
        source_sets = _find_sets_for_canonical(source_canonical, all_sets_by_exercise)
        if not source_sets:
            continue

        is_oly = target_canonical in OLY_TECHNIQUE_DISCOUNT
        source_est = estimate_exercise(source_canonical, source_sets, is_olympic=False)
        if not source_est:
            continue

        # Scale by correlation ratio
        result = dict(source_est)
        result["exercise"]        = target_canonical
        result["e1rm"]            = _round_half(source_est["e1rm"]    * ratio)
        result["e1rm_low"]        = _round_half(source_est["e1rm_low"] * ratio)
        result["e1rm_high"]       = _round_half(source_est["e1rm_high"]* ratio)
        result["e5rm"]            = _round_half(source_est["e5rm"]     * ratio)
        result["e5rm_low"]        = _round_half(source_est["e5rm_low"] * ratio)
        result["e5rm_high"]       = _round_half(source_est["e5rm_high"]* ratio)
        result["confidence"]      = "very low"  # correlated, not direct
        result["is_correlated"]   = True
        result["correlated_from"] = source_canonical
        result["correlation_ratio"] = ratio

        if is_oly:
            discount = OLY_TECHNIQUE_DISCOUNT.get(target_canonical, 0.90)
            for key in ("e1rm", "e1rm_low", "e1rm_high", "e5rm", "e5rm_low", "e5rm_high"):
                result[key] = _round_half(result[key] * discount)
            result["technique_note"] = f"Correlated estimate + technique discount (×{discount:.2f})"

        return result

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_half(x: float) -> float:
    """Round to nearest 0.5kg."""
    return round(x * 2) / 2


def _resolve_canonical(name: str) -> str:
    """Resolve exercise name to canonical, falling back to the name itself."""
    lower = name.lower()
    # Check main lifts dict
    if lower in MAIN_LIFTS:
        return MAIN_LIFTS[lower]
    if lower in OLYMPIC_LIFTS:
        return OLYMPIC_LIFTS[lower]
    # Fuzzy: check if name contains a key
    for k, v in {**MAIN_LIFTS, **OLYMPIC_LIFTS}.items():
        if k in lower or lower in k:
            return v
    return name


def _find_sets_for_canonical(canonical: str, all_sets_by_exercise: dict) -> list[dict]:
    """Find sets matching a canonical name in the sets-by-exercise dict."""
    for ex_name, sets in all_sets_by_exercise.items():
        if _resolve_canonical(ex_name) == canonical:
            return sets
    return []


def _is_olympic(exercise_name: str) -> bool:
    canon = _resolve_canonical(exercise_name)
    return canon in set(OLYMPIC_LIFTS.values())


# ---------------------------------------------------------------------------
# Database fetch
# ---------------------------------------------------------------------------

def load_counted_sets(db_path: str) -> dict[str, list[dict]]:
    """
    Load all counted sets (should_count=1), grouped by exercise name.
    Each dict has: session_date, reps, weight_kg, is_amrap, rpe
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("""
            SELECT exercise, session_date, reps, weight_kg,
                   is_amrap, should_count, rpe
            FROM lift_sets
            WHERE should_count = 1
              AND reps > 0
              AND weight_kg > 0
            ORDER BY exercise, session_date DESC
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    result: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        result[row["exercise"]].append({
            "session_date": row["session_date"],
            "reps":         row["reps"],
            "weight_kg":    row["weight_kg"],
            "is_amrap":     bool(row["is_amrap"]),
            "rpe":          row["rpe"],
        })
    return dict(result)


# ---------------------------------------------------------------------------
# Write results to DB
# ---------------------------------------------------------------------------

def write_estimates(db_path: str, estimates: list[dict]) -> None:
    today = str(date.today())
    conn = sqlite3.connect(db_path)
    try:
        rows = []
        for est in estimates:
            if est is None:
                continue
            rows.append((
                today,
                est["exercise"],
                est["e1rm"],
                est["e5rm"],
                est.get("e1rm_low"),
                est.get("e1rm_high"),
                json.dumps(est.get("window_detail")),
            ))
        conn.executemany("""
            INSERT INTO strength_estimates
              (estimated_at, exercise, e1rm_kg, e5rm_kg, confidence_low, confidence_high, method_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_kg(kg: float) -> str:
    s = f"{kg:.1f}".rstrip("0").rstrip(".")
    return s + "kg"


def format_estimate(est: dict) -> str:
    lines = []
    ex = est["exercise"]
    corr = ""
    if est.get("is_correlated"):
        corr = f"  [estimated via {est['correlated_from']} × {est['correlation_ratio']:.2f}]"

    lines.append(f"\n{'─'*60}")
    lines.append(f"{ex}{corr}")
    lines.append(f"  e1RM: {_fmt_kg(est['e1rm'])}  (range: {_fmt_kg(est['e1rm_low'])}–{_fmt_kg(est['e1rm_high'])})")
    lines.append(f"  e5RM: {_fmt_kg(est['e5rm'])}  (range: {_fmt_kg(est['e5rm_low'])}–{_fmt_kg(est['e5rm_high'])})")
    lines.append(f"  Based on: {est['n_sets']} counted sets across {est['n_sessions']} sessions")
    lines.append(f"  Confidence: {est['confidence']}")

    if est.get("technique_note"):
        lines.append(f"  Note: {est['technique_note']}")

    if est.get("window_detail"):
        lines.append("  Method breakdown:")
        for w in est["window_detail"]:
            f_parts = ", ".join(f"{k} {v}kg" for k, v in w["formulas"].items())
            lines.append(f"    {w['window'].capitalize()} window ({w['sessions']} sessions, {w['set_count']} sets): {f_parts} → {w['consensus']}kg")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fuzzy_match(query: str, exercises: list[str]) -> list[str]:
    """Return exercises whose canonical name contains the query."""
    q = query.lower()
    return [
        ex for ex in exercises
        if q in ex.lower() or q in _resolve_canonical(ex).lower()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate e1RM and e5RM from lift_sets")
    parser.add_argument("--exercise", help="Exercise name (fuzzy match)")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--write", action="store_true", help="Save estimates to strength_estimates table")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db_path)
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}")
        print("Run: python scripts/init_db.py")
        sys.exit(1)

    all_sets_by_exercise = load_counted_sets(db_path)

    if not all_sets_by_exercise:
        print("No counted sets found in database. Import some sessions first.")
        sys.exit(0)

    # Filter by exercise if requested
    exercises = list(all_sets_by_exercise.keys())
    if args.exercise:
        exercises = _fuzzy_match(args.exercise, exercises)
        if not exercises:
            print(f"No exercise matching '{args.exercise}' found in database.")
            sys.exit(1)

    estimates = []
    for ex in sorted(exercises):
        sets = all_sets_by_exercise[ex]
        is_oly = _is_olympic(ex)
        canonical = _resolve_canonical(ex)

        n_sessions = len(set(s["session_date"] for s in sets))

        if n_sessions >= MIN_SESSIONS_FOR_DIRECT:
            est = estimate_exercise(ex, sets, is_olympic=is_oly)
        else:
            # Try direct first, fall back to correlation
            est = estimate_exercise(ex, sets, is_olympic=is_oly)
            if est:
                est["confidence"] = "low"  # few sessions even if direct
            else:
                est = estimate_via_correlation(canonical, all_sets_by_exercise)

        if est:
            estimates.append(est)

    # Also try to estimate catalogue lifts not yet in DB via correlation
    if not args.exercise:
        all_canonicals_in_db = {_resolve_canonical(ex) for ex in all_sets_by_exercise}
        catalogue_canonicals = set(MAIN_LIFTS.values()) | set(OLYMPIC_LIFTS.values())
        missing = catalogue_canonicals - all_canonicals_in_db
        for canon in sorted(missing):
            est = estimate_via_correlation(canon, all_sets_by_exercise)
            if est:
                est["exercise"] = canon
                estimates.append(est)

    if not estimates:
        print("No estimates could be computed.")
        sys.exit(0)

    if args.json:
        print(json.dumps(estimates, indent=2))
    else:
        print(f"\nStrength estimates — as of {date.today()}")
        for est in estimates:
            print(format_estimate(est))
        print()

    if args.write:
        write_estimates(db_path, estimates)
        print(f"Saved {len(estimates)} estimate(s) to strength_estimates table.")


if __name__ == "__main__":
    main()
