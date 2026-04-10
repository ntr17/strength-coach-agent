"""
pipeline.py — Data pipeline entry point.

Reads from Google Sheets + Garmin Connect, runs DS analysis, and writes four
markdown files to output/ (and optionally Google Drive).

Usage:
  python src/pipeline.py              # full run
  python src/pipeline.py --dry-run   # print files to stdout, no writes
  python src/pipeline.py --days 30   # override Garmin history window

No Telegram, no LLM, no Railway. Just data → files.
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

# Add src/ to path so imports work whether run from project root or src/
sys.path.insert(0, os.path.dirname(__file__))


def main():
    parser = argparse.ArgumentParser(description="Strength coach data pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print markdown files to stdout instead of writing")
    parser.add_argument("--days", type=int, default=None,
                        help="Override Garmin history window (days)")
    args = parser.parse_args()

    # Bootstrap Google credentials from env vars (for GitHub Actions)
    from config import bootstrap_google_credentials, PROGRAM_REGISTRY, GARMIN_HISTORY_DAYS, DRIVE_OUTPUT_FOLDER_ID
    bootstrap_google_credentials()

    garmin_days = args.days or GARMIN_HISTORY_DAYS
    output_dir = Path(__file__).parent.parent / "output"
    drive_folder_id = DRIVE_OUTPUT_FOLDER_ID or None

    print(f"[pipeline] Starting — {date.today()}")
    print(f"[pipeline] Programs: {[p['name'] for p in PROGRAM_REGISTRY]}")
    print(f"[pipeline] Garmin window: {garmin_days} days")
    print(f"[pipeline] Drive folder: {drive_folder_id or 'not configured (local only)'}")
    print()

    # ------------------------------------------------------------------
    # 1. Build lift history from all program sheets
    # ------------------------------------------------------------------
    print("[pipeline] Reading lift history from sheets...")
    from lift_history import build_lift_history, personal_records
    records = build_lift_history(PROGRAM_REGISTRY)
    prs = personal_records(records)
    print(f"[pipeline] {len(records)} exercise records across {len(PROGRAM_REGISTRY)} program(s)")
    print(f"[pipeline] {len(prs)} exercises with PR data")

    # ------------------------------------------------------------------
    # 2. Read program data (current week context, progression, goals)
    # ------------------------------------------------------------------
    print("[pipeline] Reading program context...")
    from sheets import read_program_data, infer_week_from_sheet
    from config import PROGRAM_SHEET_ID

    program_data = {}
    week_num = 1
    if PROGRAM_SHEET_ID:
        try:
            week_num = infer_week_from_sheet(PROGRAM_SHEET_ID)
            program_data = read_program_data(week_num=week_num, lookback=4, sheet_id=PROGRAM_SHEET_ID)
            print(f"[pipeline] Current week: {week_num}")
        except Exception as e:
            print(f"[pipeline] Warning — could not read program context: {e}")

    progression = program_data.get("progression", {})
    goals = program_data.get("goals", {})
    daily_log = program_data.get("daily_log", [])

    # ------------------------------------------------------------------
    # 3. Fetch Garmin data
    # ------------------------------------------------------------------
    print(f"[pipeline] Fetching Garmin data ({garmin_days} days)...")
    garmin_data = []
    try:
        from garmin import GarminClient
        gc = GarminClient()
        if gc.is_available():
            garmin_data = gc.fetch_range(days=garmin_days)
            print(f"[pipeline] {len(garmin_data)} Garmin records fetched")
        else:
            print("[pipeline] Garmin not available (missing credentials) — skipping")
    except Exception as e:
        print(f"[pipeline] Garmin fetch failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 4. Run analysis
    # ------------------------------------------------------------------
    print("[pipeline] Running analysis...")
    from analysis_engine import run_all
    analysis = run_all(records, garmin_data, progression, goals)

    stalls = analysis.get("stalls", {}) or {}
    stalled_count = sum(1 for v in stalls.values() if v["status"] == "STALL")
    print(f"[pipeline] Stalls detected: {stalled_count}")
    print(f"[pipeline] Load signal: {analysis.get('load_index', {}).get('signal', 'N/A')}")

    # ------------------------------------------------------------------
    # 5. Generate markdown files
    # ------------------------------------------------------------------
    print("[pipeline] Generating markdown files...")
    from drive_export import (
        generate_training_log_md,
        generate_program_context_md,
        generate_health_recovery_md,
        generate_analysis_md,
        write_output,
    )

    total_weeks = program_data.get("progression") and max(program_data["progression"].keys()) or 30

    files = {
        "training_log.md": generate_training_log_md(records, prs),
        "program_context.md": generate_program_context_md(program_data, week_num, total_weeks),
        "health_recovery.md": generate_health_recovery_md(garmin_data, daily_log),
        "analysis.md": generate_analysis_md(analysis),
    }

    for name, content in files.items():
        word_count = len(content.split())
        print(f"[pipeline]   {name}: {word_count} words, {len(content)} chars")

    # ------------------------------------------------------------------
    # 6. Write output
    # ------------------------------------------------------------------
    if args.dry_run:
        print()
        print("=" * 60)
        print("DRY RUN — printing files to stdout")
        print("=" * 60)
        for name, content in files.items():
            print(f"\n{'='*60}\n{name}\n{'='*60}")
            print(content[:3000] + ("..." if len(content) > 3000 else ""))
    else:
        print(f"[pipeline] Writing to {output_dir} ...")
        write_output(output_dir, files, drive_folder_id)

    print()
    print(f"[pipeline] Done — {date.today()}")


if __name__ == "__main__":
    main()
