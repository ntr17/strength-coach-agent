"""
query_lifts.py — Lift history queries and trend reporting.

Usage:
    python scripts/query_lifts.py                          # recent sessions summary
    python scripts/query_lifts.py --exercise bench         # history for one lift
    python scripts/query_lifts.py --exercise bench --weeks 12
    python scripts/query_lifts.py --sessions 5             # last N sessions
    python scripts/query_lifts.py --top-sets               # best sets per exercise ever
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


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def recent_sessions(conn: sqlite3.Connection, n: int = 5) -> None:
    cur = conn.execute("""
        SELECT session_date, week_number, block_number, day_number,
               COUNT(*) as total_sets,
               SUM(CASE WHEN should_count THEN 1 ELSE 0 END) as counted_sets,
               GROUP_CONCAT(DISTINCT exercise) as exercises
        FROM lift_sets
        GROUP BY session_date, day_number
        ORDER BY session_date DESC
        LIMIT ?
    """, (n,))
    rows = cur.fetchall()

    if not rows:
        print("No sessions found.")
        return

    print(f"\n{'─'*60}")
    print(f"Last {min(n, len(rows))} sessions")
    print(f"{'─'*60}")
    for r in rows:
        exs = r["exercises"].replace(",", ", ")
        print(f"  {r['session_date']}  W{r['week_number']} B{r['block_number']} D{r['day_number']}  "
              f"({r['counted_sets']}/{r['total_sets']} sets)  {exs}")
    print()


def exercise_history(
    conn: sqlite3.Connection,
    exercise_query: str,
    weeks: int = None,
) -> None:
    # Fuzzy match on exercise name
    cur = conn.execute("SELECT DISTINCT exercise FROM lift_sets")
    all_exercises = [r[0] for r in cur.fetchall()]
    matches = [e for e in all_exercises if exercise_query.lower() in e.lower()]

    if not matches:
        print(f"No exercise matching '{exercise_query}' found.")
        return

    date_filter = ""
    params: list = []
    if weeks:
        cutoff = str(date.today() - timedelta(weeks=weeks))
        date_filter = "AND session_date >= ?"
        params.append(cutoff)

    for exercise in matches:
        cur = conn.execute(f"""
            SELECT session_date, week_number, set_number, reps, weight_kg,
                   is_amrap, should_count, rpe, notes
            FROM lift_sets
            WHERE exercise = ?
            {date_filter}
            ORDER BY session_date DESC, set_number ASC
        """, [exercise] + params)
        rows = cur.fetchall()

        if not rows:
            continue

        print(f"\n{'─'*60}")
        print(f"{exercise}")
        print(f"{'─'*60}")

        current_date = None
        for r in rows:
            if r["session_date"] != current_date:
                current_date = r["session_date"]
                print(f"\n  {current_date}  (W{r['week_number']})")
                print(f"  {'set':>3}  {'reps':>4}  {'kg':>6}  {'amrap':>5}  {'count':>5}  {'rpe':>4}  notes")

            amrap = "yes" if r["is_amrap"] else "no"
            count = "yes" if r["should_count"] else "no"
            rpe   = f"{r['rpe']:.1f}" if r["rpe"] else "—"
            notes = r["notes"] or ""
            print(f"  {r['set_number']:>3}  {r['reps']:>4}  {r['weight_kg']:>6.1f}  "
                  f"{amrap:>5}  {count:>5}  {rpe:>4}  {notes}")

    print()


def top_sets(conn: sqlite3.Connection) -> None:
    """Best single set (by weight × reps) per exercise across all time."""
    cur = conn.execute("""
        SELECT exercise,
               session_date,
               reps,
               weight_kg,
               is_amrap,
               rpe,
               weight_kg * reps AS volume
        FROM lift_sets
        WHERE should_count = 1
        ORDER BY exercise, weight_kg DESC, reps DESC
    """)
    rows = cur.fetchall()

    if not rows:
        print("No counted sets found.")
        return

    seen_exercises: set[str] = set()
    best_by_exercise: list = []
    for r in rows:
        if r["exercise"] not in seen_exercises:
            seen_exercises.add(r["exercise"])
            best_by_exercise.append(r)

    print(f"\n{'─'*60}")
    print("Best sets ever (by weight, counted sets only)")
    print(f"{'─'*60}")
    for r in sorted(best_by_exercise, key=lambda x: x["exercise"]):
        amrap = " AMRAP" if r["is_amrap"] else ""
        rpe   = f" @RPE{r['rpe']:.0f}" if r["rpe"] else ""
        print(f"  {r['exercise']:<30}  {r['reps']}×{r['weight_kg']:.1f}kg{amrap}{rpe}  ({r['session_date']})")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Query lift history from coach.db")
    parser.add_argument("--exercise", help="Exercise name (fuzzy match)")
    parser.add_argument("--weeks", type=int, help="Limit history to last N weeks")
    parser.add_argument("--sessions", type=int, default=5, help="Number of recent sessions to show")
    parser.add_argument("--top-sets", action="store_true", help="Show best set per exercise")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = _connect(os.path.abspath(args.db_path))
    try:
        if args.top_sets:
            top_sets(conn)
        elif args.exercise:
            exercise_history(conn, args.exercise, weeks=args.weeks)
        else:
            recent_sessions(conn, n=args.sessions)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
