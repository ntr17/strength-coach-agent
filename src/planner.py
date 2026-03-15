"""
Strategic planning pass — the coach's long-term brain.

Runs weekly (Sundays) via --think flag. Reads all memory and program data,
thinks through the next 6-12 months, and writes an updated strategic plan
to the Coach Memory Sheet (Strategic Plan + Planning Notes tabs).

This is invisible to the athlete. It silently informs daily emails.
"""

import re
import sys
from datetime import date

import anthropic

from config import ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL, resolve_program_start_date


# ---------------------------------------------------------------------------
# Prompt builder for the planning pass
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = f"""You are the internal strategic mind of {ATHLETE_NAME}'s long-term strength coach.

This is not a coaching email. This is your private thinking session — the athlete never sees this directly.

You are building and maintaining a multi-month coaching roadmap. You think in phases (8-16 week blocks), you track where the athlete is headed, and you make sure today's decisions serve tomorrow's goals.

Your job here:
1. Assess the current state honestly: progress, risks, momentum
2. Update the multi-month phased plan with realistic targets and timelines
3. Write candid internal notes about what needs to happen and why
4. Flag anything that could derail the long-term trajectory

You are not motivating anyone. You are thinking clearly about what this person needs, over the next 6-12 months, based on data.
"""


def _build_planning_prompt(program_data: dict, memory_data: dict, week_num: int) -> str:
    """Build the user message for the planning pass."""
    today = date.today()
    sections = []

    # Current state snapshot
    sections.append(
        f"TODAY: {today.strftime('%A, %B %d, %Y')}\n"
        f"CURRENT WEEK: Week {week_num} of current program (started {resolve_program_start_date()})\n"
        f"ATHLETE: {ATHLETE_NAME}"
    )

    # Athlete profile + goals
    profile = memory_data.get("athlete_profile", "")
    if profile:
        sections.append(f"ATHLETE PROFILE\n{profile}")

    lt_goals = memory_data.get("long_term_goals", "")
    if lt_goals:
        sections.append(f"LONG-TERM GOALS\n{lt_goals}")

    # Program targets (goals from program sheet)
    goals = program_data.get("goals", {})
    if goals:
        goal_lines = []
        for lift, g in goals.items():
            goal_lines.append(f"  {lift}: {g.get('start', '?')} → {g.get('goal', '?')}")
        sections.append("PROGRAM GOALS (start → target)\n" + "\n".join(goal_lines))

    # Current lift trajectory
    lift_history = memory_data.get("lift_history", [])
    if lift_history:
        # Summarize recent 1RMs for tracked lifts (MAIN only for planning context)
        from memory import read_tracked_lifts
        tracked = memory_data.get("tracked_lifts") or read_tracked_lifts()
        lifts_for_plan = [(tl["domain"], tl["match_pattern"]) for tl in tracked
                          if tl.get("lift_type", "MAIN") == "MAIN"]
        lift_lines = []
        for _domain, lift in lifts_for_plan:
            readings = []
            for row in lift_history:
                if lift.lower() in row.get("Exercise", "").lower():
                    est = row.get("Est 1RM", "")
                    d = row.get("Date", "")
                    if est:
                        try:
                            readings.append((d, float(est)))
                        except (ValueError, TypeError):
                            pass
            if readings:
                last = readings[-1]
                first = readings[0]
                trend = ""
                if len(readings) >= 3:
                    recent_vals = [v for _, v in readings[-3:]]
                    change = recent_vals[-1] - recent_vals[0]
                    trend = f" (last 3 sessions: {change:+.1f}kg)"
                lift_lines.append(
                    f"  {lift}: {last[1]:.1f}kg est. 1RM [{last[0]}]{trend} | "
                    f"started at {first[1]:.1f}kg [{first[0]}]"
                )
        if lift_lines:
            sections.append("LIFT TRAJECTORY (estimated 1RM)\n" + "\n".join(lift_lines))

    # Health trends
    health_log = memory_data.get("health_log", [])
    if health_log:
        recent_health = health_log[-30:]
        bw_vals = []
        sleep_vals = []
        for e in recent_health:
            try:
                bw_vals.append(float(e.get("Bodyweight (kg)", "") or ""))
            except (ValueError, TypeError):
                pass
            try:
                sleep_vals.append(float(e.get("Sleep (hrs)", "") or ""))
            except (ValueError, TypeError):
                pass
        health_lines = []
        if bw_vals:
            health_lines.append(f"  Bodyweight: avg {sum(bw_vals)/len(bw_vals):.1f}kg (last 30 days)")
        if sleep_vals:
            health_lines.append(f"  Sleep: avg {sum(sleep_vals)/len(sleep_vals):.1f}h (last 30 days)")
        if health_lines:
            sections.append("HEALTH TRENDS\n" + "\n".join(health_lines))

    # Life context
    life_ctx = memory_data.get("life_context", [])
    if life_ctx:
        ctx_lines = "\n".join(f"  [{c['date']}] {c['context']}" for c in life_ctx[-5:])
        sections.append(f"RECENT LIFE CONTEXT\n{ctx_lines}")

    # Program history
    prog_history = memory_data.get("program_history", [])
    if prog_history:
        ph_lines = "\n".join(
            f"  {p.get('Program', '?')} | {p.get('Start Date', '?')} → {p.get('End Date', '?')} | "
            f"{p.get('Weeks Completed', '?')} weeks | {p.get('Notes', '')}"
            for p in prog_history
        )
        sections.append(f"PROGRAM HISTORY\n{ph_lines}")

    # Current strategic plan (for continuity)
    strategic_plan = memory_data.get("strategic_plan", [])
    plan_rows = [p for p in strategic_plan if not p.get("Phase", "").startswith("#")]
    if plan_rows:
        plan_lines = []
        for p in plan_rows:
            plan_lines.append(
                f"  {p.get('Phase', '?')} | {p.get('Start Date', '?')} → {p.get('End Date', '?')} | "
                f"Focus: {p.get('Focus', '?')} | Targets: {p.get('Key Targets', '?')}"
            )
        sections.append("CURRENT STRATEGIC PLAN (previous version)\n" + "\n".join(plan_lines))

    # Recent planning notes
    planning_notes = memory_data.get("planning_notes", [])
    if planning_notes:
        last_note = planning_notes[-1]
        sections.append(f"LAST PLANNING SESSION [{last_note['date']}]\n{last_note['notes'][:800]}")

    prompt = "\n\n---\n\n".join(sections)
    prompt += """

---

Now do your planning work. Output EXACTLY this structure:

STRATEGIC PHASES (one per line, pipe-separated):
Phase Name | Start Date | End Date | Focus | Key Targets | Notes

PLANNING NOTES:
[Your candid internal assessment — several paragraphs. Cover:
- Current momentum and risks
- Whether the long-term goal is still on track
- What needs to happen in each upcoming phase for the long-term plan to work
- Any lifestyle/health factors that will affect periodization
- What you'll be watching closely in the next 4 weeks
- Honest take on athlete's trajectory — no fluff]

Use realistic dates. Phases typically run 8-16 weeks. Think through what comes after the current program ends.
Start Date and End Date format: YYYY-MM-DD
"""
    return prompt


