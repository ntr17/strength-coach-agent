"""
init_db.py — Create data/coach.db with the full schema.

Run this once to initialize the database. Safe to re-run: uses
IF NOT EXISTS so it never destroys existing data.

Usage:
    python scripts/init_db.py
    python scripts/init_db.py --db-path /custom/path/coach.db
"""

import argparse
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "coach.db")

SCHEMA = """
-- Every tracked set, ever
CREATE TABLE IF NOT EXISTS lift_sets (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_date      DATE NOT NULL,
  week_number       INTEGER NOT NULL,
  block_number      INTEGER NOT NULL,
  day_number        INTEGER NOT NULL,
  exercise          TEXT NOT NULL,
  set_number        INTEGER NOT NULL,
  reps              INTEGER NOT NULL,
  weight_kg         REAL NOT NULL,
  is_amrap          BOOLEAN DEFAULT 0,
  should_count      BOOLEAN DEFAULT 1,
  rpe               REAL,
  notes             TEXT,
  source            TEXT NOT NULL DEFAULT 'sheet',  -- 'sheet' = real date | 'bootstrap' = approx date
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Computed strength estimates (run by estimate_strength.py)
CREATE TABLE IF NOT EXISTS strength_estimates (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  estimated_at      DATE NOT NULL,
  exercise          TEXT NOT NULL,
  e1rm_kg           REAL NOT NULL,
  e5rm_kg           REAL NOT NULL,
  confidence_low    REAL,
  confidence_high   REAL,
  method_detail     TEXT,
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Daily health data
CREATE TABLE IF NOT EXISTS health_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  log_date          DATE NOT NULL UNIQUE,
  body_weight_kg    REAL,
  body_fat_pct      REAL,
  visceral_fat_index REAL,
  sleep_hours       REAL,
  sleep_quality     INTEGER,
  steps             INTEGER,
  resting_hr        INTEGER,
  hrv               REAL,
  source            TEXT,
  notes             TEXT,
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Injuries
CREATE TABLE IF NOT EXISTS injury_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  injury_name       TEXT NOT NULL,
  body_part         TEXT NOT NULL,
  start_date        DATE NOT NULL,
  end_date          DATE,
  severity          INTEGER,
  notes             TEXT,
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Open threads / unresolved decisions
CREATE TABLE IF NOT EXISTS threads (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id         TEXT NOT NULL UNIQUE,
  title             TEXT NOT NULL,
  level             TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'open',
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  resolved_at       TIMESTAMP,
  description       TEXT,
  resolution        TEXT
);

-- Reasoning log
CREATE TABLE IF NOT EXISTS reasoning_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  log_date          DATE NOT NULL,
  level             TEXT NOT NULL,
  trigger_event     TEXT NOT NULL,
  summary           TEXT NOT NULL,
  affected_plans    TEXT,
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Medical records (blood work, body composition, fitness tests, etc.)
CREATE TABLE IF NOT EXISTS medical_records (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  test_date   DATE NOT NULL,
  category    TEXT NOT NULL,   -- blood_work | body_comp | fitness_test | hormone | imaging | other
  test_name   TEXT NOT NULL,   -- e.g. 'Testosterone', 'HbA1c', 'VO2max', 'DEXA body fat %'
  value       REAL,            -- numeric value (null if text-only)
  value_text  TEXT,            -- for non-numeric results or free text
  unit        TEXT,            -- 'ng/dL', '%', 'mL/kg/min', etc.
  ref_low     REAL,            -- reference range low
  ref_high    REAL,            -- reference range high
  flag        TEXT,            -- 'LOW' | 'HIGH' | 'NORMAL' | null (computed if ref range given)
  notes       TEXT,
  source      TEXT,            -- 'lab' | 'garmin' | 'doctor' | 'manual'
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cardio sessions (Garmin or manual)
CREATE TABLE IF NOT EXISTS cardio_sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_date    DATE NOT NULL,
  activity_type   TEXT NOT NULL,   -- 'run' | 'bike' | 'row' | 'swim' | 'other'
  duration_min    REAL,
  distance_km     REAL,
  avg_hr          INTEGER,
  max_hr          INTEGER,
  avg_pace_sec_km REAL,            -- seconds per km
  hr_zone1_min    REAL,
  hr_zone2_min    REAL,
  hr_zone3_min    REAL,
  hr_zone4_min    REAL,
  hr_zone5_min    REAL,
  vo2max_estimate REAL,            -- Garmin's estimate if available
  notes           TEXT,
  source          TEXT DEFAULT 'garmin',
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_lift_sets_date        ON lift_sets(session_date);
CREATE INDEX IF NOT EXISTS idx_lift_sets_exercise    ON lift_sets(exercise);
CREATE INDEX IF NOT EXISTS idx_lift_sets_counted     ON lift_sets(exercise, should_count);
CREATE INDEX IF NOT EXISTS idx_health_log_date       ON health_log(log_date);
CREATE INDEX IF NOT EXISTS idx_strength_est_exercise ON strength_estimates(exercise, estimated_at);
CREATE INDEX IF NOT EXISTS idx_medical_date         ON medical_records(test_date);
CREATE INDEX IF NOT EXISTS idx_medical_name         ON medical_records(test_name, test_date);
CREATE INDEX IF NOT EXISTS idx_cardio_date ON cardio_sessions(session_date);
"""


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize coach.db schema")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db_path)
    print(f"Initializing database at: {db_path}")
    init_db(db_path)

    # Verify by listing tables
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    print("Tables created:")
    for t in tables:
        print(f"  {t}")
    print("Done.")


if __name__ == "__main__":
    main()
