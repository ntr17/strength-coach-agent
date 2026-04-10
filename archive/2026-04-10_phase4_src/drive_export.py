"""
drive_export.py — Markdown generators and file writer.

Four output files:
  training_log.md    — session history, PRs, last 16 weeks
  program_context.md — program targets, current position, goal gaps
  health_recovery.md — Garmin + daily log, last 30 days
  analysis.md        — stall detection, volume, load, trajectory, adherence, correlations

write_output(output_dir, files, drive_folder_id) writes locally and optionally to Drive.
"""

import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# training_log.md
# ---------------------------------------------------------------------------

def generate_training_log_md(records: list[dict], prs: dict, weeks: int = 16) -> str:
    """
    Generate training_log.md from lift history records.

    Args:
        records: from lift_history.build_lift_history()
        prs:     from lift_history.personal_records()
        weeks:   how many recent weeks to include in session detail
    """
    lines = [
        "# Training Log",
        f"Generated: {date.today().isoformat()}",
        "",
    ]

    # --- Personal Records ---
    lines += ["## Personal Records", ""]
    if prs:
        lines.append("| Exercise | Weight | Reps | e1RM | Date | Week | Program |")
        lines.append("|----------|--------|------|------|------|------|---------|")
        for ex, pr in sorted(prs.items()):
            w = f"{pr['weight_kg']}kg" if pr['weight_kg'] else "—"
            r = f"{pr['reps']:.0f}" if pr['reps'] else "—"
            e = f"{pr['e1rm']}kg" if pr['e1rm'] else "—"
            d = str(pr['date']) if pr['date'] else "—"
            lines.append(f"| {ex} | {w} | {r} | {e} | {d} | W{pr['week']} | {pr['program']} |")
    else:
        lines.append("*No completed records with e1RM data yet.*")
    lines.append("")

    # --- Session History ---
    # Find max week with done data
    done_records = [r for r in records if r["done"] is True]
    if not done_records:
        lines += ["## Session History", "", "*No completed sessions found.*"]
        return "\n".join(lines)

    max_week = max(r["week"] for r in done_records)
    min_week = max(1, max_week - weeks + 1)
    recent = [r for r in records if r["week"] >= min_week]

    lines.append(f"## Session History (Weeks {min_week}–{max_week})")
    lines.append("")

    # Group by week → day → exercises
    from collections import defaultdict
    by_week: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in recent:
        by_week[r["week"]][r["day"]].append(r)

    for w in sorted(by_week.keys(), reverse=True):
        lines.append(f"### Week {w}")
        for day_label, exs in sorted(by_week[w].items()):
            lines.append(f"**{day_label}**")
            # Session date
            dates = [r["date"] for r in exs if r["date"]]
            if dates:
                lines.append(f"*{min(dates)}*")
            lines.append("")
            lines.append("| Exercise | Planned | Actual | Sets | Reps | RPE | e1RM | Notes |")
            lines.append("|----------|---------|--------|------|------|-----|------|-------|")
            for r in exs:
                planned = f"{r['planned_weight_kg']}kg" if r['planned_weight_kg'] else "—"
                actual_w = f"{r['actual_weight_kg']}kg" if r['actual_weight_kg'] else "—"
                sets = str(r['actual_sets'] or r['planned_sets'] or "—")
                reps = f"{r['actual_reps']:.0f}" if r['actual_reps'] else (f"{r['planned_reps']:.0f}" if r['planned_reps'] else "—")
                rpe = str(r['rpe']) if r['rpe'] else "—"
                e1rm = f"{r['e1rm']}kg" if r['e1rm'] else "—"
                done_marker = "✓" if r['done'] is True else ("✗" if r['done'] is False else "·")
                notes = (r['session_notes'] or "").replace("|", "/")[:60]
                lines.append(f"| {done_marker} {r['exercise']} | {planned} | {actual_w} | {sets} | {reps} | {rpe} | {e1rm} | {notes} |")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# program_context.md
# ---------------------------------------------------------------------------

