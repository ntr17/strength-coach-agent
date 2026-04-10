"""
import_medical.py — Import medical test results into coach.db.

Usage (single record):
    python scripts/import_medical.py \\
        --date 2026-04-11 \\
        --category blood_work \\
        --name "Testosterone" \\
        --value 650 \\
        --unit "ng/dL" \\
        --ref-low 300 \\
        --ref-high 1000

Usage (from markdown file):
    python scripts/import_medical.py --file medical_results.md

Markdown file format:
    # Medical Results — 2026-04-11
    Category: blood_work
    Source: lab

    | Test | Value | Unit | Ref Low | Ref High | Notes |
    |------|-------|------|---------|----------|-------|
    | Testosterone | 650 | ng/dL | 300 | 1000 | |
    | HbA1c | 5.4 | % | | 5.7 | Pre-diabetic threshold is 5.7 |
    | Glucose (fasting) | 92 | mg/dL | 70 | 100 | |

Categories: blood_work | body_comp | fitness_test | hormone | imaging | other
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

DEFAULT_DB = Path(__file__).parent.parent / "data" / "coach.db"


def _compute_flag(value, ref_low, ref_high):
    if value is None:
        return None
    if ref_low is not None and value < ref_low:
        return "LOW"
    if ref_high is not None and value > ref_high:
        return "HIGH"
    if ref_low is not None or ref_high is not None:
        return "NORMAL"
    return None


def insert_record(db_path, test_date, category, test_name, value, value_text,
                  unit, ref_low, ref_high, notes, source):
    flag = _compute_flag(value, ref_low, ref_high)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO medical_records
              (test_date, category, test_name, value, value_text, unit,
               ref_low, ref_high, flag, notes, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (test_date, category, test_name, value, value_text, unit,
             ref_low, ref_high, flag, notes, source),
        )
        conn.commit()
    finally:
        conn.close()

    flag_str = f" [{flag}]" if flag and flag != "NORMAL" else ""
    val_str  = f"{value} {unit or ''}".strip() if value is not None else (value_text or "—")
    print(f"  Inserted: {test_name} = {val_str}{flag_str} ({test_date})")


def parse_markdown_file(filepath):
    """
    Parse a markdown medical results file. Returns list of record dicts.

    Expected format:
        # Medical Results — YYYY-MM-DD
        Category: blood_work
        Source: lab

        | Test | Value | Unit | Ref Low | Ref High | Notes |
        |------|-------|------|---------|----------|-------|
        | Testosterone | 650 | ng/dL | 300 | 1000 | |
    """
    text = Path(filepath).read_text(encoding="utf-8")

    # Extract date from heading
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    test_date = date_match.group(1) if date_match else str(date.today())

    # Extract category
    cat_match = re.search(r"^Category:\s*(\S+)", text, re.MULTILINE | re.IGNORECASE)
    category = cat_match.group(1).lower() if cat_match else "other"

    # Extract source
    src_match = re.search(r"^Source:\s*(\S+)", text, re.MULTILINE | re.IGNORECASE)
    source = src_match.group(1).lower() if src_match else "manual"

    records = []
    in_table = False
    header_seen = False

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if not cells:
            continue

        # Detect header row
        if not header_seen:
            if cells[0].lower() in ("test", "name", "test name"):
                header_seen = True
            continue

        # Skip separator row
        if all(set(c) <= set("-: ") for c in cells):
            continue

        if len(cells) < 2:
            continue

        test_name  = cells[0] if len(cells) > 0 else ""
        raw_value  = cells[1].strip() if len(cells) > 1 else ""
        unit       = cells[2].strip() if len(cells) > 2 else ""
        raw_low    = cells[3].strip() if len(cells) > 3 else ""
        raw_high   = cells[4].strip() if len(cells) > 4 else ""
        notes      = cells[5].strip() if len(cells) > 5 else ""

        def _parse_float(s):
            try:
                return float(s) if s else None
            except ValueError:
                return None

        value     = _parse_float(raw_value)
        value_text = raw_value if value is None and raw_value else None
        ref_low   = _parse_float(raw_low)
        ref_high  = _parse_float(raw_high)

        if not test_name:
            continue

        records.append({
            "test_date":  test_date,
            "category":   category,
            "test_name":  test_name,
            "value":      value,
            "value_text": value_text,
            "unit":       unit or None,
            "ref_low":    ref_low,
            "ref_high":   ref_high,
            "notes":      notes or None,
            "source":     source,
        })

    return records


def main():
    parser = argparse.ArgumentParser(description="Import medical records into coach.db")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))

    sub = parser.add_mutually_exclusive_group(required=True)
    sub.add_argument("--file", help="Markdown file with results table")
    sub.add_argument("--name", help="Single test name")

    # Single record args
    parser.add_argument("--date", dest="test_date", default=str(date.today()))
    parser.add_argument("--category", default="other",
                        choices=["blood_work", "body_comp", "fitness_test",
                                 "hormone", "imaging", "other"])
    parser.add_argument("--value", type=float)
    parser.add_argument("--value-text")
    parser.add_argument("--unit")
    parser.add_argument("--ref-low", type=float)
    parser.add_argument("--ref-high", type=float)
    parser.add_argument("--notes")
    parser.add_argument("--source", default="manual")

    args = parser.parse_args()
    db_path = Path(args.db_path)

    if not db_path.exists():
        print(f"Error: DB not found at {db_path}. Run init_db.py first.")
        sys.exit(1)

    if args.file:
        records = parse_markdown_file(args.file)
        if not records:
            print("No records found in file. Check the table format.")
            sys.exit(1)
        print(f"Importing {len(records)} record(s) from {args.file}:")
        for r in records:
            insert_record(db_path, **r)
    else:
        if args.value is None and args.value_text is None:
            print("Error: provide --value or --value-text")
            sys.exit(1)
        insert_record(
            db_path,
            test_date  = args.test_date,
            category   = args.category,
            test_name  = args.name,
            value      = args.value,
            value_text = args.value_text,
            unit       = args.unit,
            ref_low    = args.ref_low,
            ref_high   = args.ref_high,
            notes      = args.notes,
            source     = args.source,
        )

    print("Done.")


if __name__ == "__main__":
    main()
