"""
serve.py — Minimal JSON API server for the dashboard.

Serves index.html + exposes /api/* endpoints that query coach.db.

Usage:
    python dashboard/serve.py
    python dashboard/serve.py --port 8080 --db-path data/coach.db

Endpoints:
    GET /                      → index.html
    GET /api/strength          → e1RM estimates per exercise (latest per exercise)
    GET /api/sessions          → last 20 sessions summary
    GET /api/lifts/<exercise>  → weekly e1RM trend for one exercise
    GET /api/health            → last 60 days of health data
    GET /api/injuries          → active injuries
"""

import json
import os
import sqlite3
import sys
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH    = os.path.join(REPO_ROOT, "data", "coach.db")
DASH_DIR   = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(DASH_DIR, "index.html")

PORT = 8080


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

def api_strength() -> list:
    """Latest e1RM / e5RM estimate per exercise."""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT exercise, estimated_at, e1rm_kg, e5rm_kg, confidence_low, confidence_high
            FROM strength_estimates s1
            WHERE estimated_at = (
                SELECT MAX(s2.estimated_at) FROM strength_estimates s2
                WHERE s2.exercise = s1.exercise
            )
            ORDER BY exercise
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def api_sessions(n: int = 20) -> list:
    """Last N sessions summary."""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT session_date, week_number, block_number, day_number,
                   COUNT(*) as total_sets,
                   SUM(CASE WHEN should_count THEN 1 ELSE 0 END) as counted_sets,
                   GROUP_CONCAT(DISTINCT exercise) as exercises
            FROM lift_sets
            GROUP BY session_date, day_number
            ORDER BY session_date DESC
            LIMIT ?
        """, (n,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def api_lifts_trend(exercise: str) -> list:
    """Weekly best e1RM estimate for a given exercise (from strength_estimates)."""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT estimated_at, e1rm_kg, e5rm_kg, confidence_low, confidence_high
            FROM strength_estimates
            WHERE LOWER(exercise) LIKE LOWER(?)
            ORDER BY estimated_at ASC
        """, (f"%{exercise}%",)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def api_health(days: int = 60) -> list:
    """Last N days of health data."""
    conn = _db()
    cutoff = str(date.today() - timedelta(days=days))
    try:
        rows = conn.execute("""
            SELECT log_date, body_weight_kg, sleep_hours, steps, resting_hr, hrv, source
            FROM health_log
            WHERE log_date >= ?
            ORDER BY log_date ASC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def api_injuries() -> list:
    """Active injuries."""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT injury_name, body_part, start_date, severity, notes
            FROM injury_log
            WHERE end_date IS NULL
            ORDER BY start_date DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default Apache-style logs

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path in ("/", ""):
                self._html(INDEX_HTML)

            elif path == "/api/strength":
                self._json(api_strength())

            elif path == "/api/sessions":
                n = int(params.get("n", [20])[0])
                self._json(api_sessions(n))

            elif path.startswith("/api/lifts/"):
                exercise = urllib.parse.unquote(path[len("/api/lifts/"):])
                self._json(api_lifts_trend(exercise))

            elif path == "/api/health":
                days = int(params.get("days", [60])[0])
                self._json(api_health(days))

            elif path == "/api/injuries":
                self._json(api_injuries())

            else:
                self._json({"error": "not found"}, 404)

        except FileNotFoundError:
            self._json({"error": "index.html not found"}, 404)
        except sqlite3.OperationalError as e:
            self._json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global DB_PATH, PORT

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",    type=int, default=PORT)
    parser.add_argument("--db-path", default=DB_PATH)
    args = parser.parse_args()

    PORT    = args.port
    DB_PATH = os.path.abspath(args.db_path)

    if not os.path.isfile(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}. Run: python scripts/init_db.py")
        sys.exit(1)

    print(f"Dashboard running at http://localhost:{PORT}")
    print(f"Database: {DB_PATH}")
    print("Ctrl+C to stop.")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
