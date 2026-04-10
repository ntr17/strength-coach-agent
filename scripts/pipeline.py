"""
pipeline.py — Data pipeline: Sheets + Garmin -> markdown -> Google Drive.

Reads from Google Sheets and Garmin Connect, runs analysis, and writes
four markdown files to output/ and uploads them to Google Drive.

This is what GitHub Actions runs nightly.

Usage:
  python scripts/pipeline.py              # full run
  python scripts/pipeline.py --dry-run    # print to stdout, no writes
  python scripts/pipeline.py --skip-garmin
  python scripts/pipeline.py --week 11   # override week number
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

# Add scripts/ to path
sys.path.insert(0, os.path.dirname(__file__))

OUTPUT_DIR = Path(__file__).parent.parent / "output"
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1Zi6dFQA2lCRickf6XYpfedIiFPRHrTpn")
PROGRAM_SHEET_ID = os.environ.get("PROGRAM_SHEET_ID", "")
GARMIN_HISTORY_DAYS = int(os.environ.get("GARMIN_HISTORY_DAYS", "90"))


def main():
    parser = argparse.ArgumentParser(description="Strength coach data pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Print files to stdout, no writes")
    parser.add_argument("--skip-garmin", action="store_true", help="Skip Garmin fetch")
    parser.add_argument("--week", type=int, default=None, help="Override current week number")
    parser.add_argument("--days", type=int, default=None, help="Override Garmin history window")
    args = parser.parse_args()

    garmin_days = args.days or GARMIN_HISTORY_DAYS

    print(f"[pipeline] Starting — {date.today()}")
    print(f"[pipeline] Drive folder: {DRIVE_FOLDER_ID or 'not configured (dry-run only)'}")

    # ------------------------------------------------------------------
    # 1. Build lift history from Google Sheet
    # ------------------------------------------------------------------
    print("[pipeline] Reading lift history from sheets...")
    records = []
    prs = {}
    program_data = {}
    week_num = args.week or 1

    if PROGRAM_SHEET_ID:
        try:
            from sheets_client import infer_week_from_sheet, read_program_data
            from lift_history import build_lift_history, personal_records

            if args.week is None:
                week_num = infer_week_from_sheet(PROGRAM_SHEET_ID)

            registry = [{"name": "30-Week Strength", "sheet_id": PROGRAM_SHEET_ID}]
            records = build_lift_history(registry)
            prs = personal_records(records)
            program_data = read_program_data(sheet_id=PROGRAM_SHEET_ID, week_num=week_num)
            print(f"[pipeline] Week {week_num} | {len(records)} records | {len(prs)} PRs")
        except Exception as e:
            print(f"[pipeline] Warning — could not read sheet: {e}")
    else:
        print("[pipeline] No PROGRAM_SHEET_ID — skipping sheet read")

    # ------------------------------------------------------------------
    # 2. Fetch Garmin data
    # ------------------------------------------------------------------
    garmin_data = []
    if not args.skip_garmin:
        print(f"[pipeline] Fetching Garmin data ({garmin_days} days)...")
        try:
            from garmin_sync import fetch_garmin_range
            garmin_data = fetch_garmin_range(days=garmin_days)
            print(f"[pipeline] {len(garmin_data)} Garmin records")
        except Exception as e:
            print(f"[pipeline] Garmin fetch failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 3. Run analysis
    # ------------------------------------------------------------------
    print("[pipeline] Running analysis...")
    from analysis_engine import run_all
    progression = program_data.get("progression", {})
    goals = program_data.get("goals", {})
    daily_log = program_data.get("daily_log", [])
    analysis = run_all(records, garmin_data, progression, goals)

    stalls = analysis.get("stalls", {}) or {}
    stalled_count = sum(1 for v in stalls.values() if v["status"] == "STALL")
    print(f"[pipeline] Stalls: {stalled_count} | Load: {analysis.get('load_index', {}).get('signal', 'N/A')}")

    # ------------------------------------------------------------------
    # 4. Generate markdown files
    # ------------------------------------------------------------------
    print("[pipeline] Generating markdown files...")
    from drive_export import (
        generate_training_log_md,
        generate_program_context_md,
        generate_health_recovery_md,
        generate_analysis_md,
        generate_briefing_md,
    )

    total_weeks = max(program_data.get("progression", {}).keys(), default=30)

    files = {
        "training_log.md": generate_training_log_md(records, prs),
        "program_context.md": generate_program_context_md(program_data, week_num, total_weeks),
        "health_recovery.md": generate_health_recovery_md(garmin_data, daily_log),
        "analysis.md": generate_analysis_md(analysis),
        "BRIEFING.md": generate_briefing_md(records, prs, program_data, week_num, total_weeks, garmin_data, daily_log, analysis),
    }

    for name, content in files.items():
        print(f"[pipeline]   {name}: {len(content.split())} words")

    # ------------------------------------------------------------------
    # 5. Write output
    # ------------------------------------------------------------------
    if args.dry_run:
        print("\n" + "="*60 + "\nDRY RUN\n" + "="*60)
        print(files.get("BRIEFING.md", "")[:4000])
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (OUTPUT_DIR / filename).write_text(content, encoding="utf-8")
        print(f"  [local] Written: output/{filename}")

    if DRIVE_FOLDER_ID:
        print(f"[pipeline] Uploading to Drive folder {DRIVE_FOLDER_ID}...")
        try:
            from sheets_client import upload_files_to_drive
            upload_files_to_drive(files, DRIVE_FOLDER_ID)
        except Exception as e:
            print(f"[pipeline] Drive upload failed (non-fatal): {e}")

    print(f"\n[pipeline] Done — {date.today()}")


if __name__ == "__main__":
    main()
