"""
query_health.py — Health log queries and trend reporting.

Usage:
    python scripts/query_health.py                  # last 14 days
    python scripts/query_health.py --days 30        # last N days
    python scripts/query_health.py --injuries       # show injury log
    python scripts/query_health.py --insert         # interactive: add today's health entry
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "coach.db")


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}. Run: python scripts/init_db.py")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt(val, fmt=".1f", unit="", fallback="—"):
    if val is None:
        return fallback
    return f"{val:{fmt}}{unit}"


# ---------------------------------------------------------------------------
# Health log display
# ---------------------------------------------------------------------------

def show_health_log(conn: sqlite3.Connection, days: int = 14) -> None:
    cutoff = str(date.today() - timedelta(days=days))
    cur = conn.execute("""
        SELECT log_date, body_weight_kg, sleep_hours, sleep_quality,
               steps, resting_hr, hrv, source, notes
        FROM health_log
        WHERE log_date >= ?
        ORDER BY log_date DESC
    """, (cutoff,))
    rows = cur.fetchall()

    if not rows:
        print(f"No health data found in the last {days} days.")
        return

    print(f"\n{'─'*72}")
    print(f"Health log — last {days} days")
    print(f"{'─'*72}")
    print(f"  {'date':<12} {'bw':>6} {'sleep':>6} {'sq':>3} {'steps':>6} {'rhr':>5} {'hrv':>5}  source")
    print(f"  {'':─<12} {'':─>6} {'':─>6} {'':─>3} {'':─>6} {'':─>5} {'':─>5}")
    for r in rows:
        bw    = _fmt(r["body_weight_kg"], ".1f", "kg")
        sleep = _fmt(r["sleep_hours"], ".1f", "h")
        sq    = _fmt(r["sleep_quality"], "d")
        steps = _fmt(r["steps"], "d")
        rhr   = _fmt(r["resting_hr"], "d", "bpm")
        hrv   = _fmt(r["hrv"], ".1f", "ms")
        src   = r["source"] or "manual"
        print(f"  {r['log_date']:<12} {bw:>6} {sleep:>6} {sq:>3} {steps:>6} {rhr:>5} {hrv:>5}  {src}")
        if r["notes"]:
            print(f"  {'':12} ↳ {r['notes']}")

    # Averages
    bw_vals    = [r["body_weight_kg"] for r in rows if r["body_weight_kg"] is not None]
    sleep_vals = [r["sleep_hours"]    for r in rows if r["sleep_hours"]    is not None]
    step_vals  = [r["steps"]          for r in rows if r["steps"]          is not None]
    hrv_vals   = [r["hrv"]            for r in rows if r["hrv"]            is not None]

    print(f"\n  Averages ({len(rows)} days with data):")
    if bw_vals:
        print(f"    Body weight : {sum(bw_vals)/len(bw_vals):.1f}kg  "
              f"(min {min(bw_vals):.1f}, max {max(bw_vals):.1f})")
    if sleep_vals:
        print(f"    Sleep       : {sum(sleep_vals)/len(sleep_vals):.1f}h")
    if step_vals:
        print(f"    Steps       : {int(sum(step_vals)/len(step_vals)):,}")
    if hrv_vals:
        print(f"    HRV         : {sum(hrv_vals)/len(hrv_vals):.1f}ms")
    print()


# ---------------------------------------------------------------------------
# Injury log
# ---------------------------------------------------------------------------

def show_injuries(conn: sqlite3.Connection) -> None:
    cur = conn.execute("""
        SELECT injury_name, body_part, start_date, end_date, severity, notes
        FROM injury_log
        ORDER BY end_date IS NULL DESC, start_date DESC
    """)
    rows = cur.fetchall()

    if not rows:
        print("No injuries logged.")
        return

    print(f"\n{'─'*60}")
    print("Injury log")
    print(f"{'─'*60}")
    active = [r for r in rows if r["end_date"] is None]
    past   = [r for r in rows if r["end_date"] is not None]

    if active:
        print("  ACTIVE:")
        for r in active:
            sev = f"  severity {r['severity']}/5" if r["severity"] else ""
            print(f"    [{r['start_date']} → present]  {r['injury_name']} ({r['body_part']}){sev}")
            if r["notes"]:
                print(f"      {r['notes']}")

    if past:
        print("  PAST:")
        for r in past:
            duration = (
                (date.fromisoformat(r["end_date"]) - date.fromisoformat(r["start_date"])).days
            )
            print(f"    [{r['start_date']} → {r['end_date']}  {duration}d]  "
                  f"{r['injury_name']} ({r['body_part']})")
    print()


# ---------------------------------------------------------------------------
# Interactive manual insert
# ---------------------------------------------------------------------------

def _prompt_float(label: str) -> float | None:
    val = input(f"  {label}: ").strip()
    return float(val) if val else None


def _prompt_int(label: str) -> int | None:
    val = input(f"  {label}: ").strip()
    return int(val) if val else None


def insert_today(conn: sqlite3.Connection) -> None:
    today = str(date.today())
    print(f"\nEntering health data for {today} (press Enter to skip a field)")

    bw      = _prompt_float("Body weight (kg)")
    sleep_h = _prompt_float("Sleep hours")
    sleep_q = _prompt_int("Sleep quality (1-5)")
    steps   = _prompt_int("Steps")
    rhr     = _prompt_int("Resting HR (bpm)")
    hrv     = _prompt_float("HRV (ms)")
    notes   = input("  Notes: ").strip() or None

    try:
        conn.execute("""
            INSERT INTO health_log
              (log_date, body_weight_kg, sleep_hours, sleep_quality, steps,
               resting_hr, hrv, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?)
            ON CONFLICT(log_date) DO UPDATE SET
              body_weight_kg = COALESCE(excluded.body_weight_kg, body_weight_kg),
              sleep_hours    = COALESCE(excluded.sleep_hours,    sleep_hours),
              sleep_quality  = COALESCE(excluded.sleep_quality,  sleep_quality),
              steps          = COALESCE(excluded.steps,          steps),
              resting_hr     = COALESCE(excluded.resting_hr,     resting_hr),
              hrv            = COALESCE(excluded.hrv,            hrv),
              notes          = COALESCE(excluded.notes,          notes)
        """, (today, bw, sleep_h, sleep_q, steps, rhr, hrv, notes))
        conn.commit()
        print(f"Saved health entry for {today}.")
    except Exception as e:
        print(f"ERROR: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Query health log from coach.db")
    parser.add_argument("--days", type=int, default=14, help="Days of history to show")
    parser.add_argument("--injuries", action="store_true", help="Show injury log")
    parser.add_argument("--insert", action="store_true", help="Manually insert today's health entry")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = _connect(os.path.abspath(args.db_path))
    try:
        if args.insert:
            insert_today(conn)
        elif args.injuries:
            show_injuries(conn)
        else:
            show_health_log(conn, days=args.days)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