def generate_program_context_md(
    program_data: dict,
    week_num: int,
    total_weeks: int = 30,
) -> str:
    """
    Generate program_context.md from sheets.read_program_data() output.
    """
    lines = [
        "# Program Context",
        f"Generated: {date.today().isoformat()}",
        f"Current Week: {week_num} of {total_weeks}",
        "",
    ]

    goals = program_data.get("goals", {})
    progression = program_data.get("progression", {})

    # --- Goal gaps ---
    if goals:
        lines += ["## Goals & Current Status", ""]

        # Get current e1RM from current week data (best effort from recent_weeks)
        # We don't have lift history here so just show goal targets
        lines.append("| Lift | Start | Target | Gain | Progress |")
        lines.append("|------|-------|--------|------|----------|")
        for lift, g in goals.items():
            weeks_left = total_weeks - week_num
            pct = round(week_num / total_weeks * 100)
            lines.append(f"| {lift} | {g.get('start','—')} | {g.get('goal','—')} | {g.get('gain','—')} | Week {week_num}/{total_weeks} ({pct}%) — {weeks_left}w left |")
        lines.append("")

    # --- Current week ---
    cw = program_data.get("current_week")
    if cw:
        lines.append(f"## Current Week: {cw.get('title', f'Week {week_num}')}")
        lines.append("")
        for day in cw.get("days", []):
            done = sum(1 for e in day["exercises"] if e.get("done") is True)
            total = len(day["exercises"])
            lines.append(f"**{day['label']}** — {done}/{total} done")
            if day.get("date"):
                lines.append(f"*{day['date']}*")
        lines.append("")

    # --- 30-week progression table ---
    if progression:
        # Lifts from header
        sample_week = next(iter(progression.values()), {})
        lift_cols = [k for k in sample_week if k not in ("type", "block")]

        lines.append("## 30-Week Progression Targets")
        lines.append("")
        header = "| Week | Block | Type | " + " | ".join(lift_cols) + " |"
        sep = "|------|-------|------|" + "|".join(["------"] * len(lift_cols)) + "|"
        lines += [header, sep]

        for w in sorted(progression.keys()):
            wdata = progression[w]
            # Highlight current week
            marker = " ← now" if w == week_num else ""
            cols = " | ".join(str(wdata.get(l, "—")) for l in lift_cols)
            lines.append(f"| {w}{marker} | {wdata.get('block','—')} | {wdata.get('type','—')} | {cols} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# health_recovery.md
# ---------------------------------------------------------------------------

def generate_health_recovery_md(
    garmin_data: list[dict],
    daily_log: list[dict],
) -> str:
    lines = [
        "# Health & Recovery",
        f"Generated: {date.today().isoformat()}",
        "",
    ]

    # Sort garmin by date descending
    garmin_sorted = sorted(garmin_data, key=lambda g: g["date"], reverse=True)

    # --- 7-day summary ---
    recent_7 = garmin_sorted[:7]
    if recent_7:
        def avg(key):
            vals = [g[key] for g in recent_7 if g.get(key) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        lines += [
            "## 7-Day Averages (Garmin)",
            "",
            f"| HRV (ms) | Sleep (h) | Sleep Score | Body Battery | RHR (bpm) | Steps |",
            f"|----------|-----------|-------------|--------------|-----------|-------|",
            f"| {avg('hrv_ms') or '—'} | {avg('sleep_hrs') or '—'} | {avg('sleep_score') or '—'} | "
            f"{avg('body_battery_start') or '—'}→{avg('body_battery_end') or '—'} | "
            f"{avg('resting_hr') or '—'} | {int(avg('steps')) if avg('steps') else '—'} |",
            "",
        ]

    # --- Garmin history ---
    if garmin_sorted:
        lines += ["## Garmin Daily Log (Last 90 Days)", ""]
        lines.append("| Date | HRV | Sleep | Score | Body Bat. | RHR | Steps |")
        lines.append("|------|-----|-------|-------|-----------|-----|-------|")
        for g in garmin_sorted[:90]:
            def _v(k):
                v = g.get(k)
                return str(v) if v is not None else "—"
            bb = f"{_v('body_battery_start')}→{_v('body_battery_end')}"
            lines.append(
                f"| {g['date']} | {_v('hrv_ms')} | {_v('sleep_hrs')}h | {_v('sleep_score')} | "
                f"{bb} | {_v('resting_hr')} | {_v('steps')} |"
            )
        lines.append("")
    else:
        lines += ["## Garmin Daily Log", "", "*No Garmin data available.*", ""]

    # --- Daily log (from sheet) ---
    if daily_log:
        # Sort by date descending
        log_sorted = sorted(
            [e for e in daily_log if e.get("date")],
            key=lambda e: e["date"],
            reverse=True,
        )[:30]

        lines += ["## Daily Log (from Sheet)", ""]
        lines.append("| Date | Bodyweight | Sleep | Energy | Food | Sun | Notes |")
        lines.append("|------|------------|-------|--------|------|-----|-------|")
        for e in log_sorted:
            def _lv(k):
                v = e.get(k)
                return str(v) if v is not None else "—"
            sun = "☀" if e.get("sun") is True else ("✗" if e.get("sun") is False else "—")
            notes = (e.get("notes") or "").replace("|", "/")[:60]
            lines.append(
                f"| {e['date']} | {_lv('bodyweight')}kg | {_lv('sleep')}h | "
                f"{_lv('food_quality')}/10 | {_lv('food_quality')}/10 | {sun} | {notes} |"
            )
        lines.append("")
    else:
        lines += ["## Daily Log", "", "*No daily log entries found.*", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# analysis.md
# ---------------------------------------------------------------------------

def generate_analysis_md(analysis: dict) -> str:
    lines = [
        "# Analysis & Insights",
        f"Generated: {date.today().isoformat()}",
        "",
    ]

    # --- Stall detection ---
    stalls = analysis.get("stalls")
    lines.append("## Stall Detection")
    lines.append("")
    if stalls:
        stalled = {k: v for k, v in stalls.items() if v["status"] == "STALL"}
        progressing = {k: v for k, v in stalls.items() if v["status"] == "PROGRESSING"}
        insufficient = {k: v for k, v in stalls.items() if v["status"] == "INSUFFICIENT_DATA"}

        if stalled:
            lines.append("### Stalled Lifts")
            for ex, s in stalled.items():
                lines.append(
                    f"- **{ex}**: {s['recent_peak']}kg e1RM (last {s['weeks_seen']} weeks shown) "
                    f"— delta {s['delta']:+.1f}kg vs prior period (W{s['last_week']})"
                )
        else:
            lines.append("*No stalls detected.*")

        if progressing:
            lines.append("")
            lines.append("### Progressing Lifts")
            for ex, s in progressing.items():
                lines.append(
                    f"- **{ex}**: {s['last_e1rm']}kg e1RM — +{s['delta']}kg vs prior period (W{s['last_week']})"
                )

        if insufficient:
            lines.append("")
            lines.append("### Insufficient Data (<3 weeks)")
            for ex, s in insufficient.items():
                n = s["weeks_seen"]
                e = f"{s['last_e1rm']}kg" if s["last_e1rm"] else "no data"
                lines.append(f"- {ex}: {n} week(s) seen, last e1RM {e}")
    else:
        lines.append("*No data.*")
    lines.append("")

    # --- Volume trends ---
    vol = analysis.get("volume")
    lines.append("## Volume Trends (Working Sets per Muscle Group)")
    lines.append("")
    if vol and vol.get("weeks"):
        weeks = vol["weeks"]
        header = "| Muscle Group | " + " | ".join(f"W{w}" for w in weeks) + " |"
        sep = "|---|" + "|".join(["---"] * len(weeks)) + "|"
        lines += [header, sep]
        for group, sets_list in sorted(vol["groups"].items()):
            row = " | ".join(str(s) for s in sets_list)
            lines.append(f"| {group} | {row} |")
        total_row = " | ".join(str(t) for t in vol["total_sets_per_week"])
        lines.append(f"| **TOTAL** | {total_row} |")
    else:
        lines.append("*No volume data.*")
    lines.append("")

    # --- Load index ---
    li = analysis.get("load_index")
    lines.append("## Load Index")
    lines.append("")
    if li and li.get("signal") != "INSUFFICIENT_DATA":
        signal_label = {"HIGH": "HIGH — consider deload soon", "NORMAL": "NORMAL", "LOW": "LOW — below baseline"}.get(li["signal"], li["signal"])
        lines.append(f"- Recent 4-week avg volume: {li['recent_4w_avg']:,} kg·sets·reps")
        lines.append(f"- 8-week baseline avg: {li['baseline_8w_avg']:,} kg·sets·reps")
        lines.append(f"- Load ratio: {li['ratio']} → **{signal_label}**")
        lines.append("- *Ratio >1.3 = deload signal; <0.7 = undertraining*")
    else:
        lines.append("*Insufficient data for load index (need 12+ weeks).*")
    lines.append("")

    # --- 1RM trajectory ---
    traj = analysis.get("trajectory")
    lines.append("## 1RM Trajectory vs Program Targets")
    lines.append("")
    if traj:
        for lift, t in traj.items():
            on_track = "on track ✓" if t["on_track"] is True else ("behind ✗" if t["on_track"] is False else "")
            current_e1rm = f"{t['current_e1rm']}kg" if t["current_e1rm"] else "no data"
            current_target = f"{t['current_target']}kg" if t["current_target"] else "no target"
            gap = f"{t['gap_to_goal']:+.1f}kg to goal" if t["gap_to_goal"] is not None else ""
            lines.append(f"### {lift}")
            lines.append(f"Start: {t['start_kg']}kg → Goal: {t['goal_kg']}kg | Current e1RM: {current_e1rm} | Target: {current_target} | {on_track} | {gap}")
            lines.append("")
            if t["by_week"]:
                lines.append("| Week | Target | Actual e1RM | Δ |")
                lines.append("|------|--------|-------------|---|")
                for w in sorted(t["by_week"].keys()):
                    bw = t["by_week"][w]
                    tgt = f"{bw['target_kg']}kg" if bw["target_kg"] else "—"
                    act = f"{bw['actual_e1rm']}kg" if bw["actual_e1rm"] else "—"
                    if bw["target_kg"] and bw["actual_e1rm"]:
                        delta = round(bw["actual_e1rm"] - bw["target_kg"], 1)
                        delta_str = f"{delta:+.1f}kg"
                    else:
                        delta_str = "—"
                    lines.append(f"| {w} | {tgt} | {act} | {delta_str} |")
            lines.append("")
    else:
        lines.append("*No trajectory data.*")

    # --- Adherence ---
    adh = analysis.get("adherence")
    lines.append("## Adherence")
    lines.append("")
    if adh:
        ov = adh["overall"]
        r4 = adh["last_4_weeks"]
        lines.append(f"- Overall: {ov['done']}/{ov['planned']} ({ov['rate']*100:.0f}%)")
        lines.append(f"- Last 4 weeks: {r4['done']}/{r4['planned']} ({r4['rate']*100:.0f}%)")
        lines.append("")
        lines.append("| Week | Done | Planned | Rate |")
        lines.append("|------|------|---------|------|")
        for w, v in sorted(adh["by_week"].items()):
            lines.append(f"| {w} | {v['done']} | {v['planned']} | {v['rate']*100:.0f}% |")
    else:
        lines.append("*No adherence data.*")
    lines.append("")

    # --- Sleep correlation ---
    corr = analysis.get("sleep_correlation")
    lines.append("## Sleep → Performance Correlation")
    lines.append("")
    if corr:
        lines.append(f"*Based on {corr['n']} paired observations (prior-night HRV/sleep vs session RPE)*")
        lines.append("")
        if corr.get("hrv_vs_rpe"):
            h = corr["hrv_vs_rpe"]
            lines.append(f"- **HRV vs RPE**: r = {h['r']} ({h['strength']} {h['direction']}) — {corr['note']}")
        if corr.get("sleep_vs_rpe"):
            s = corr["sleep_vs_rpe"]
            lines.append(f"- **Sleep hours vs RPE**: r = {s['r']} ({s['strength']} {s['direction']})")
    else:
        lines.append("*Not enough RPE + Garmin paired data yet (need ≥20 observations).*")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File writer (local + optional Drive)
# ---------------------------------------------------------------------------

def write_output(
    output_dir: Path,
    files: dict[str, str],
    drive_folder_id: Optional[str] = None,
) -> None:
    """
    Write markdown files to output_dir (always) and optionally upload to Google Drive.

    Args:
        output_dir: local directory (created if needed)
        files: {filename: content} e.g. {"training_log.md": "..."}
        drive_folder_id: if set, upload each file to this Drive folder
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for filename, content in files.items():
        path = output_dir / filename
        path.write_text(content, encoding="utf-8")
        print(f"  [export] Written locally: {path}")

    if not drive_folder_id:
        return

    try:
        _upload_to_drive(files, drive_folder_id)
    except Exception as e:
        print(f"  [export] Drive upload failed (non-fatal): {e}")
        print(f"  [export] Files are available locally in {output_dir}")


def _upload_to_drive(files: dict[str, str], folder_id: str) -> None:
    """Upload or update files in the given Drive folder."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload
    from sheets import get_credentials

    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    # List existing files in the folder so we can update instead of duplicate
    existing: dict[str, str] = {}  # {filename: file_id}
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            existing[f["name"]] = f["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    for filename, content in files.items():
        media = MediaInMemoryUpload(
            content.encode("utf-8"),
            mimetype="text/markdown",
            resumable=False,
        )
        if filename in existing:
            service.files().update(
                fileId=existing[filename],
                media_body=media,
            ).execute()
            print(f"  [export] Updated Drive: {filename}")
        else:
            metadata = {"name": filename, "parents": [folder_id]}
            service.files().create(
                body=metadata,
                media_body=media,
                fields="id",
            ).execute()
            print(f"  [export] Created Drive: {filename}")
