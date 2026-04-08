"""
cardio_zones.py — Garmin Cardio Zone Analytics + Orchestrator

Fetches cardio-only activities from Garmin Connect, extracts HR zone
distribution, VO2max trend, and tracks compliance against a prescription.

KEY DESIGN DECISION: We explicitly EXCLUDE strength training sessions
from zone analysis. Lifting sets have HR spikes that don't represent
aerobic conditioning work. Only activities classified as cardio/aerobic
are included.

Cardio session filter:
  Included: running, cycling, rowing, swimming, walking (brisk), elliptical,
            HIIT (when cardio-focused), hiking
  Excluded: strength_training, weight_training, lap_swimming (if short),
            any activity under 10 minutes

HR Zone definitions (5-zone model, % of max HR):
  Zone 1 (Recovery):   50-60% HRmax — easy, recovery pace
  Zone 2 (Aerobic):    60-70% HRmax — fat burning, aerobic base
  Zone 3 (Tempo):      70-80% HRmax — aerobic/anaerobic crossover
  Zone 4 (Threshold):  80-90% HRmax — lactate threshold
  Zone 5 (VO2max):     90-100% HRmax — max effort

Zone 2 is most important for Nacho's health goals (aerobic base + insulin resistance).

CardioOrchestrator:
  set_prescription(zone2_mins_per_week=90, sessions_per_week=3)
  compute_compliance(actual_distribution, prescription) → compliance %
  generate_cardio_summary_for_prompt() → text block
  run_cardio_analysis(days=30) → writes CARDIO_ZONES domain

Usage:
  from cardio_zones import CardioOrchestrator
  orchestrator = CardioOrchestrator()
  summary = orchestrator.run_cardio_analysis(days=21)
"""

import json
import os
from datetime import date, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Activity type filter — what counts as cardio
# ---------------------------------------------------------------------------

CARDIO_ACTIVITY_TYPES = {
    # Garmin activity type strings (lowercase)
    "running", "run", "trail_running", "treadmill_running",
    "cycling", "indoor_cycling", "road_biking", "mountain_biking",
    "rowing", "indoor_rowing",
    "swimming", "pool_swimming", "open_water_swimming",
    "walking", "hiking",
    "elliptical",
    "cardio", "aerobic_workout", "hiit",
    "resort_skiing_snowboarding",  # still aerobic
    "inline_skating",
    "kayaking", "stand_up_paddleboarding",
}

# Activity types that are NOT cardio and should be excluded
STRENGTH_ACTIVITY_TYPES = {
    "strength_training", "weight_training", "yoga", "pilates",
    "flexibility", "breathwork", "meditation",
    "bouldering", "rock_climbing",
}

MIN_SESSION_MINUTES = 10  # sessions shorter than this are ignored


# ---------------------------------------------------------------------------
# Zone definitions
# ---------------------------------------------------------------------------

ZONE_DEFINITIONS = {
    1: {"name": "Recovery",  "min_pct": 50, "max_pct": 60, "label": "Zone 1 (50-60%)"},
    2: {"name": "Aerobic",   "min_pct": 60, "max_pct": 70, "label": "Zone 2 (60-70%)"},
    3: {"name": "Tempo",     "min_pct": 70, "max_pct": 80, "label": "Zone 3 (70-80%)"},
    4: {"name": "Threshold", "min_pct": 80, "max_pct": 90, "label": "Zone 4 (80-90%)"},
    5: {"name": "VO2max",    "min_pct": 90, "max_pct": 100, "label": "Zone 5 (90-100%)"},
}


# ---------------------------------------------------------------------------
# Mock data for testing
# ---------------------------------------------------------------------------

def _build_mock_sessions(days: int = 21) -> list[dict]:
    """Return plausible fake cardio session data for testing."""
    today = date.today()
    sessions = []
    # Simulate 3 sessions/week
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        if d.weekday() in (1, 3, 6):  # Tue, Thu, Sun
            sessions.append({
                "date": str(d),
                "activity_type": "running" if d.weekday() != 6 else "cycling",
                "duration_mins": 35 if d.weekday() != 6 else 45,
                "avg_hr": 145 if d.weekday() != 6 else 130,
                "max_hr": 168 if d.weekday() != 6 else 152,
                "calories": 380,
                "distance_km": 5.2 if d.weekday() != 6 else 18.0,
                "zone_minutes": {
                    "zone1": 5,
                    "zone2": 18 if d.weekday() != 6 else 25,
                    "zone3": 8,
                    "zone4": 3,
                    "zone5": 1,
                },
                "vo2max": 48.5,
            })
    return sessions


