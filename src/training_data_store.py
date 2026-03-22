"""
training_data_store.py — Data Cleaning Pipeline for Strength Coach

Transforms raw lift_history + health_log entries into clean, analysis-ready
datasets. Handles the Actual column (nearly always empty), normalizes exercise
names, flags missing data, and generates clarification requests.

Design philosophy:
  - Don't silently impute. Flag missing, infer where obvious, ask when critical.
  - Be like a DS: know the data quality, report it, fix what's fixable.
  - Never crash the coach. All data issues are captured as warnings.

Terminology:
  - "prescribed" weight/reps = what the program says
  - "actual" weight/reps = what the athlete logged
  - "inferred" = prescribed used as proxy when actual is missing

Entry point: DataStore(lift_history, health_log).build()
"""

import re
from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Canonical exercise name map — normalize common variants
# ---------------------------------------------------------------------------

CANONICAL_NAMES: dict[str, str] = {
    # Squat variations
    "back squat": "squat",
    "b. squat": "squat",
    "sq.": "squat",
    "squats": "squat",
    "pause squat": "squat (pause)",
    "tempo squat": "squat (tempo)",
    "box squat": "squat (box)",
    "front sq": "front squat",
    "front squats": "front squat",
    "goblet sq": "goblet squat",
    # Bench variations
    "bench": "bench press",
    "bp": "bench press",
    "b.p.": "bench press",
    "b. press": "bench press",
    "benchpress": "bench press",
    "inclined bench": "incline bench press",
    "incline bp": "incline bench press",
    "incline": "incline bench press",
    "incline press": "incline bench press",
    "db bench": "dumbbell bench press",
    "dumbbell bench": "dumbbell bench press",
    # OHP variations
    "ohp": "overhead press",
    "o.h.p.": "overhead press",
    "military press": "overhead press",
    "seated ohp": "seated overhead press",
    "push press": "push press",
    # Deadlift variations
    "dl": "deadlift",
    "conventional dl": "deadlift",
    "conv. deadlift": "deadlift",
    "sumo dl": "sumo deadlift",
    "rdl": "romanian deadlift",
    "r.d.l.": "romanian deadlift",
    "romanian dl": "romanian deadlift",
    "stiff leg": "stiff leg deadlift",
    "sldl": "stiff leg deadlift",
    # Row variations
    "bb row": "barbell row",
    "bent over row": "barbell row",
    "b.o. row": "barbell row",
    "t-bar": "t-bar row",
    "seated cable row": "cable row",
    "cable rows": "cable row",
    "db row": "dumbbell row",
    "1-arm row": "dumbbell row",
    # Pull variations
    "pull up": "pull-up",
    "pullup": "pull-up",
    "chin up": "chin-up",
    "chinup": "chin-up",
    "lat pulldown": "lat pulldown",
    "lat pd": "lat pulldown",
    "pulldown": "lat pulldown",
    # Curl variations
    "bicep curl": "barbell curl",
    "biceps curl": "barbell curl",
    "bb curl": "barbell curl",
    "ez curl": "ez bar curl",
    "hammer": "hammer curl",
    # Tricep variations
    "tricep pushdown": "tricep pushdown",
    "triceps pushdown": "tricep pushdown",
    "skull crusher": "skull crusher",
    "skullcrusher": "skull crusher",
    "jm press": "jm press",
    # Core / accessories
    "hip thrust": "hip thrust",
    "glute bridge": "glute bridge",
    "nordic": "nordic curl",
    "nordic hamstring curl": "nordic curl",
    "bulgarian": "bulgarian split squat",
    "bulgarian ss": "bulgarian split squat",
    "split squat": "split squat",
    "lunge": "lunges",
    "walking lunge": "walking lunges",
    "face pull": "face pull",
    "face pulls": "face pull",
    # Cardio proxies
    "run": "running",
    "jog": "running",
    "cycling": "cycling",
    "bike": "cycling",
    "row machine": "rowing machine",
    "concept2": "rowing machine",
}


