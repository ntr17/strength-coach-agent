"""
GarminClient — fetches daily recovery metrics from Garmin Connect.

Credentials: GARMIN_EMAIL + GARMIN_PASSWORD env vars.
No MFA / session pickle needed (credential-only auth).

Data fetched per day:
  - HRV (last night, milliseconds)
  - Sleep duration (hours) + sleep score (0–100 if available)
  - Resting heart rate (bpm)
  - Body battery start/end of day (0–100)
  - Steps

All fetch methods are wrapped in try/except — the coach never crashes on
Garmin failure. If Garmin is unreachable or returns bad data, the method
returns None and the sync is silently skipped.

Mock mode: set GARMIN_MOCK=1 env var to return hardcoded data for testing
without hitting the API.

Usage:
  from garmin import GarminClient
  client = GarminClient()
  if client.is_available():
      metrics = client.fetch_range(days=7)
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Mock data for testing without live credentials
# ---------------------------------------------------------------------------

def _build_mock_data(target_date: date) -> dict:
    """Return plausible fake metrics for testing."""
    return {
        "date": str(target_date),
        "hrv_ms": 52,
        "sleep_hrs": 6.8,
        "sleep_score": 72,
        "resting_hr": 54,
        "body_battery_start": 84,
        "body_battery_end": 31,
        "steps": 7400,
    }


# ---------------------------------------------------------------------------
# GarminClient
# ---------------------------------------------------------------------------

class GarminClient:
    """
    Thin wrapper around garminconnect.Garmin.
    Auth: GARMIN_EMAIL + GARMIN_PASSWORD env vars.
    """

    def __init__(self):
        self._email = os.environ.get("GARMIN_EMAIL", "")
        self._password = os.environ.get("GARMIN_PASSWORD", "")
        self._mock = os.environ.get("GARMIN_MOCK", "").strip() in ("1", "true", "yes")
        self._client = None

    def is_available(self) -> bool:
        """
        Returns True if credentials are present and a test login succeeds.
        Returns False immediately if env vars are missing.
        In mock mode always returns True.
        """
        if self._mock:
            return True
        if not self._email or not self._password:
            return False
        return self._login()

    def _login(self) -> bool:
        """Attempt login; cache client on success. Returns True if logged in."""
        if self._client is not None:
            return True
        try:
            import garminconnect  # noqa: F401 — presence check
            client = garminconnect.Garmin(self._email, self._password)
            client.login()
            self._client = client
            return True
        except ImportError:
            print("  [Garmin] garminconnect package not installed. Run: pip install garminconnect")
            return False
        except Exception as e:
            print(f"  [Garmin] Login failed (non-fatal): {e}")
            return False

    def fetch_daily_metrics(self, target_date: date) -> dict | None:
        """
        Fetch recovery metrics for a single day.

        Returns dict with keys:
          date, hrv_ms, sleep_hrs, sleep_score, resting_hr,
          body_battery_start, body_battery_end, steps
        Returns None on any error (API down, no data, etc.).

        Note: Garmin calculates overnight HRV, so today's HRV data is only
        available after the athlete wakes up. Fetching yesterday is most reliable.
        """
        if self._mock:
            return _build_mock_data(target_date)

        if not self._login():
            return None

        date_str = str(target_date)
        result = {
            "date": date_str,
            "hrv_ms": None,
            "sleep_hrs": None,
            "sleep_score": None,
            "resting_hr": None,
            "body_battery_start": None,
            "body_battery_end": None,
            "steps": None,
        }

        # --- HRV ---
        try:
            hrv_data = self._client.get_hrv_data(date_str)
            if hrv_data:
                summary = hrv_data.get("hrvSummary", {})
                # Try both key variants (watch-model dependent)
                hrv_val = summary.get("lastNight5MinHigh") or summary.get("lastNight")
                if hrv_val is not None:
                    result["hrv_ms"] = int(float(hrv_val))
        except Exception as e:
            print(f"  [Garmin] HRV fetch error for {date_str} (non-fatal): {e}")

        # --- Sleep ---
        try:
            sleep_data = self._client.get_sleep_data(date_str)
            if sleep_data:
                dto = sleep_data.get("dailySleepDTO", {})
                sleep_secs = dto.get("sleepTimeSeconds")
                if sleep_secs:
                    result["sleep_hrs"] = round(float(sleep_secs) / 3600, 1)
                score_obj = dto.get("sleepScores", {})
                if isinstance(score_obj, dict):
                    overall = score_obj.get("overall", {})
                    if isinstance(overall, dict):
                        result["sleep_score"] = overall.get("value")
        except Exception as e:
            print(f"  [Garmin] Sleep fetch error for {date_str} (non-fatal): {e}")

        # --- Daily stats (steps, resting HR) ---
        try:
            stats = self._client.get_stats(date_str)
            if stats:
                rhr = stats.get("restingHeartRate")
                if rhr:
                    result["resting_hr"] = int(rhr)
                steps = stats.get("totalSteps")
                if steps:
                    result["steps"] = int(steps)
        except Exception as e:
            print(f"  [Garmin] Stats fetch error for {date_str} (non-fatal): {e}")

        # --- Body battery ---
        try:
            bb_data = self._client.get_body_battery([date_str])
            if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
                day_bb = bb_data[0]
                charged = day_bb.get("charged")
                drained = day_bb.get("drained")
                if charged is not None:
                    result["body_battery_start"] = int(charged)
                if drained is not None:
                    # body_battery_end = charged - drained
                    try:
                        result["body_battery_end"] = max(0, int(charged) - int(drained))
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            print(f"  [Garmin] Body battery fetch error for {date_str} (non-fatal): {e}")

        # Return None only if we got absolutely no data at all
        if all(result.get(k) is None for k in ("hrv_ms", "sleep_hrs", "resting_hr", "steps")):
            return None

        return result

    def fetch_range(self, days: int = 7) -> list[dict]:
        """
        Fetch metrics for the last N days (most recent first).
        Skips dates where fetch returns None.
        Returns list of metric dicts.
        """
        today = date.today()
        results = []
        for i in range(1, days + 1):  # start from yesterday (day-1) — today's HRV not yet final
            target = today - timedelta(days=i)
            m = self.fetch_daily_metrics(target)
            if m is not None:
                results.append(m)
        return results