# ---------------------------------------------------------------------------
# GarminCardioClient — extends garmin.GarminClient for cardio activity data
# ---------------------------------------------------------------------------

class GarminCardioClient:
    """
    Fetches cardio activity data from Garmin Connect.
    Separates cardio sessions from strength sessions.

    Uses GARMIN_EMAIL + GARMIN_PASSWORD env vars.
    Set GARMIN_MOCK=1 for testing.
    """

    def __init__(self):
        self._email = os.environ.get("GARMIN_EMAIL", "")
        self._password = os.environ.get("GARMIN_PASSWORD", "")
        self._mock = os.environ.get("GARMIN_MOCK", "").strip() in ("1", "true", "yes")
        self._client = None

    def is_available(self) -> bool:
        if self._mock:
            return True
        if not self._email or not self._password:
            return False
        return self._login()

    def _login(self) -> bool:
        if self._client is not None:
            return True
        try:
            import garminconnect
            client = garminconnect.Garmin(self._email, self._password)
            client.login()
            self._client = client
            return True
        except ImportError:
            print("  [GarminCardio] garminconnect not installed.")
            return False
        except Exception as e:
            print(f"  [GarminCardio] Login failed: {e}")
            return False

    def fetch_cardio_sessions(self, days: int = 21) -> list[dict]:
        """
        Fetch cardio sessions for the last N days.
        Filters out strength training sessions.
        Returns list of session dicts.
        """
        if self._mock:
            return _build_mock_sessions(days)

        if not self._login():
            return []

        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        try:
            activities = self._client.get_activities_by_date(
                str(start_date), str(end_date)
            )
        except Exception as e:
            print(f"  [GarminCardio] Activity fetch failed: {e}")
            return []

        sessions = []
        for act in activities:
            session = self._parse_activity(act)
            if session:
                sessions.append(session)

        return sorted(sessions, key=lambda s: s["date"])

    def _parse_activity(self, activity: dict) -> Optional[dict]:
        """
        Parse a raw Garmin activity dict into our clean session format.
        Returns None if activity should be excluded.
        """
        try:
            # Activity type classification
            act_type = (
                activity.get("activityType", {}).get("typeKey", "") or
                activity.get("activityTypeName", "") or ""
            ).lower().replace(" ", "_")

            # Exclude strength sessions
            if act_type in STRENGTH_ACTIVITY_TYPES:
                return None

            # Only include known cardio types (or unknown — default include)
            if act_type and act_type not in CARDIO_ACTIVITY_TYPES:
                # Unknown activity type — include cautiously if duration is reasonable
                pass

            # Duration check
            duration_secs = activity.get("duration", 0) or 0
            duration_mins = round(duration_secs / 60, 1)
            if duration_mins < MIN_SESSION_MINUTES:
                return None

            # Date
            start_time = activity.get("startTimeLocal", "") or ""
            session_date = start_time[:10] if len(start_time) >= 10 else str(date.today())

            # HR data
            avg_hr = activity.get("averageHR") or activity.get("avgHr") or None
            max_hr = activity.get("maxHR") or activity.get("maxHr") or None

            # Zone minutes — Garmin provides this in heartRateZones
            zone_minutes = self._extract_zone_minutes(activity)

            # VO2max
            vo2max = activity.get("vO2MaxValue") or activity.get("vo2Max") or None

            # Distance
            dist_raw = activity.get("distance", 0) or 0
            distance_km = round(dist_raw / 1000, 2) if dist_raw > 100 else round(float(dist_raw), 2)

            return {
                "date": session_date,
                "activity_type": act_type,
                "duration_mins": duration_mins,
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "calories": activity.get("calories"),
                "distance_km": distance_km if distance_km > 0 else None,
                "zone_minutes": zone_minutes,
                "vo2max": float(vo2max) if vo2max else None,
            }

        except Exception as e:
            print(f"  [GarminCardio] Activity parse error (non-fatal): {e}")
            return None

    def _extract_zone_minutes(self, activity: dict) -> dict:
        """
        Extract HR zone minutes from a Garmin activity.
        Falls back to estimating from avg/max HR if zone data isn't available.
        """
        zones = activity.get("heartRateZones", []) or []

        zone_minutes: dict = {
            "zone1": 0, "zone2": 0, "zone3": 0, "zone4": 0, "zone5": 0
        }

        if zones:
            for z in zones:
                zone_num = z.get("zoneNumber") or z.get("seqNum")
                secs = z.get("secsInZone") or z.get("secondsInZone") or 0
                mins = round(secs / 60, 1)
                if zone_num and 1 <= zone_num <= 5:
                    zone_minutes[f"zone{zone_num}"] = mins

        return zone_minutes


