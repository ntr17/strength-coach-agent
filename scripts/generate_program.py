"""
generate_program.py — Parameterized training program generator.

Claude can invoke this during a conversation to generate structured training programs.
The output is a markdown file ready for the user to follow.

Usage:
    python scripts/generate_program.py --help
    python scripts/generate_program.py --type week --days 2 --focus upper
    python scripts/generate_program.py --type block --weeks 4 --start-week 12 --focus intensity
    python scripts/generate_program.py --from-profile  # reads system/profile.json

Output: prints to stdout (Claude shows it to user), optionally saves to system/plans/
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROFILE_PATH = Path(__file__).parent.parent / "system" / "profile.json"
STATE_PATH   = Path(__file__).parent.parent / "system" / "state.json"


# ---------------------------------------------------------------------------
# 1RM-based load calculation helpers
# ---------------------------------------------------------------------------

def pct_of_1rm(e1rm: float, pct: float) -> float:
    """Round to nearest 2.5kg increment."""
    raw = e1rm * pct / 100
    return round(raw / 2.5) * 2.5


def sets_reps_scheme(phase: str) -> list[tuple[int, int]]:
    """
    Return (sets, reps) tuples for a given phase.
    Phases: volume, strength, intensity, peak, deload, test
    """
    schemes = {
        "volume":    [(4, 8), (3, 10), (3, 8)],
        "strength":  [(4, 5), (4, 4), (3, 5)],
        "intensity": [(4, 4), (3, 3), (4, 3)],
        "peak":      [(3, 3), (3, 2), (2, 2)],
        "deload":    [(3, 5), (3, 5), (3, 5)],   # 60-70% load
        "test":      [(1, 1), (1, 1), (1, 1)],   # working up to e1RM
        "travel":    [(3, 8), (3, 10), (3, 8)],   # higher reps, lower load (hotel gym)
    }
    return schemes.get(phase, schemes["strength"])


# ---------------------------------------------------------------------------
# Program templates
# ---------------------------------------------------------------------------

FOUR_DAY_SPLIT = {
    "DAY 1": {"label": "Squat + Bench Heavy", "primary": ["Squat", "Bench Press"], "secondary": ["Dips", "Romanian Deadlift"]},
    "DAY 2": {"label": "Deadlift + OHP", "primary": ["Deadlift", "OHP"], "secondary": ["Pull-Ups", "Incline DB Press"]},
    "DAY 3": {"label": "Squat + Bench Volume", "primary": ["Squat", "Bench Press"], "secondary": ["DB Row", "Leg Press"]},
    "DAY 4": {"label": "Deadlift + Accessory", "primary": ["Romanian Deadlift"], "secondary": ["Pull-Ups", "OHP", "Lateral Raises"]},
}

TWO_DAY_SPLIT = {
    "DAY 1": {"label": "Upper (Push + Pull)", "primary": ["Bench Press", "OHP"], "secondary": ["Pull-Ups", "DB Row", "Dips"]},
    "DAY 2": {"label": "Lower (Squat + Hinge)", "primary": ["Squat", "Romanian Deadlift"], "secondary": ["Leg Press", "Nordic Curl"]},
}

THREE_DAY_SPLIT = {
    "DAY 1": {"label": "Push Heavy (Bench + OHP)", "primary": ["Bench Press", "OHP"], "secondary": ["Dips", "Lateral Raises"]},
    "DAY 2": {"label": "Pull + Legs (Deadlift + Back)", "primary": ["Deadlift", "Pull-Ups"], "secondary": ["DB Row", "Romanian Deadlift"]},
    "DAY 3": {"label": "Squat + Upper Volume", "primary": ["Squat"], "secondary": ["Incline DB Press", "Pull-Ups", "Leg Press"]},
}


def get_split(days: int) -> dict:
    if days <= 2:
        return TWO_DAY_SPLIT
    elif days == 3:
        return THREE_DAY_SPLIT
    else:
        return FOUR_DAY_SPLIT


# ---------------------------------------------------------------------------
# Core generators
# ---------------------------------------------------------------------------

def generate_week(
    week_num: int,
    block_num: int,
    phase: str,
    days: int,
    lifts_e1rm: dict,  # {lift_name: e1rm_kg}
    injuries: list = None,
    notes: str = "",
) -> str:
    """Generate a single training week as markdown."""
    injuries = injuries or []
    split = get_split(days)

    # Load percentages by phase
    load_pcts = {
        "volume":    {"primary": 72.5, "secondary": 67.5},
        "strength":  {"primary": 82.5, "secondary": 75.0},
        "intensity": {"primary": 87.5, "secondary": 80.0},
        "peak":      {"primary": 92.5, "secondary": 85.0},
        "deload":    {"primary": 65.0, "secondary": 60.0},
        "test":      {"primary": 95.0, "secondary": 80.0},
        "travel":    {"primary": 70.0, "secondary": 65.0},
    }
    pcts = load_pcts.get(phase, load_pcts["strength"])

    lines = [
        f"# Week {week_num} — Block {block_num} ({phase.upper()})",
        f"Generated: {date.today().isoformat()}",
        "",
    ]
    if notes:
        lines += [f"*{notes}*", ""]

    injury_notes = ""
    if "elbow" in " ".join(injuries).lower():
        injury_notes = "Elbow: avoid direct tricep isolation. Stop any pulling if sharp pain."
    if injury_notes:
        lines += [f"> **Injury note:** {injury_notes}", ""]

    for day_key, day_info in split.items():
        lines.append(f"## {day_key}: {day_info['label']}")
        lines.append("")
        lines.append("| Exercise | Weight | Sets x Reps | Done | Actual | Notes |")
        lines.append("|----------|--------|-------------|------|--------|-------|")

        all_lifts = day_info["primary"] + day_info["secondary"]
        for lift in all_lifts:
            is_primary = lift in day_info["primary"]
            e1rm = lifts_e1rm.get(lift)

            if e1rm:
                pct = pcts["primary"] if is_primary else pcts["secondary"]
                weight = pct_of_1rm(e1rm, pct)
                weight_str = f"{weight}kg"
            else:
                weight_str = "—"

            scheme = sets_reps_scheme(phase)
            s_r = f"{scheme[0][0]}x{scheme[0][1]}" if is_primary else f"{scheme[1][0]}x{scheme[1][1]}"

            lines.append(f"| {lift} | {weight_str} | {s_r} | | | |")

        lines.append("")

    lines += [
        "## Weekly Notes",
        "",
        "| | |",
        "|---|---|",
        "| Bodyweight | |",
        "| Sleep avg | |",
        "| Energy (1-10) | |",
        "| Notes | |",
        "",
    ]

    return "\n".join(lines)


def generate_block(
    start_week: int,
    num_weeks: int,
    block_num: int,
    phase: str,
    days: int,
    lifts_e1rm: dict,
    injuries: list = None,
    include_deload: bool = True,
) -> str:
    """Generate a multi-week block as markdown."""
    sections = []
    actual_weeks = num_weeks - 1 if include_deload else num_weeks
    for i in range(actual_weeks):
        week_num = start_week + i
        sections.append(generate_week(week_num, block_num, phase, days, lifts_e1rm, injuries))
    if include_deload:
        deload_week = start_week + actual_weeks
        sections.append(generate_week(deload_week, block_num, "deload", days, lifts_e1rm, injuries,
                                      notes="Deload week — reduce load and volume, focus on recovery"))
    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    if PROFILE_PATH.exists():
        return json.loads(PROFILE_PATH.read_text())
    return {}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def main():
    parser = argparse.ArgumentParser(description="Generate a training program")
    parser.add_argument("--type", choices=["week", "block", "travel"], default="week",
                        help="What to generate: single week, a block, or travel week")
    parser.add_argument("--week", type=int, default=None, help="Week number")
    parser.add_argument("--block", type=int, default=None, help="Block number")
    parser.add_argument("--weeks", type=int, default=4, help="Number of weeks (for block type)")
    parser.add_argument("--phase", default=None,
                        choices=["volume", "strength", "intensity", "peak", "deload", "test", "travel"],
                        help="Training phase")
    parser.add_argument("--days", type=int, default=None, help="Training days per week")
    parser.add_argument("--from-profile", action="store_true", help="Load e1RM and profile from system/profile.json")
    parser.add_argument("--output", type=str, default=None,
                        help="Save to file (e.g. system/plans/weekly/2026-W16.md)")
    parser.add_argument("--squat", type=float, default=None, help="Squat e1RM (kg)")
    parser.add_argument("--bench", type=float, default=None, help="Bench Press e1RM (kg)")
    parser.add_argument("--deadlift", type=float, default=None, help="Deadlift e1RM (kg)")
    parser.add_argument("--ohp", type=float, default=None, help="OHP e1RM (kg)")
    args = parser.parse_args()

    profile = load_profile()
    state = load_state()

    # Resolve parameters
    week_num = args.week or state.get("current_week", 1)
    block_num = args.block or ((week_num - 1) // 5 + 1)
    days = args.days or profile.get("lifestyle", {}).get("typical_training_days", 4)
    injuries = [i["injury_name"] for i in profile.get("active_injuries", [])] if isinstance(profile.get("active_injuries"), list) else []

    # Build e1RM dict
    lifts_e1rm = {}
    if args.from_profile:
        targets = profile.get("goals", {}).get("target_lifts", {})
        # Use current estimates if available
        for lift, data in targets.items():
            if isinstance(data, dict) and "current_e1rm" in data:
                lifts_e1rm[lift] = data["current_e1rm"]
    if args.squat:
        lifts_e1rm["Squat"] = args.squat
    if args.bench:
        lifts_e1rm["Bench Press"] = args.bench
    if args.deadlift:
        lifts_e1rm["Deadlift"] = args.deadlift
    if args.ohp:
        lifts_e1rm["OHP"] = args.ohp

    # Determine phase
    phase = args.phase
    if phase is None:
        if args.type == "travel":
            phase = "travel"
        else:
            # Infer from block number
            phase_map = {1: "volume", 2: "strength", 3: "intensity", 4: "intensity", 5: "peak", 6: "test"}
            phase = phase_map.get(block_num, "strength")

    # Generate
    if args.type == "travel":
        output = generate_week(week_num, block_num, "travel", min(days, 3), lifts_e1rm, injuries,
                               notes="Travel week — hotel gym. Prioritize main lifts, skip accessories if needed.")
    elif args.type == "block":
        output = generate_block(week_num, args.weeks, block_num, phase, days, lifts_e1rm, injuries)
    else:
        output = generate_week(week_num, block_num, phase, days, lifts_e1rm, injuries)

    print(output)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"\nSaved to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
