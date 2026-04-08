"""
garmin_sync.py — Pull yesterday's Garmin data and upsert into health_log.

IMPORTANT: The garminconnect library uses Garmin's unofficial API.
It works, but Garmin can break it at any time. If this script suddenly
stops working, check for a new version:
    pip install --upgrade "garminconnect>=0.2.22"
If the API changed fundamentally, see: https://github.com/cyberjunky/python-garminconnect

Credentials (environment variables):
    GARMIN_EMAIL      your Garmin Connect email
    GARMIN_PASSWORD   your Garmin Connect password

Behavior:
  - Pulls data for yesterday (or --date YYYY-MM-DD)
  - Does NOT overwrite existing manual entries for the same date
  - If credentials are missing or auth fails: prints error, exits 0
    (so the GitHub Action doesn't spam failure notifications)
  - GARMIN_MOCK=1 returns synthetic data without hitting the API

Usage:
    python scripts/garmin_sync.py
    python scripts/garmin_sync.py --date 2026-04-07
    python scripts/garmin_sync.py --days 7         # backfill last 7 days
    GARMIN_MOCK=1 python scripts/garmin_sync.py    # test without credentials
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "coach.db")


# ---------------------------------------------------------------------------
# Mock data (for testing without live credentials)
# ---------------------------------------------------------------------------

def _mock_data(target_date: date) -> dict:
    return {
        "date":          str(target_date),
        "steps":         8200,
        "sleep_hours":   7.1,
        "resting_hr":    53,
        "hrv":           58.0,
    }


# ---------------------------------------------------------------------------
# Garmin fetch
# ---------------------------------------------------------------------------

def _fetch_garmin(target_date: date, email: str, password: str) -> dict | None:
    """
    Fetch daily metrics from Garmin Connect for target_date.
    Returns a dict or None on any failure.

    The garminconnect library's API surface:
      client.get_stats(datestr)     → daily steps, calories, etc.
      client.get_sleep_data(datestr)→ sleep duration + score
      client.get_heart_rates(datestr)→ resting HR
      client.get_hrv_data(datestr)  → HRV (weekly avg in ms)

    All calls are wrapped in try/except — the sync never crashes the pipeline.
    """
    try:
        import garminconnect
    except ImportError:
        print("ERROR: garminconnect not installed. Run: pip install 'garminconnect>=0.2.22'")
        return None

    datestr = str(target_date)

    try:
        client = garminconnect.Garmin(email, password)
        client.login()
    except Exception as e:
        print(f"Garmin auth failed: {e}")
        return None

    result = {"date": datestr}

    # Steps
    try:
        stats = client.get_stats(datestr)
        result["steps"] = stats.get("totalSteps")
    except Exception:
        pass

    # Sleep
    try:
        sleep = client.get_sleep_data(datestr)
        daily = sleep.get("dailySleepDTO", {})
        secs  = daily.get("sleepTimeSeconds")
        if secs:
            result["sleep_hours"] = round(secs / 3600, 2)
    except Exception:
        pass

    # Resting HR
    try:
        hr_data = client.get_heart_rates(datestr)
        result["resting_hr"] = hr_data.get("restingHeartRate")
    except Exception:
        pass

    # HRV (Garmin exposes last-night HRV via get_hrv_data on some devices)
    try:
        hrv_data = client.get_hrv_data(datestr)
        weekly   = hrv_data.get("hrvSummary", {})
        last_night = weekly.get("lastNight")  # ms, may be None
        if last_night:
            result["hrv"] = float(last_night)
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# DB upsert — does NOT overwrite manual entries
# ---------------------------------------------------------------------------

def _upsert(conn: sqlite3.Connection, data: dict) -> bool:
    """
    Insert Garmin data for data['date'].
    Returns True if a row was inserted or updated.

    Manual entries (source='manual') are never overwritten.
    """
    target_date = data["date"]

    existing = conn.execute(
        "SELECT source FROM health_log WHERE log_date=?", (target_date,)
    ).fetchone()

    if existing and existing[0] == "manual":
        print(f"  {target_date}: manual entry exists — skipping (will not overwrite)")
        return False

    if existing:
        # Update Garmin entry (Garmin can backfill with corrected data)
        conn.execute("""
            UPDATE health_log SET
              steps       = COALESCE(?, steps),
              sleep_hours = COALESCE(?, sleep_hours),
              resting_hr  = COALESCE(?, resting_hr),
              hrv         = COALESCE(?, hrv),
              source      = 'garmin'
            WHERE log_date = ?
        """, (
            data.get("steps"), data.get("sleep_hours"),
            data.get("resting_hr"), data.get("hrv"),
            target_date,
        ))
    else:
        conn.execute("""
            INSERT INTO health_log
              (log_date, steps, sleep_hours, resting_hr, hrv, source)
            VALUES (?, ?, ?, ?, ?, 'garmin')
        """, (
            target_date,
            data.get("steps"), data.get("sleep_hours"),
            data.get("resting_hr"), data.get("hrv"),
        ))

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Garmin data into coach.db")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--days", type=int, default=1, help="Sync last N days (default: 1)")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    db_path = os.path.abspath(args.db_path)
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}. Run: python scripts/init_db.py")
        sys.exit(0)  # exit 0 so GH Actions doesn't spam

    email    = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    mock     = os.environ.get("GARMIN_MOCK", "").strip() in ("1", "true", "yes")

    if not mock and (not email or not password):
        print("Garmin credentials not set (GARMIN_EMAIL / GARMIN_PASSWORD). Skipping sync.")
        sys.exit(0)

    # Build list of dates to sync
    if args.date:
        from datetime import date as date_type
        try:
            start = date_type.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: invalid date '{args.date}'")
            sys.exit(1)
        dates = [start]
    else:
        yesterday = date.today() - timedelta(days=1)
        dates = [yesterday - timedelta(days=i) for i in range(args.days)]

    conn = sqlite3.connect(db_path)
    inserted = 0
    try:
        for target_date in dates:
            print(f"Syncing {target_date}...")
            if mock:
                data = _mock_data(target_date)
            else:
                data = _fetch_garmin(target_date, email, password)

            if data is None:
                print(f"  No data returned for {target_date}.")
                continue

            changed = _upsert(conn, data)
            if changed:
                print(f"  Saved: steps={data.get('steps')}, "
                      f"sleep={data.get('sleep_hours')}h, "
                      f"rhr={data.get('resting_hr')}bpm, "
                      f"hrv={data.get('hrv')}ms")
                inserted += 1

        conn.commit()
    finally:
        conn.close()

    print(f"Done. {inserted} row(s) written.")


if __name__ == "__main__":
    main()
