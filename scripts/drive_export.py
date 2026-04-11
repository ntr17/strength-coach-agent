"""
drive_export.py — Markdown generators for the Claude Project.

Four detail files + one combined briefing:
  training_log.md    — session history, PRs, last 16 weeks
  program_context.md — program targets, current position, goal gaps
  health_recovery.md — Garmin + daily log, last 30 days
  analysis.md        — stall detection, volume, load, trajectory, adherence, correlations
  BRIEFING.md        — compact combined briefing optimized for Claude to read at session start

File writing and Drive upload are handled by pipeline.py and drive_client.py.

DB-native variants (no Google Sheet dependency):
  generate_program_context_md_from_db  — reads state.json + profile.json + strength_estimates
  generate_briefing_md_from_db         — full briefing from DB data only
"""

from datetime import date
from collections import defaultdict


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
# BRIEFING.md — combined, Claude-optimized
# ---------------------------------------------------------------------------

def generate_briefing_md(
    records: list[dict],
    prs: dict,
    program_data: dict,
    week_num: int,
    total_weeks: int,
    garmin_data: list[dict],
    daily_log: list[dict],
    analysis: dict,
) -> str:
    """
    Generate a compact BRIEFING.md combining all four files into one
    Claude-optimized document. Intended to be read at the start of every
    coaching conversation in the Claude Project.
    """
    today = date.today().isoformat()
    lines = [
        "# Coaching Briefing",
        f"Date: {today}",
        "",
    ]

    # ------------------------------------------------------------------
    # 1. Where You Are
    # ------------------------------------------------------------------
    pct_done = round(week_num / total_weeks * 100) if total_weeks else 0
    weeks_left = total_weeks - week_num
    block_num = ((week_num - 1) // 5 + 1) if week_num else 1

    # Days since last session
    done_records_with_date = [r for r in records if r["done"] is True and r["date"]]
    days_since = None
    if done_records_with_date:
        last_session = max(r["date"] for r in done_records_with_date)
        days_since = (date.today() - last_session).days

    lines += [
        "## Where You Are",
        "",
        f"- Program: 30-Week Strength | Week **{week_num}** of {total_weeks} ({pct_done}% done, {weeks_left}w left)",
        f"- Block: {block_num}",
    ]
    if days_since is not None:
        lines.append(f"- Last session: {days_since} day(s) ago ({last_session})")
    else:
        lines.append("- Last session: unknown")
    lines.append("")

    # ------------------------------------------------------------------
    # 2. Strength (e1RM estimates for main lifts)
    # ------------------------------------------------------------------
    main_lifts = ["Squat", "Bench Press", "Deadlift", "OHP"]
    goals = program_data.get("goals", {})

    lines += ["## Strength (e1RM estimates)", ""]
    lines.append("| Lift | Current e1RM | PR date | Goal | Gap |")
    lines.append("|------|-------------|---------|------|-----|")

    traj = (analysis.get("trajectory") or {})
    for lift in main_lifts:
        # Try trajectory first (has current e1RM from recent weeks), then fall back to PRs
        t = traj.get(lift)
        pr = prs.get(lift)

        if t and t.get("current_e1rm"):
            e1rm_str = f"{t['current_e1rm']}kg"
            pr_date = str(pr["date"]) if pr and pr.get("date") else "—"
            goal_str = f"{t['goal_kg']}kg" if t.get("goal_kg") else "—"
            gap_str = f"{t['gap_to_goal']:+.1f}kg" if t.get("gap_to_goal") is not None else "—"
        elif pr:
            e1rm_str = f"{pr['e1rm']}kg"
            pr_date = str(pr["date"]) if pr.get("date") else "—"
            goal_info = goals.get(lift, {})
            goal_str = goal_info.get("goal", "—")
            gap_str = "—"
        else:
            e1rm_str = "no data"
            pr_date = "—"
            goal_str = goals.get(lift, {}).get("goal", "—")
            gap_str = "—"

        lines.append(f"| {lift} | {e1rm_str} | {pr_date} | {goal_str} | {gap_str} |")
    lines.append("")

    # ------------------------------------------------------------------
    # 3. Health (7-day)
    # ------------------------------------------------------------------
    lines += ["## Health (7-day)", ""]
    garmin_sorted = sorted(garmin_data, key=lambda g: g["date"], reverse=True) if garmin_data else []
    recent_7 = garmin_sorted[:7]

    if recent_7:
        def _avg(key):
            vals = [g[key] for g in recent_7 if g.get(key) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        sleep_avg = _avg("sleep_hrs")
        hrv_avg = _avg("hrv_ms")
        steps_avg = _avg("steps")
        rhr_avg = _avg("resting_hr")

        lines.append("| Metric | 7-day avg |")
        lines.append("|--------|-----------|")
        lines.append(f"| Sleep (h) | {sleep_avg or '—'} |")
        lines.append(f"| HRV (ms) | {hrv_avg or '—'} |")
        lines.append(f"| RHR (bpm) | {rhr_avg or '—'} |")
        lines.append(f"| Steps | {int(steps_avg) if steps_avg else '—'} |")
    else:
        # Fall back to daily log for bodyweight
        log_sorted = sorted(
            [e for e in daily_log if e.get("date") and e.get("bodyweight")],
            key=lambda e: e["date"],
            reverse=True,
        )
        if log_sorted:
            lines.append(f"- Bodyweight (latest): {log_sorted[0]['bodyweight']}kg ({log_sorted[0]['date']})")
        else:
            lines.append("*No Garmin or daily log data available.*")
    lines.append("")

    # ------------------------------------------------------------------
    # 4. Analysis
    # ------------------------------------------------------------------
    lines += ["## Analysis", ""]

    stalls = analysis.get("stalls") or {}
    stalled = {k: v for k, v in stalls.items() if v["status"] == "STALL"}
    progressing = {k: v for k, v in stalls.items() if v["status"] == "PROGRESSING"}

    if stalled:
        lines.append(f"**Stalls ({len(stalled)}):** " + ", ".join(
            f"{ex} ({v['recent_peak']}kg e1RM, {v['delta']:+.1f}kg)" for ex, v in stalled.items()
        ))
    else:
        lines.append("Stalls: none detected")

    if progressing:
        lines.append(f"**Progressing ({len(progressing)}):** " + ", ".join(
            f"{ex} (+{v['delta']}kg)" for ex, v in progressing.items()
        ))

    li = analysis.get("load_index") or {}
    load_signal = li.get("signal", "INSUFFICIENT_DATA")
    load_ratio = li.get("ratio")
    if load_ratio:
        lines.append(f"Load index: {load_ratio} → **{load_signal}**")
    else:
        lines.append(f"Load index: {load_signal}")

    adh = analysis.get("adherence") or {}
    if adh.get("last_4_weeks"):
        r4 = adh["last_4_weeks"]
        lines.append(f"Adherence (last 4w): {r4['done']}/{r4['planned']} ({r4['rate']*100:.0f}%)")
    lines.append("")

    # ------------------------------------------------------------------
    # 5. Open Items
    # ------------------------------------------------------------------
    lines += [
        "## Open Items",
        "",
        "- Check `system/threads.json` for any open coaching threads or pending flags.",
        "- Check `system/state.json` for active deload or travel week flags.",
        "",
    ]

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------
    lines += [
        "---",
        "",
        "*For full detail: training_log.md | program_context.md | health_recovery.md | analysis.md*",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# program_context.md — DB-native (no Google Sheet)
# ---------------------------------------------------------------------------

def generate_program_context_md_from_db(
    state: dict,
    profile: dict,
    estimates: dict,
    week_num: int,
    total_weeks: int,
) -> str:
    """
    Generate program_context.md from state.json + profile.json + latest estimates.
    Does NOT read from Google Sheet.

    Args:
        state:       system/state.json contents
        profile:     system/profile.json contents
        estimates:   load_latest_estimates() output
        week_num:    current week number
        total_weeks: total program weeks
    """
    today        = date.today().isoformat()
    pct_done     = round(week_num / total_weeks * 100) if total_weeks else 0
    weeks_left   = total_weeks - week_num
    block_num    = ((week_num - 1) // 5 + 1) if week_num else 1
    program_name = profile.get("current_program", {}).get("name", "30-Week Strength")

    lines = [
        "# Program Context",
        f"Generated: {today}",
        f"Program: {program_name}",
        f"Current Week: {week_num} of {total_weeks} (Block {block_num}) — {pct_done}% done, {weeks_left}w remaining",
        "",
    ]

    # --- Goal lifts ---
    goals_section = profile.get("goals", {})
    current_e1rms = goals_section.get("current_e1rm", {})
    target_e5rms  = goals_section.get("current_e5rm_targets", {})

    if target_e5rms:
        lines += ["## Goal Lifts", ""]
        lines.append("| Lift | Current e1RM | Target e5RM | Gap | Estimated e1RM |")
        lines.append("|------|-------------|-------------|-----|---------------|")
        for lift, target in sorted(target_e5rms.items()):
            current = current_e1rms.get(lift)
            est     = estimates.get(lift, {})
            est_e1rm = est.get("e1rm_kg")
            est_date = est.get("estimated_at", "")

            current_str = f"{current}kg" if current is not None else "—"
            target_str  = f"{target}kg"  if target  is not None else "—"
            est_str     = f"{est_e1rm}kg ({est_date[:10]})" if est_e1rm else "—"

            if current is not None and target is not None:
                try:
                    gap     = round(float(target) - float(current), 1)
                    gap_str = f"{gap:+.1f}kg"
                except (TypeError, ValueError):
                    gap_str = "—"
            else:
                gap_str = "—"

            lines.append(f"| {lift} | {current_str} | {target_str} | {gap_str} | {est_str} |")
        lines.append("")

    # --- Latest strength estimates (full table) ---
    if estimates:
        lines += ["## Strength Estimates (latest)", ""]
        lines.append("| Exercise | e1RM | e5RM | CI low | CI high | Date |")
        lines.append("|----------|------|------|--------|---------|------|")
        for ex, est in sorted(estimates.items()):
            e1rm = f"{est['e1rm_kg']}kg" if est.get("e1rm_kg") is not None else "—"
            e5rm = f"{est['e5rm_kg']}kg" if est.get("e5rm_kg") is not None else "—"
            lo   = f"{est['confidence_low']}kg"  if est.get("confidence_low")  is not None else "—"
            hi   = f"{est['confidence_high']}kg" if est.get("confidence_high") is not None else "—"
            dt   = str(est.get("estimated_at", "—"))[:10]
            lines.append(f"| {ex} | {e1rm} | {e5rm} | {lo} | {hi} | {dt} |")
        lines.append("")
    else:
        lines += [
            "## Strength Estimates",
            "",
            "*No estimates yet — run: `python scripts/estimate_strength.py --write`*",
            "",
        ]

    # --- Active flags from state ---
    flags = []
    if state.get("deload_week"):
        flags.append("DELOAD WEEK active")
    if state.get("travel_week"):
        flags.append("TRAVEL WEEK active")
    if state.get("injury_flag"):
        flags.append(f"INJURY FLAG: {state['injury_flag']}")

    if flags:
        lines += ["## Active Flags", ""]
        for f in flags:
            lines.append(f"- {f}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BRIEFING.md — DB-native (Claude reads this at the start of every conversation)
# ---------------------------------------------------------------------------

def generate_briefing_md_from_db(
    state: dict,
    profile: dict,
    estimates: dict,
    prs: dict,
    health_data: list,
    analysis: dict,
    week_num: int,
    total_weeks: int,
) -> str:
    """
    Generate BRIEFING.md — the main document Claude reads at every conversation start.

    Designed for coaching intelligence, not dashboards:
    - Absolute values + trends + context (not just snapshots)
    - Strength: week-by-week trajectory + rate + projection
    - Body comp: weight/fat history + trend
    - Recovery: vs personal baseline, not 7-day avg in isolation
    - Volume: weekly load trend
    - No medical section (uploaded directly to Claude Project)
    """
    today        = date.today().isoformat()
    block_num    = state.get("current_block", ((week_num - 1) // 5 + 1))
    program_name = profile.get("current_program", {}).get("name", "30-Week Strength")
    goals_section = profile.get("goals", {})
    target_e5rms  = goals_section.get("current_e5rm_targets", {})

    lines = [
        f"# BRIEFING — {today}",
        f"Week {week_num}/{total_weeks} (Block {block_num}) | {program_name}",
        "",
    ]

    # ------------------------------------------------------------------
    # 1. STRENGTH TRAJECTORY
    # Absolute + history + rate + projection per lift
    # ------------------------------------------------------------------
    lines += ["## Strength Trajectory", ""]

    strength_trends = analysis.get("strength_trends") or {}
    main_lifts = ["Squat", "Bench Press", "Deadlift", "OHP"]

    # e5RM → e1RM conversion factor (Brzycki 5-rep: e1RM = e5RM / 0.8706)
    E5_TO_E1 = 1 / 0.8706

    for lift in main_lifts:
        trend = strength_trends.get(lift, {})
        history = trend.get("history", [])  # [(week, e1rm), ...]
        current_e1rm = trend.get("current_e1rm")
        rate = trend.get("rate_kg_per_week")
        goal_e5rm = None
        try:
            goal_e5rm = float(target_e5rms.get(lift, 0)) if target_e5rms.get(lift) else None
        except (TypeError, ValueError):
            pass

        # Convert e5RM goal → e1RM equivalent for apples-to-apples projection
        goal_e1rm = round(goal_e5rm * E5_TO_E1, 1) if goal_e5rm else None

        # Projection: weeks to hit goal e1RM at current rate
        projection_str = ""
        if current_e1rm and goal_e1rm and rate and rate > 0:
            if current_e1rm >= goal_e1rm:
                projection_str = " → **goal e1RM already reached**"
            else:
                weeks_needed = (goal_e1rm - current_e1rm) / rate
                projection_str = f" → goal in ~{weeks_needed:.0f}w at current rate"
        elif current_e1rm and goal_e1rm and rate is not None and rate <= 0:
            projection_str = " → **NOT PROGRESSING toward goal**"

        rate_str = f"{rate:+.1f}kg/w" if rate is not None else "no data"
        current_str = f"{current_e1rm}kg" if current_e1rm else "no data"
        # Show e5RM goal to user (that's the program target), e1RM equivalent in parens
        goal_str = f"{goal_e5rm}kg x5 (≈{goal_e1rm}kg e1RM)" if goal_e5rm else "—"

        lines.append(f"### {lift}")
        lines.append(f"Current: **{current_str}** | Rate: {rate_str} | Goal: {goal_str}{projection_str}")
        lines.append("")

        if history:
            # Show last 8 weeks of history
            recent = history[-8:]
            hist_str = " → ".join(f"W{w}:{e1rm}kg" for w, e1rm in recent)
            lines.append(f"History: {hist_str}")
        lines.append("")

    # e5RM estimates table (for planning working weights)
    if estimates:
        lines += ["**e5RM estimates (for programming):**", ""]
        lines.append("| Lift | e1RM | e5RM | CI |")
        lines.append("|------|------|------|-----|")
        for ex in main_lifts:
            est = estimates.get(ex)
            if est:
                e1 = f"{est['e1rm_kg']}kg" if est.get("e1rm_kg") else "—"
                e5 = f"{est['e5rm_kg']}kg" if est.get("e5rm_kg") else "—"
                lo = est.get("confidence_low")
                hi = est.get("confidence_high")
                ci = f"{lo}–{hi}kg" if lo and hi else "—"
                lines.append(f"| {ex} | {e1} | {e5} | {ci} |")
        lines.append("")

    # ------------------------------------------------------------------
    # 2. BODY COMPOSITION
    # Weight + fat % + visceral — history + trend + rate
    # ------------------------------------------------------------------
    lines += ["## Body Composition", ""]

    bc = analysis.get("body_comp") or {}
    w_data  = bc.get("weight", {})
    fat_data = bc.get("body_fat_pct", {})
    visc_data = bc.get("visceral_fat", {})

    def _bc_row(label, data, unit, good_direction="stable"):
        current = data.get("current")
        rate = data.get("rate_kg_per_week") or data.get("rate_per_week")
        flag = data.get("flag", "")
        history = data.get("history", [])[-8:]  # last 8 entries

        if current is None:
            return f"**{label}**: no data"

        rate_str = f" ({rate:+.3f}{unit}/wk)" if rate else ""
        flag_str = f" **[{flag}]**" if flag else ""
        hist_pts = ", ".join(f"{v}{unit}" for _, v in history)
        return f"**{label}**: {current}{unit}{rate_str}{flag_str} | History: {hist_pts}"

    lines.append(_bc_row("Weight", w_data, "kg"))
    if fat_data.get("current") is not None:
        lines.append(_bc_row("Body fat", fat_data, "%"))
    if visc_data.get("current") is not None:
        lines.append(_bc_row("Visceral fat index", visc_data, ""))
    lines.append("")

    # ------------------------------------------------------------------
    # 3. RECOVERY / HEALTH
    # Absolute + vs baseline + trend — not just 7-day avg
    # ------------------------------------------------------------------
    lines += ["## Recovery & Health", ""]

    ht = analysis.get("health_trends") or {}

    def _health_row(label, data, unit, context_note=""):
        if not data:
            return f"**{label}**: no data"
        current = data.get("current")
        baseline = data.get("baseline_8w")
        pct = data.get("pct_vs_baseline")
        trend = data.get("trend")
        flag = data.get("flag")
        recent = data.get("recent_7", [])

        current_str = f"{current}{unit}" if current else "—"
        baseline_str = f"(baseline {baseline}{unit})" if baseline else ""
        pct_str = f"{pct:+.0f}% vs baseline" if pct is not None else ""
        trend_str = "↑" if trend == "UP" else ("↓" if trend == "DOWN" else "→" if trend else "")
        flag_str = f" **[{flag}]**" if flag else ""
        hist_str = ", ".join(str(v) for v in recent)

        return f"**{label}**: {current_str} {baseline_str} {pct_str} {trend_str}{flag_str} | Last 7: {hist_str}"

    lines.append(_health_row("HRV", ht.get("hrv"), "ms",
                             "Suppressed HRV = underrecovery. <15% of baseline = flag."))
    lines.append(_health_row("RHR", ht.get("rhr"), "bpm"))
    lines.append(_health_row("Sleep", ht.get("sleep"), "h",
                             "<6h = critical risk. 6-7h = marginal. 7h+ = OK."))
    lines.append("")

    # ------------------------------------------------------------------
    # 4. VOLUME LOAD TREND
    # Total kg lifted per week — trend matters
    # ------------------------------------------------------------------
    lines += ["## Volume Load", ""]

    li = analysis.get("load_index") or {}
    volumes = li.get("volume_per_week", [])
    weeks_v = li.get("weeks", [])
    signal = li.get("signal", "INSUFFICIENT_DATA")
    ratio = li.get("ratio")

    if volumes and weeks_v:
        vol_history = " → ".join(f"W{w}:{v:,}kg" for w, v in zip(weeks_v[-8:], volumes[-8:]))
        ratio_str = f" | ratio {ratio} → **{signal}**" if ratio else ""
        lines.append(f"Weekly kg·sets·reps: {vol_history}{ratio_str}")
    else:
        lines.append("*Insufficient volume data*")
    lines.append("")

    # ------------------------------------------------------------------
    # 5. STALLS & ADHERENCE
    # ------------------------------------------------------------------
    lines += ["## Training Status", ""]

    stalls = analysis.get("stalls") or {}
    stalled = {k: v for k, v in stalls.items() if v["status"] == "STALL"}
    progressing = {k: v for k, v in stalls.items() if v["status"] == "PROGRESSING"}

    if stalled:
        lines.append("**Stalls detected:** " + ", ".join(
            f"{ex} ({v['delta']:+.1f}kg, {v['weeks_seen']}w)" for ex, v in stalled.items()
        ))
    else:
        lines.append("Stalls: none")

    adh = analysis.get("adherence") or {}
    if adh.get("last_4_weeks"):
        r4 = adh["last_4_weeks"]
        lines.append(f"Adherence (last 4w): {r4['done']}/{r4['planned']} ({r4['rate']*100:.0f}%)")

    td = analysis.get("training_days") or {}
    if td.get("weeks"):
        history = " | ".join(f"W{w}:{d}d" for w, d in zip(td["weeks"], td["days_per_week"]))
        avg = td.get("avg_days")
        avg_str = f" (avg {avg}/week)" if avg is not None else ""
        lines.append(f"Training days/week: {history}{avg_str}")

    corr = analysis.get("sleep_correlation")
    if corr:
        if corr.get("hrv_vs_rpe"):
            h = corr["hrv_vs_rpe"]
            lines.append(f"HRV→RPE correlation: r={h['r']} ({h['strength']} {h['direction']}, n={corr['n']})")
    lines.append("")

    # ------------------------------------------------------------------
    # 6. OPEN ITEMS
    # ------------------------------------------------------------------
    lines += ["## Open Items", ""]
    if state.get("deload_week"):
        lines.append("- **DELOAD WEEK active** — load ceiling 70%")
    if state.get("travel_week"):
        lines.append("- **TRAVEL WEEK active**")
    if state.get("injury_flag"):
        lines.append(f"- **INJURY FLAG: {state['injury_flag']}**")
    lines.append("- Check `system/threads.json` for open decisions.")
    lines.append("")

    lines += [
        "---",
        "Detail files: training_log.md | health_recovery.md | analysis.md | program_context.md",
    ]

    return "\n".join(lines)