# ---------------------------------------------------------------------------
# Parse Claude output
# ---------------------------------------------------------------------------

def _parse_planning_output(output: str) -> tuple[list[dict], str]:
    """
    Parse Claude's planning output into:
    - list of phase dicts
    - free-form planning notes string
    """
    phases = []
    notes = ""

    # Split on PLANNING NOTES marker
    parts = re.split(r"PLANNING NOTES\s*:", output, maxsplit=1, flags=re.IGNORECASE)
    phases_section = parts[0]
    notes = parts[1].strip() if len(parts) > 1 else ""

    # Parse phases section
    phases_start = re.search(r"STRATEGIC PHASES.*?:", phases_section, re.IGNORECASE)
    if phases_start:
        phases_text = phases_section[phases_start.end():]
    else:
        phases_text = phases_section

    today = str(date.today())
    for line in phases_text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Phase Name"):
            continue
        parts_line = [p.strip() for p in line.split("|")]
        if len(parts_line) >= 2:
            phases.append({
                "Phase": parts_line[0] if len(parts_line) > 0 else "",
                "Start Date": parts_line[1] if len(parts_line) > 1 else "",
                "End Date": parts_line[2] if len(parts_line) > 2 else "",
                "Focus": parts_line[3] if len(parts_line) > 3 else "",
                "Key Targets": parts_line[4] if len(parts_line) > 4 else "",
                "Notes": parts_line[5] if len(parts_line) > 5 else "",
                "Last Updated": today,
            })

    return phases, notes


# ---------------------------------------------------------------------------
# Main planning pass
# ---------------------------------------------------------------------------

def run_planning_pass(program_data: dict, memory_data: dict, week_num: int,
                      dry_run: bool = False) -> tuple[list[dict], str]:
    """
    Run the strategic planning pass.
    Returns (phases, planning_notes).
    If dry_run=True, prints output but does not write to Coach Memory.
    """
    print("  Building strategic planning prompt...")
    user_message = _build_planning_prompt(program_data, memory_data, week_num)

    print("  Running planning pass with Claude...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2500,
        system=PLANNER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
    )
    output = message.content[0].text

    if dry_run:
        print("\n--- STRATEGIC PLANNING OUTPUT ---")
        print(output)
        print("--- END PLANNING OUTPUT ---\n")

    phases, notes = _parse_planning_output(output)

    if not dry_run:
        from memory import upsert_strategic_plan, append_planning_notes
        if phases:
            upsert_strategic_plan(phases)
            print(f"  Updated Strategic Plan: {len(phases)} phases.")
        if notes:
            append_planning_notes(notes)
            print("  Appended planning notes.")
    else:
        print(f"  [DRY RUN] Would write {len(phases)} phases to Strategic Plan tab.")
        print(f"  [DRY RUN] Would append {len(notes)} chars of planning notes.")

    return phases, notes