def normalize_exercise_name(name: str) -> str:
    """
    Normalize exercise name to canonical form.
    1. Lowercase + strip
    2. Check canonical map (exact, then prefix)
    3. Return best match or cleaned version
    """
    if not name:
        return ""
    n = name.lower().strip()

    # Remove parenthetical notes like "(volume)" or "(heavy)"
    n_clean = re.sub(r"\s*\([^)]*\)\s*$", "", n).strip()

    # Exact match
    if n_clean in CANONICAL_NAMES:
        return CANONICAL_NAMES[n_clean]

    # Prefix match (longest key that the name starts with)
    best_key = ""
    for key in CANONICAL_NAMES:
        if n_clean.startswith(key) and len(key) > len(best_key):
            best_key = key

    if best_key:
        return CANONICAL_NAMES[best_key]

    # Return cleaned version
    return n_clean


# ---------------------------------------------------------------------------
# Weight extraction helpers
# ---------------------------------------------------------------------------

def _extract_weight(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:kg)?", str(text).strip())
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _extract_sets_reps(text: str) -> tuple[Optional[int], Optional[int]]:
    """Extract (sets, reps) from text like '4x5', '3x8-10'. Returns (None, None) if not found."""
    if not text:
        return None, None
    m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", str(text))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


# ---------------------------------------------------------------------------
# Single lift entry cleaner
# ---------------------------------------------------------------------------

