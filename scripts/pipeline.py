"""
pipeline.py — Data pipeline: coach.db + Garmin -> markdown -> Google Drive.

Source of truth: data/coach.db (SQLite)
- lift_sets: populated by import_session.py after each training session
- health_log: populated by garmin_sync.py (nightly)
- strength_estimates: populated by estimate_strength.py --write (auto-run each pipeline)
- medical_records: populated by import_medical.py (manual)

This script reads from coach.db, runs analysis, generates markdown summaries,
and uploads them to Google Drive for the Claude Project to read.

GitHub Actions runs this nightly at 06:30 UTC.

Usage:
  python scripts/pipeline.py              # full run
  python scripts/pipeline.py --dry-run    # print BRIEFING.md to stdout, no writes
  python scripts/pipeline.py --skip-garmin
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

OUTPUT_DIR      = Path(__file__).parent.parent / "output"
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1Zi6dFQA2lCRickf6XYpfedIiFPRHrTpn")
DB_PATH         = Path(__file__).parent.parent / "data" / "coach.db"


def main():
    parser = argparse.ArgumentParser(description="Strength coach data pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Print BRIEFING.md to stdout, no writes")
    parser.add_argument("--skip-garmin", action="store_true", help="Skip Garmin sync step")
    parser.add_argument("--days", type=int, default=90, help="Garmin history window (days)")
    args = parser.parse_args()

    print(f"[pipeline] Starting — {date.today()}")
    print(f"[pipeline] DB: {DB_PATH}")

    # ------------------------------------------------------------------
    # 1. Garmin sync (writes to health_log in coach.db)
    # ------------------------------------------------------------------
    if not args.skip_garmin:
        print("[pipeline] Syncing Garmin...")
        try:
            from garmin_sync import sync_yesterday
            sync_yesterday(str(DB_PATH))
            print("[pipeline] Garmin sync done")
        except Exception as e:
            print(f"[pipeline] Garmin sync failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 2. Run estimate_strength (writes fresh e1RM/e5RM to strength_estimates)
    # ------------------------------------------------------------------
    print("[pipeline] Running strength estimation...")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "estimate_strength.py"),
             "--write", "--db-path", str(DB_PATH)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print("[pipeline] Strength estimates updated")
        else:
            print(f"[pipeline] estimate_strength warning: {result.stderr[:200]}")
    except Exception as e:
        print(f"[pipeline] estimate_strength failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # 3. Read data from coach.db
    # ------------------------------------------------------------------
    print("[pipeline] Reading data from DB...")
    from db_reader import (
        load_lift_records,
        compute_personal_records,
        load_health_records,
        load_latest_estimates,
        load_latest_medical,
        load_state,
        load_profile,
        compute_progression_targets,
        compute_goals,
    )

    records     = load_lift_records(DB_PATH)
    prs         = compute_personal_records(records)
    health_data = load_health_records(DB_PATH, days=args.days)
    estimates   = load_latest_estimates(DB_PATH)
    medical     = load_latest_medical(DB_PATH)
    state       = load_state()
    profile     = load_profile()
    progression = compute_progression_targets(profile, state)
    goals       = compute_goals(profile)

    week_num    = state.get("current_week", 1)
    total_weeks = profile.get("current_program", {}).get("total_weeks", 30)

    print(f"[pipeline] Week {week_num}/{total_weeks} | {len(records)} sets | {len(prs)} PRs | "
          f"{len(health_data)} health records | {len(estimates)} estimates | {len(medical)} medical tests")

    # ------------------------------------------------------------------
    # 4. Run analysis
    # ------------------------------------------------------------------
    print("[pipeline] Running analysis...")
    from analysis_engine import run_all
    analysis = run_all(records, health_data, progression, goals)

    stalls = analysis.get("stalls", {}) or {}
    stalled_count = sum(1 for v in stalls.values() if v["status"] == "STALL")
    print(f"[pipeline] Stalls: {stalled_count} | Load: {analysis.get('load_index', {}).get('signal', 'N/A')}")

    # ------------------------------------------------------------------
    # 5. Generate markdown files
    # ------------------------------------------------------------------
    print("[pipeline] Generating markdown...")
    from drive_export import (
        generate_training_log_md,
        generate_program_context_md_from_db,
        generate_health_recovery_md,
        generate_analysis_md,
        generate_briefing_md_from_db,
    )

    files = {
        "training_log.md":    generate_training_log_md(records, prs),
        "program_context.md": generate_program_context_md_from_db(state, profile, estimates, week_num, total_weeks),
        "health_recovery.md": generate_health_recovery_md(health_data, []),
        "analysis.md":        generate_analysis_md(analysis),
        "BRIEFING.md":        generate_briefing_md_from_db(
                                  state, profile, estimates, prs,
                                  health_data, analysis, week_num, total_weeks,
                                  medical=medical),
    }

    for name, content in files.items():
        print(f"[pipeline]   {name}: {len(content.split())} words")

    # ------------------------------------------------------------------
    # 6. Write output
    # ------------------------------------------------------------------
    if args.dry_run:
        print("\n" + "=" * 60 + "\nDRY RUN — BRIEFING.md\n" + "=" * 60)
        print(files["BRIEFING.md"])
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (OUTPUT_DIR / filename).write_text(content, encoding="utf-8")
        print(f"  [local] output/{filename}")

    if DRIVE_FOLDER_ID:
        print("[pipeline] Uploading to Drive...")
        try:
            from drive_client import upload_files_to_drive
            upload_files_to_drive(files, DRIVE_FOLDER_ID)
        except Exception as e:
            print(f"[pipeline] Drive upload failed (non-fatal): {e}")

    # Update state.json last_pipeline_run
    state_path = Path(__file__).parent.parent / "system" / "state.json"
    if state_path.exists():
        try:
            s = json.loads(state_path.read_text())
            s["last_pipeline_run"] = date.today().isoformat()
            state_path.write_text(json.dumps(s, indent=2))
        except Exception:
            pass

    print(f"\n[pipeline] Done — {date.today()}")


if __name__ == "__main__":
    main()