# ---------------------------------------------------------------------------
# Telegram log summarization — weekly compression to prevent prompt bloat
# ---------------------------------------------------------------------------

def run_telegram_summarization(memory_data: dict, dry_run: bool = False) -> str:
    """
    Summarize Telegram Log entries older than 14 days into a TELEGRAM_HISTORY
    Coach State domain. Prevents prompt bloat as months of conversation accumulate.

    Called weekly from run_think() on Sundays.
    Uses Haiku (cheap) — the summary is ~3-5 sentences, not a verbatim transcript.
    Returns the summary text (empty string if nothing to summarize).
    """
    from datetime import timedelta
    from memory import read_telegram_log_since, upsert_coach_state, read_coach_state

    today = date.today()
    cutoff = today - timedelta(days=14)

    # Check existing TELEGRAM_HISTORY so we can append rather than replace
    existing_state = memory_data.get("coach_state") or read_coach_state()
    existing_summary = existing_state.get("TELEGRAM_HISTORY", {}).get("summary", "")

    # Read older entries (before cutoff). We read all and filter locally.
    all_log = memory_data.get("telegram_log", [])
    if not all_log:
        print("  Telegram summarization: no log entries — skipping.")
        return ""

    old_entries = []
    for entry in all_log:
        try:
            from datetime import datetime as _dt
            entry_date = _dt.strptime(entry.get("Date", "")[:10], "%Y-%m-%d").date()
            if entry_date < cutoff:
                old_entries.append(entry)
        except (ValueError, TypeError):
            pass

    if not old_entries:
        print("  Telegram summarization: no entries older than 14 days — skipping.")
        return ""

    # Build condensed message log for Haiku
    log_lines = []
    for e in old_entries[-150:]:  # cap at 150 to keep prompt size bounded
        direction = "Nacho" if e.get("Direction", "").upper() == "IN" else "Coach"
        log_lines.append(f"  [{e.get('Date', '')}] {direction}: {e.get('Message', '').strip()[:200]}")
    log_text = "\n".join(log_lines)

    prompt = (
        f"Summarize the following Telegram coaching conversation history from the past weeks.\n"
        f"Focus on: key training events mentioned, athlete concerns raised, coach advice given, "
        f"patterns in athlete mood/performance, any recurring topics or unresolved threads.\n"
        f"Write 4-6 dense sentences. No headers, just a paragraph.\n\n"
        f"EXISTING SUMMARY (from previous compression — extend it, don't discard it):\n"
        f"{existing_summary or '(none yet)'}\n\n"
        f"NEW MESSAGES TO INCORPORATE (older than 14 days):\n{log_text}"
    )

    print(f"  Telegram summarization: compressing {len(old_entries)} old entries with Haiku...")
    if dry_run:
        print(f"  [DRY RUN] Would compress {len(old_entries)} entries into TELEGRAM_HISTORY.")
        return f"[DRY RUN] {len(old_entries)} entries would be summarized."

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = result.content[0].text.strip()
        upsert_coach_state("TELEGRAM_HISTORY", summary, "MEDIUM")
        print(f"  TELEGRAM_HISTORY Coach State written ({len(summary)} chars).")
        return summary
    except Exception as e:
        print(f"  Telegram summarization failed (non-fatal): {e}")
        return ""


# ---------------------------------------------------------------------------
# Per-lift deep dive (triggered by plateau detection)
# ---------------------------------------------------------------------------

def run_lift_deep_dive(lift_name: str, full_history: list[dict],
                        system_prompt: str) -> str:
    """
    When a plateau is detected, run a focused analysis of the full lift history.
    Returns a short analysis string to inject into the reasoning pass context.
    """
    if not full_history:
        return ""

    # Build a compact history table
    rows = []
    for row in full_history:
        rows.append(
            f"  {row.get('Date', '?')} | W{row.get('Week', '?')} | "
            f"{row.get('Prescribed Weight', '?')} | actual: {row.get('Actual Weight/Reps', '?')} | "
            f"done: {row.get('Completed', '?')} | est1RM: {row.get('Est 1RM', '?')} | "
            f"notes: {row.get('Notes', '')}"
        )

    history_text = "\n".join(rows[-40:])  # last 40 sessions for this lift
    prompt = (
        f"PLATEAU ALERT — {lift_name}\n\n"
        f"Full history for {lift_name}:\n{history_text}\n\n"
        f"In 3-5 sentences: what's the pattern here? Is this a real plateau or noise? "
        f"What should change — volume, intensity, technique cue, or just more time?"
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",  # fast + cheap for this sub-task
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    dry = "--dry-run" in sys.argv

    print("Loading data for planning pass...")
    from sheets import read_program_data
    from memory import read_all
    from config import compute_current_week, resolve_program_start_date as _rsd

    week_num = compute_current_week(_rsd())
    program_data = read_program_data(week_num=week_num)
    memory_data = read_all()

    phases, notes = run_planning_pass(program_data, memory_data, week_num, dry_run=dry)
    print(f"\nPlanning pass complete. {len(phases)} phases, {len(notes)} chars of notes.")