class CleanLiftEntry:
    """
    A cleaned lift history entry with data quality annotations.
    """
    __slots__ = (
        "raw", "exercise", "date", "week",
        "prescribed_weight", "actual_weight", "weight_source",
        "sets", "reps", "sets_reps_source",
        "done", "notes", "rpe",
        "warnings",
    )

    def __init__(self, raw: dict):
        self.raw = raw
        self.warnings: list[str] = []
        self._parse()

    def _parse(self):
        # Exercise
        raw_name = (
            self.raw.get("Exercise") or self.raw.get("exercise") or
            self.raw.get("Lift") or self.raw.get("lift") or ""
        )
        self.exercise = normalize_exercise_name(raw_name)

        # Date and week
        date_raw = self.raw.get("Date") or self.raw.get("date") or ""
        self.date = date_raw[:10] if len(str(date_raw)) >= 10 else ""
        self.week = self._infer_week()

        # Prescribed weight (always available if program is set)
        prescribed_raw = self.raw.get("Weight") or self.raw.get("weight") or ""
        self.prescribed_weight = _extract_weight(str(prescribed_raw))

        # Actual weight (often empty — this is the core data quality problem)
        actual_raw = (
            self.raw.get("Actual Weight/Reps") or self.raw.get("actual") or
            self.raw.get("Actual") or ""
        )
        actual_parsed = _extract_weight(str(actual_raw)) if actual_raw else None

        if actual_parsed:
            self.actual_weight = actual_parsed
            self.weight_source = "actual"
        elif self.prescribed_weight:
            # Infer from prescribed — mark as inferred
            done_raw = (self.raw.get("Done") or self.raw.get("done") or "").lower().strip()
            is_done = done_raw in ("yes", "done", "true", "1", "x", "✓", "ok")
            if is_done:
                self.actual_weight = self.prescribed_weight
                self.weight_source = "inferred_from_prescribed"
                self.warnings.append(
                    f"actual_weight_missing: using prescribed {self.prescribed_weight}kg as proxy"
                )
            else:
                self.actual_weight = None
                self.weight_source = "missing"
        else:
            self.actual_weight = None
            self.weight_source = "missing"
            if not actual_raw:
                self.warnings.append("actual_weight_missing: no prescribed weight to fall back on")

        # Sets and reps
        sr_raw = self.raw.get("Sets x Reps") or self.raw.get("sets_reps") or ""
        sets, reps = _extract_sets_reps(str(sr_raw))

        if sets and reps:
            self.sets = sets
            self.reps = reps
            self.sets_reps_source = "explicit"
        else:
            # Try from actual field
            sets2, reps2 = _extract_sets_reps(str(actual_raw))
            if sets2 and reps2:
                self.sets = sets2
                self.reps = reps2
                self.sets_reps_source = "actual_field"
            else:
                self.sets = None
                self.reps = None
                self.sets_reps_source = "missing"
                self.warnings.append("sets_reps_missing")

        # Done status
        done_raw = (self.raw.get("Done") or self.raw.get("done") or "").lower().strip()
        self.done = done_raw in ("yes", "done", "true", "1", "x", "✓", "ok")

        # Notes
        self.notes = self.raw.get("Session Notes") or self.raw.get("Notes") or self.raw.get("notes") or ""

        # RPE
        self.rpe = self._extract_rpe()

    def _infer_week(self) -> Optional[str]:
        try:
            d = date.fromisoformat(str(self.date)[:10])
            y, w, _ = d.isocalendar()
            return f"{y}-W{w:02d}"
        except (ValueError, TypeError, AttributeError):
            return None

    def _extract_rpe(self) -> Optional[float]:
        text = (self.raw.get("RPE") or self.raw.get("rpe") or self.notes or "").lower()
        patterns = [
            r"rpe\s*[:\-]?\s*(\d+(?:[.,]\d+)?)",
            r"@\s*(\d+(?:[.,]\d+)?)\b",
            r"felt\s+(\d+)\s*/\s*10",
        ]
        for p in patterns:
            m = re.search(p, str(text), re.IGNORECASE)
            if m:
                try:
                    val = float(m.group(1).replace(",", "."))
                    if 1.0 <= val <= 10.0:
                        return val
                except ValueError:
                    pass
        return None

    def to_dict(self) -> dict:
        return {
            "exercise": self.exercise,
            "date": self.date,
            "week": self.week,
            "prescribed_weight": self.prescribed_weight,
            "actual_weight": self.actual_weight,
            "weight_source": self.weight_source,
            "sets": self.sets,
            "reps": self.reps,
            "sets_reps_source": self.sets_reps_source,
            "done": self.done,
            "notes": self.notes,
            "rpe": self.rpe,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Health log entry cleaner
# ---------------------------------------------------------------------------

class CleanHealthEntry:
    """
    A cleaned health log entry with data quality annotations.
    """
    __slots__ = (
        "raw", "date", "week",
        "sleep_hrs", "bodyweight_kg", "hrv_ms", "resting_hr",
        "steps", "food_quality", "mood", "stress",
        "notes", "warnings",
    )

    # Expected numeric fields and their plausible ranges
    _FIELDS: dict[str, tuple] = {
        "Sleep (hrs)":        (3.0, 12.0, "sleep_hrs"),
        "Bodyweight (kg)":    (40.0, 150.0, "bodyweight_kg"),
        "HRV (ms)":           (10, 200, "hrv_ms"),
        "Resting HR (bpm)":   (30, 120, "resting_hr"),
        "Steps":              (0, 100000, "steps"),
        "Food Quality (1-10)":(1, 10, "food_quality"),
        "Mood (1-10)":        (1, 10, "mood"),
        "Stress (1-10)":      (1, 10, "stress"),
    }

    def __init__(self, raw: dict):
        self.raw = raw
        self.warnings: list[str] = []
        self._parse()

    def _parse(self):
        date_raw = self.raw.get("Date") or self.raw.get("date") or ""
        self.date = str(date_raw)[:10] if len(str(date_raw)) >= 10 else ""
        self.week = self._infer_week()
        self.notes = self.raw.get("Notes") or self.raw.get("notes") or ""

        # Parse and validate each numeric field
        for sheet_key, (lo, hi, attr) in self._FIELDS.items():
            raw_val = self.raw.get(sheet_key) or self.raw.get(attr) or ""
            parsed = self._parse_numeric(raw_val, lo, hi, sheet_key)
            setattr(self, attr, parsed)

    def _parse_numeric(self, raw_val: str, lo: float, hi: float, field_name: str) -> Optional[float]:
        if not raw_val or str(raw_val).strip() in ("", "-", "N/A", "n/a"):
            return None
        try:
            val = float(str(raw_val).replace(",", ".").strip())
            if lo <= val <= hi:
                return val
            else:
                self.warnings.append(
                    f"{field_name}: value {val} outside expected range [{lo}, {hi}]"
                )
                return None
        except (ValueError, TypeError):
            return None

    def _infer_week(self) -> Optional[str]:
        try:
            d = date.fromisoformat(str(self.date)[:10])
            y, w, _ = d.isocalendar()
            return f"{y}-W{w:02d}"
        except (ValueError, TypeError, AttributeError):
            return None

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "week": self.week,
            "sleep_hrs": self.sleep_hrs,
            "bodyweight_kg": self.bodyweight_kg,
            "hrv_ms": self.hrv_ms,
            "resting_hr": self.resting_hr,
            "steps": self.steps,
            "food_quality": self.food_quality,
            "mood": self.mood,
            "stress": self.stress,
            "notes": self.notes,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# DataStore — main class
# ---------------------------------------------------------------------------

class DataStore:
    """
    Cleans and stores lift history + health log for analysis.

    Usage:
        store = DataStore(lift_history, health_log)
        store.build()
        clean_lifts = store.get_clean_lifts()
        clean_health = store.get_clean_health()
        report = store.get_data_quality_report()
        questions = store.get_clarification_requests()
    """

    def __init__(self, lift_history: list, health_log: list):
        self._raw_lifts = lift_history or []
        self._raw_health = health_log or []
        self._clean_lifts: list[CleanLiftEntry] = []
        self._clean_health: list[CleanHealthEntry] = []
        self._built = False

    def build(self) -> "DataStore":
        """Process all raw data. Returns self for chaining."""
        self._clean_lifts = [CleanLiftEntry(r) for r in self._raw_lifts if r]
        self._clean_health = [CleanHealthEntry(r) for r in self._raw_health if r]
        self._built = True
        return self

    def get_clean_lifts(self) -> list[dict]:
        """Return cleaned lift entries as list of dicts."""
        self._ensure_built()
        return [e.to_dict() for e in self._clean_lifts]

    def get_clean_health(self) -> list[dict]:
        """Return cleaned health entries as list of dicts."""
        self._ensure_built()
        return [e.to_dict() for e in self._clean_health]

    def get_data_quality_report(self) -> dict:
        """
        DS-style data quality report. Reports:
        - Total entries
        - Missing actual weights (count + rate)
        - Missing sets/reps (count + rate)
        - Missing health fields per column
        - Exercises with no actual data ever
        - Date gaps > 7 days in health log
        """
        self._ensure_built()

        # Lift quality
        total_lifts = len(self._clean_lifts)
        missing_actual = sum(
            1 for e in self._clean_lifts
            if e.weight_source in ("missing", "inferred_from_prescribed")
        )
        inferred = sum(
            1 for e in self._clean_lifts
            if e.weight_source == "inferred_from_prescribed"
        )
        missing_sr = sum(1 for e in self._clean_lifts if e.sets_reps_source == "missing")
        missing_date = sum(1 for e in self._clean_lifts if not e.date)

        # Exercises with zero actual data (weight_source never "actual")
        actual_by_exercise: dict = {}
        for e in self._clean_lifts:
            actual_by_exercise.setdefault(e.exercise, {"actual": 0, "total": 0})
            actual_by_exercise[e.exercise]["total"] += 1
            if e.weight_source == "actual":
                actual_by_exercise[e.exercise]["actual"] += 1

        no_actual_exercises = [
            ex for ex, stats in actual_by_exercise.items()
            if stats["actual"] == 0 and stats["total"] >= 3
        ]

        # Health quality
        total_health = len(self._clean_health)
        health_field_coverage: dict = {}
        for field_attr in ("sleep_hrs", "bodyweight_kg", "hrv_ms", "resting_hr",
                           "steps", "food_quality", "mood", "stress"):
            present = sum(1 for e in self._clean_health if getattr(e, field_attr) is not None)
            health_field_coverage[field_attr] = {
                "present": present,
                "missing": total_health - present,
                "coverage_pct": round(present / total_health * 100, 1) if total_health > 0 else 0,
            }

        # Date gaps in health log
        sorted_health_dates = sorted(
            [e.date for e in self._clean_health if e.date],
            key=lambda d: d
        )
        date_gaps = []
        for i in range(1, len(sorted_health_dates)):
            try:
                d1 = date.fromisoformat(sorted_health_dates[i - 1])
                d2 = date.fromisoformat(sorted_health_dates[i])
                gap_days = (d2 - d1).days
                if gap_days > 7:
                    date_gaps.append({
                        "from": sorted_health_dates[i - 1],
                        "to": sorted_health_dates[i],
                        "gap_days": gap_days,
                    })
            except ValueError:
                pass

        return {
            "lift_quality": {
                "total_entries": total_lifts,
                "missing_actual_weight": {
                    "count": missing_actual,
                    "rate_pct": round(missing_actual / total_lifts * 100, 1) if total_lifts else 0,
                    "inferred_from_prescribed": inferred,
                    "truly_missing": missing_actual - inferred,
                },
                "missing_sets_reps": {
                    "count": missing_sr,
                    "rate_pct": round(missing_sr / total_lifts * 100, 1) if total_lifts else 0,
                },
                "missing_date": missing_date,
                "exercises_with_no_actual_data": no_actual_exercises,
            },
            "health_quality": {
                "total_entries": total_health,
                "field_coverage": health_field_coverage,
                "date_gaps_over_7d": date_gaps,
            },
        }

    def get_clarification_requests(self, max_requests: int = 5) -> list[dict]:
        """
        Generate actionable clarification requests for missing critical data.
        Prioritizes: missing actual weights on completed sessions, missing dates.

        Returns list of {type, exercise, date, week, question, priority}
        """
        self._ensure_built()

        requests: list[dict] = []

        # Flag completed sessions with no actual weight and no inference
        for e in self._clean_lifts:
            if len(requests) >= max_requests * 2:  # collect more, then prioritize
                break
            if e.done and e.weight_source == "missing":
                requests.append({
                    "type": "missing_actual_weight",
                    "exercise": e.exercise,
                    "date": e.date,
                    "week": e.week,
                    "prescribed_weight": e.prescribed_weight,
                    "question": (
                        f"Week {e.week} ({e.date}), {e.exercise}: "
                        f"session logged as done but no actual weight recorded. "
                        f"{'Prescribed was ' + str(e.prescribed_weight) + 'kg — did you complete it?' if e.prescribed_weight else 'What weight did you use?'}"
                    ),
                    "priority": "high",
                })

        # Flag sessions marked done but suspicious weight change
        exercise_weights: dict = {}
        for e in self._clean_lifts:
            if e.actual_weight and e.exercise and e.week:
                exercise_weights.setdefault(e.exercise, []).append(
                    (e.week, e.actual_weight)
                )

        for exercise, weekly_weights in exercise_weights.items():
            weekly_weights.sort(key=lambda x: x[0])
            for i in range(1, len(weekly_weights)):
                prev_wk, prev_w = weekly_weights[i - 1]
                curr_wk, curr_w = weekly_weights[i]
                if prev_w > 0:
                    change_pct = abs(curr_w - prev_w) / prev_w * 100
                    if change_pct > 20:  # >20% jump is suspicious
                        requests.append({
                            "type": "suspicious_weight_change",
                            "exercise": exercise,
                            "date": curr_wk,
                            "week": curr_wk,
                            "prev_weight": prev_w,
                            "curr_weight": curr_w,
                            "question": (
                                f"{exercise}: weight jumped from {prev_w}kg ({prev_wk}) to "
                                f"{curr_w}kg ({curr_wk}) — {change_pct:.0f}% change. "
                                f"Was this correct, or was one of these a different variation?"
                            ),
                            "priority": "medium",
                        })

        # Sort by priority and limit
        priority_order = {"high": 0, "medium": 1, "low": 2}
        requests.sort(key=lambda r: priority_order.get(r["priority"], 99))
        return requests[:max_requests]

    def get_analysis_ready_lifts(self) -> list[dict]:
        """
        Return lift entries ready for analysis:
        - Exercise name normalized
        - Weight = actual if available, else prescribed (marked as inferred)
        - Sets/reps present
        - Date and week present
        - e1RM computed where possible (reps ≤ 6)
        """
        self._ensure_built()
        results = []
        for e in self._clean_lifts:
            weight = e.actual_weight
            if not weight or not e.sets or not e.reps or not e.date:
                continue

            entry = e.to_dict()

            # Compute e1RM if applicable
            if e.reps and 1 <= e.reps <= 6 and weight:
                e1rm = weight * (1 + e.reps / 30)
                entry["e1rm"] = round(e1rm, 1)
            else:
                entry["e1rm"] = None

            results.append(entry)

        return results

    def get_weekly_summary(self) -> dict:
        """
        Aggregate clean lift data by week and exercise.
        Returns: {week_key: {exercise: {max_weight, sets, tonnage, e1rm_max, sessions}}}
        """
        self._ensure_built()
        summary: dict = {}

        for e in self._clean_lifts:
            if not e.week or not e.exercise or not e.actual_weight:
                continue
            weight = e.actual_weight
            sets = e.sets or 0
            reps = e.reps or 0
            tonnage = weight * sets * reps

            summary.setdefault(e.week, {})
            summary[e.week].setdefault(e.exercise, {
                "max_weight": 0.0,
                "total_sets": 0,
                "total_tonnage": 0.0,
                "e1rm_max": None,
                "sessions": 0,
            })

            ex = summary[e.week][e.exercise]
            ex["max_weight"] = max(ex["max_weight"], weight)
            ex["total_sets"] += sets
            ex["total_tonnage"] += tonnage
            ex["sessions"] += 1

            if reps and 1 <= reps <= 6:
                e1rm = weight * (1 + reps / 30)
                if ex["e1rm_max"] is None or e1rm > ex["e1rm_max"]:
                    ex["e1rm_max"] = round(e1rm, 1)

        return summary

    def _ensure_built(self):
        if not self._built:
            self.build()

    def get_quality_summary_text(self) -> str:
        """
        Return a brief text summary of data quality for prompt injection.
        """
        report = self.get_data_quality_report()
        lq = report["lift_quality"]
        hq = report["health_quality"]

        lines = [
            f"Data quality: {lq['total_entries']} lift entries, "
            f"{lq['missing_actual_weight']['count']} without actual weight "
            f"({lq['missing_actual_weight']['inferred_from_prescribed']} inferred from prescribed)."
        ]

        no_actual = lq.get("exercises_with_no_actual_data", [])
        if no_actual:
            lines.append(
                f"  Exercises with zero actual weight logged: {', '.join(no_actual[:5])}."
            )

        sleep_cov = hq["field_coverage"].get("sleep_hrs", {}).get("coverage_pct", 0)
        bw_cov = hq["field_coverage"].get("bodyweight_kg", {}).get("coverage_pct", 0)
        lines.append(
            f"  Health log: sleep {sleep_cov:.0f}% coverage, "
            f"bodyweight {bw_cov:.0f}% coverage."
        )

        gaps = hq.get("date_gaps_over_7d", [])
        if gaps:
            lines.append(
                f"  Health log gaps > 7 days: {len(gaps)} gap(s). "
                f"Largest: {max(g['gap_days'] for g in gaps)} days."
            )

        return "\n".join(lines)