# ---------------------------------------------------------------------------
# Zone distribution computation
# ---------------------------------------------------------------------------

def compute_zone_distribution(sessions: list[dict]) -> dict:
    """
    Aggregate zone minutes across sessions into weekly distribution.

    Returns:
    {
        "total_sessions": N,
        "total_cardio_mins": float,
        "weekly_zone_averages": {zone1..5: avg_mins_per_week},
        "total_zone_minutes": {zone1..5: total_mins},
        "zone2_pct": float (% of total time in Zone 2),
        "vo2max_latest": float | None,
        "vo2max_trend": "improving" | "stable" | "declining" | None,
        "sessions_by_type": {type: count},
        "date_range": {"from": str, "to": str},
    }
    """
    if not sessions:
        return {
            "total_sessions": 0,
            "total_cardio_mins": 0.0,
            "weekly_zone_averages": {f"zone{i}": 0.0 for i in range(1, 6)},
            "total_zone_minutes": {f"zone{i}": 0.0 for i in range(1, 6)},
            "zone2_pct": 0.0,
            "vo2max_latest": None,
            "vo2max_trend": None,
            "sessions_by_type": {},
            "date_range": {"from": None, "to": None},
        }

    total_zone: dict = {f"zone{i}": 0.0 for i in range(1, 6)}
    total_mins = 0.0
    sessions_by_type: dict = {}
    vo2max_values: list = []
    dates = []

    for s in sessions:
        total_mins += s.get("duration_mins", 0) or 0

        for i in range(1, 6):
            zk = f"zone{i}"
            total_zone[zk] += s.get("zone_minutes", {}).get(zk, 0) or 0

        act_type = s.get("activity_type", "unknown")
        sessions_by_type[act_type] = sessions_by_type.get(act_type, 0) + 1

        if s.get("vo2max"):
            vo2max_values.append((s["date"], s["vo2max"]))

        if s.get("date"):
            dates.append(s["date"])

    # Weeks spanned
    if dates:
        date_range_days = (
            (date.fromisoformat(max(dates)) - date.fromisoformat(min(dates))).days + 1
        )
        weeks_spanned = max(1, date_range_days / 7)
    else:
        weeks_spanned = 1

    weekly_avgs = {k: round(v / weeks_spanned, 1) for k, v in total_zone.items()}

    # Zone 2 percentage of total zone time
    total_zone_mins = sum(total_zone.values())
    zone2_pct = round(total_zone["zone2"] / total_zone_mins * 100, 1) if total_zone_mins > 0 else 0.0

    # VO2max trend
    vo2max_latest = None
    vo2max_trend = None
    if vo2max_values:
        vo2max_values.sort(key=lambda x: x[0])
        vo2max_latest = vo2max_values[-1][1]
        if len(vo2max_values) >= 3:
            first_half = [v for _, v in vo2max_values[:len(vo2max_values)//2]]
            second_half = [v for _, v in vo2max_values[len(vo2max_values)//2:]]
            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)
            if avg_second > avg_first + 0.5:
                vo2max_trend = "improving"
            elif avg_second < avg_first - 0.5:
                vo2max_trend = "declining"
            else:
                vo2max_trend = "stable"

    return {
        "total_sessions": len(sessions),
        "total_cardio_mins": round(total_mins, 1),
        "weekly_zone_averages": weekly_avgs,
        "total_zone_minutes": {k: round(v, 1) for k, v in total_zone.items()},
        "zone2_pct": zone2_pct,
        "vo2max_latest": vo2max_latest,
        "vo2max_trend": vo2max_trend,
        "sessions_by_type": sessions_by_type,
        "date_range": {
            "from": min(dates) if dates else None,
            "to": max(dates) if dates else None,
        },
    }


# ---------------------------------------------------------------------------
# CardioOrchestrator
# ---------------------------------------------------------------------------

class CardioOrchestrator:
    """
    Prescribes cardio zones, tracks compliance, integrates with health readiness.

    Default prescription for Nacho:
      - Zone 2: 90 mins/week (aerobic base + insulin resistance management)
      - Zone 3-4: 20 mins/week (aerobic fitness stimulus)
      - Sessions: 3x/week minimum

    These are soft targets — health readiness may adjust them down.
    """

    DEFAULT_PRESCRIPTION = {
        "zone2_mins_per_week": 90,
        "zone3_4_mins_per_week": 20,
        "sessions_per_week": 3,
        "min_session_mins": 20,
        "notes": "Zone 2 priority for aerobic base and insulin resistance management.",
    }

    def __init__(self, prescription: dict = None):
        self._prescription = prescription or self.DEFAULT_PRESCRIPTION
        self._client = GarminCardioClient()

    def set_prescription(
        self,
        zone2_mins_per_week: int = 90,
        zone3_4_mins_per_week: int = 20,
        sessions_per_week: int = 3,
        min_session_mins: int = 20,
        notes: str = "",
    ) -> "CardioOrchestrator":
        """Update the cardio prescription. Returns self for chaining."""
        self._prescription = {
            "zone2_mins_per_week": zone2_mins_per_week,
            "zone3_4_mins_per_week": zone3_4_mins_per_week,
            "sessions_per_week": sessions_per_week,
            "min_session_mins": min_session_mins,
            "notes": notes,
        }
        return self

    def compute_compliance(self, distribution: dict, weeks: float = 3.0) -> dict:
        """
        Compute compliance against prescription.

        distribution: output of compute_zone_distribution()
        weeks: number of weeks the distribution covers

        Returns compliance dict with rates and flags.
        """
        presc = self._prescription
        weekly_avgs = distribution.get("weekly_zone_averages", {})

        zone2_actual = weekly_avgs.get("zone2", 0)
        zone3_4_actual = (
            weekly_avgs.get("zone3", 0) + weekly_avgs.get("zone4", 0)
        )
        sessions_actual = distribution.get("total_sessions", 0) / max(weeks, 1)

        zone2_target = presc["zone2_mins_per_week"]
        zone3_4_target = presc["zone3_4_mins_per_week"]
        sessions_target = presc["sessions_per_week"]

        zone2_compliance = min(100.0, zone2_actual / zone2_target * 100) if zone2_target else 100.0
        zone3_4_compliance = min(100.0, zone3_4_actual / zone3_4_target * 100) if zone3_4_target else 100.0
        sessions_compliance = min(100.0, sessions_actual / sessions_target * 100) if sessions_target else 100.0

        overall = (zone2_compliance * 0.5 + zone3_4_compliance * 0.25 + sessions_compliance * 0.25)

        flags = []
        if zone2_compliance < 50:
            flags.append("zone2_deficit_critical")
        elif zone2_compliance < 75:
            flags.append("zone2_deficit_moderate")
        if sessions_compliance < 50:
            flags.append("sessions_too_few")
        if distribution.get("zone2_pct", 0) < 40:
            flags.append("spending_too_much_time_above_zone2")

        return {
            "zone2_compliance_pct": round(zone2_compliance, 1),
            "zone3_4_compliance_pct": round(zone3_4_compliance, 1),
            "sessions_compliance_pct": round(sessions_compliance, 1),
            "overall_compliance_pct": round(overall, 1),
            "zone2_actual_per_week": round(zone2_actual, 1),
            "zone2_target_per_week": zone2_target,
            "sessions_actual_per_week": round(sessions_actual, 1),
            "sessions_target_per_week": sessions_target,
            "flags": flags,
        }

    def generate_cardio_summary_for_prompt(
        self,
        distribution: dict,
        compliance: dict,
    ) -> str:
        """
        Generate a concise cardio summary for prompt injection.
        Coach reads this alongside health readiness to prescribe or comment on cardio.
        """
        if not distribution or distribution.get("total_sessions", 0) == 0:
            return "Cardio: no sessions recorded in analysis window."

        lines = [
            f"Cardio zones ({distribution['date_range']['from']} to {distribution['date_range']['to']}):"
        ]

        weekly = distribution["weekly_zone_averages"]
        lines.append(
            f"  Zone 2 (aerobic): {weekly.get('zone2', 0):.0f} min/wk "
            f"(target: {self._prescription['zone2_mins_per_week']} min) "
            f"— compliance {compliance['zone2_compliance_pct']:.0f}%"
        )
        lines.append(
            f"  Zone 3-4 (tempo/threshold): {(weekly.get('zone3',0)+weekly.get('zone4',0)):.0f} min/wk"
        )
        lines.append(
            f"  Zone 5 (VO2max): {weekly.get('zone5', 0):.0f} min/wk"
        )
        lines.append(
            f"  Sessions: {compliance['sessions_actual_per_week']:.1f}/wk "
            f"(target {compliance['sessions_target_per_week']}/wk)"
        )

        if distribution.get("vo2max_latest"):
            trend = distribution.get("vo2max_trend", "unknown")
            lines.append(
                f"  VO2max: {distribution['vo2max_latest']} mL/kg/min ({trend})"
            )

        # Flags
        flags = compliance.get("flags", [])
        if "zone2_deficit_critical" in flags:
            lines.append(
                "  !! Zone 2 critically low. Insulin resistance management at risk. "
                "Add 30+ min easy sessions this week."
            )
        elif "zone2_deficit_moderate" in flags:
            lines.append(
                "  ! Zone 2 below target. Consider adding a 30-min easy walk/run."
            )
        if "spending_too_much_time_above_zone2" in flags:
            lines.append(
                "  ! Most cardio is above Zone 2 — shift some sessions to easy pace for aerobic base."
            )

        return "\n".join(lines)

    def run_cardio_analysis(
        self,
        days: int = 21,
        dry_run: bool = False,
    ) -> dict:
        """
        Main entry point. Fetches sessions, computes distribution, compliance.
        Writes CARDIO_ZONES domain to Coach State.

        Returns analysis dict.
        """
        sessions = []
        if self._client.is_available():
            sessions = self._client.fetch_cardio_sessions(days=days)
        else:
            print("  [CardioOrchestrator] Garmin not available — no cardio data.")

        weeks = max(1.0, days / 7)
        distribution = compute_zone_distribution(sessions)
        compliance = self.compute_compliance(distribution, weeks=weeks)
        summary_text = self.generate_cardio_summary_for_prompt(distribution, compliance)

        result = {
            "computed_date": str(date.today()),
            "analysis_window_days": days,
            "sessions": sessions,
            "distribution": distribution,
            "compliance": compliance,
            "prescription": self._prescription,
            "summary_text": summary_text,
        }

        if not dry_run:
            try:
                from memory import upsert_coach_state
                # Store without raw sessions (too large)
                store_data = {k: v for k, v in result.items() if k != "sessions"}
                upsert_coach_state(
                    "CARDIO_ZONES",
                    json.dumps(store_data, ensure_ascii=False, default=str),
                    "HIGH",
                )
                sessions_count = distribution.get("total_sessions", 0)
                zone2_mins = distribution.get("weekly_zone_averages", {}).get("zone2", 0)
                print(
                    f"  cardio_zones: {sessions_count} sessions analyzed, "
                    f"Zone 2 avg {zone2_mins:.0f} min/wk, "
                    f"compliance {compliance['overall_compliance_pct']:.0f}%."
                )
            except Exception as e:
                print(f"  cardio_zones: CARDIO_ZONES write failed: {e}")

        return result
