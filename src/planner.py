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

    # Annual arc (cross-program, year-level view — refreshed monthly)
    coach_state = memory_data.get("coach_state", {})
    annual_arc = coach_state.get("ANNUAL_ARC", {}).get("summary", "")
    if annual_arc:
        sections.append(f"ANNUAL ARC (year-level perspective, updated monthly)\n{annual_arc}")

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
# Annual arc — year-level retrospective + 12-month forward view
# ---------------------------------------------------------------------------

def run_annual_arc(memory_data: dict, dry_run: bool = False) -> str:
    """
    Build (or refresh) the ANNUAL_ARC Coach State domain.

    Covers:
    - Year-in-review: 1RM gains, key milestones, program completion rates
    - Cross-program patterns: what worked, what stalled, why
    - Goal status: are multi-year targets on track?
    - Next 12 months: recommended program sequence to hit long-term goals

    Gated to once per month (30-day check). Uses Sonnet for quality reasoning.
    Writes to ANNUAL_ARC Coach State domain, read by the weekly planning prompt.
    """
    from datetime import datetime as _dt, timedelta
    from memory import read_coach_state, upsert_coach_state, read_lift_history

    coach_state = memory_data.get("coach_state") or read_coach_state()
    existing_arc = coach_state.get("ANNUAL_ARC", {}).get("summary", "")
    last_updated = coach_state.get("ANNUAL_ARC", {}).get("last_updated", "")

    # Monthly gate — year-level data doesn't change week to week
    if last_updated and not dry_run:
        try:
            days_since = (date.today() - _dt.strptime(last_updated[:10], "%Y-%m-%d").date()).days
            if days_since < 28:
                print(f"  Annual arc: last updated {days_since}d ago — skipping (monthly update).")
                return existing_arc
        except (ValueError, TypeError):
            pass

    today = date.today()
    one_year_ago = str(today.replace(year=today.year - 1))

    # --- Lift history: last 12 months, summarised per lift ---
    try:
        lift_history = read_lift_history(after_date=one_year_ago, limit=1000)
    except Exception:
        lift_history = memory_data.get("lift_history", [])

    from config import KEY_LIFTS
    tracked_lifts = memory_data.get("tracked_lifts")
    main_lifts = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                  if tl.get("lift_type", "MAIN") == "MAIN"] if tracked_lifts else KEY_LIFTS

    lift_summary_lines = []
    for _domain, lift in main_lifts:
        readings = []
        for row in lift_history:
            if lift.lower() in row.get("Exercise", "").lower():
                try:
                    readings.append((row.get("Date", ""), float(row.get("Est 1RM", "") or 0)))
                except (ValueError, TypeError):
                    pass
        if readings:
            readings.sort(key=lambda x: x[0])
            start_date, start_val = readings[0]
            end_date, end_val = readings[-1]
            gain = end_val - start_val
            lift_summary_lines.append(
                f"  {lift}: {start_val:.1f}kg [{start_date}] → {end_val:.1f}kg [{end_date}] "
                f"({gain:+.1f}kg over {len(readings)} sessions)"
            )

    lift_section = "\n".join(lift_summary_lines) or "  (no lift history in past 12 months)"

    # --- Program history ---
    prog_history = memory_data.get("program_history", [])
    prog_lines = []
    for p in prog_history:
        prog_lines.append(
            f"  {p.get('Program', '?')} | {p.get('Start Date', '?')} → {p.get('End Date', '?')} "
            f"| {p.get('Weeks Completed', '?')} weeks | {p.get('Notes', '')}"
        )
    prog_section = "\n".join(prog_lines) or "  (no past programs)"

    # Current program info
    current_program = ""
    sheet_registry = memory_data.get("sheet_registry", [])
    for entry in sheet_registry:
        if entry.get("Status", "").upper() == "ACTIVE":
            current_program = (
                f"{entry.get('Name', '?')} | started {entry.get('Start Date', '?')} "
                f"| {entry.get('Total Weeks', '?')} weeks"
            )
            break

    # Long-term goals
    lt_goals = memory_data.get("long_term_goals", "")
    profile = memory_data.get("athlete_profile", "")

    # Previous arc (for continuity)
    prev_arc_section = (
        f"PREVIOUS ANNUAL ARC (from {last_updated[:10] if last_updated else 'never'}):\n"
        f"{existing_arc[:600]}\n" if existing_arc else ""
    )

    prompt = (
        f"You are {ATHLETE_NAME}'s long-term strength coach building the annual coaching arc.\n\n"
        f"TODAY: {today}\n\n"
        f"ATHLETE PROFILE:\n{profile[:400]}\n\n"
        f"LONG-TERM GOALS:\n{lt_goals[:400] if lt_goals else '(see athlete profile)'}\n\n"
        f"CURRENT PROGRAM: {current_program or '(see program history)'}\n\n"
        f"PROGRAM HISTORY:\n{prog_section}\n\n"
        f"LIFT TRAJECTORY (last 12 months, est. 1RM):\n{lift_section}\n\n"
        f"{prev_arc_section}"
        f"Write a concise annual arc update (5-8 sentences). Cover:\n"
        f"1. Year-in-review: what were the biggest strength gains and key milestones?\n"
        f"2. Cross-program patterns: what training approaches worked best, what stalled?\n"
        f"3. Goal status: for each long-term goal, is the athlete ahead/on-track/behind?\n"
        f"4. Next 12 months: what program sequence and phase structure will get them to their goals?\n"
        f"Be specific with numbers. No fluff. This is internal coach thinking."
    )

    print(f"  Running annual arc update (Sonnet)...")
    if dry_run:
        print(f"  [DRY RUN] Would generate annual arc from {len(lift_history)} lift sessions.")
        return "[DRY RUN] Annual arc would be generated."

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        arc_text = response.content[0].text.strip()
        cost = (
            response.usage.input_tokens / 1_000_000 * 3.0
            + response.usage.output_tokens / 1_000_000 * 15.0
        )
        upsert_coach_state("ANNUAL_ARC", arc_text, "HIGH")
        print(f"  ANNUAL_ARC Coach State written ({len(arc_text)} chars, cost ${cost:.4f}).")
        return arc_text
    except Exception as e:
        print(f"  Annual arc failed (non-fatal): {e}")
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
# Bi-monthly Steer Co
# ---------------------------------------------------------------------------

STEER_CO_GATE_DAYS = 60  # trigger every ~2 months


def _initiate_steer_co(memory_data: dict, dry_run: bool = False) -> None:
    """
    Initiate a bi-monthly steering co (steer co) when it's been ~60 days since the last one.

    Step 1: Coach reviews last 60 days and writes STEER_CO_DRAFT to Coach State.
    Step 2: Sends a Telegram message to open the conversation.
    The Railway bot handles follow-up questions over the next 1-2 days.
    Call run_steer_co_finalize() (via --steer-co-finalize) to synthesize and send the email.
    """
    from datetime import datetime as _dt, timedelta
    from memory import read_coach_state, upsert_coach_state, read_lift_history, read_coach_log

    today = date.today()
    coach_state = read_coach_state()

    # Gate: check when last steer co was
    last_steer_co_str = coach_state.get("LAST_STEER_CO", {}).get("summary", "")
    if last_steer_co_str:
        try:
            last_steer_co = _dt.strptime(last_steer_co_str[:10], "%Y-%m-%d").date()
            if (today - last_steer_co).days < STEER_CO_GATE_DAYS:
                print(f"  Steer co: last was {last_steer_co_str[:10]}, "
                      f"{(today - last_steer_co).days} days ago — not yet due.")
                return
        except (ValueError, TypeError):
            pass

    print(f"  Steer co: due (last: {last_steer_co_str or 'never'}). Initiating...")

    # Gather 60-day retrospective data
    cutoff = today - timedelta(days=60)
    try:
        lift_rows = read_lift_history(limit=200, after_date=cutoff)
        lift_summary = f"{len(lift_rows)} lift sessions in last 60 days"
    except Exception:
        lift_rows = []
        lift_summary = "lift history unavailable"

    try:
        coach_logs = read_coach_log(limit=10)
        log_summary = "\n".join(
            f"  [{e.get('Date', '')}] {e.get('Key Observations', '')[:120]}"
            for e in coach_logs
        )
    except Exception:
        log_summary = "coach log unavailable"

    goals = memory_data.get("long_term_goals", "")
    life_ctx = memory_data.get("life_context", [])
    life_summary = "\n".join(f"  [{c['date']}] {c['context']}" for c in life_ctx[-5:])
    coach_focus = memory_data.get("coach_focus", [])
    open_items = [f"  {f['Item']}" for f in coach_focus if f.get("Status") == "OPEN"][:8]

    draft_prompt = (
        f"You are {ATHLETE_NAME}'s long-term strength coach. It's time for a bi-monthly steer co — "
        f"a private internal review of the last 60 days and what needs to change or stay the same.\n\n"
        f"This is your PRIVATE DRAFT — it will not be sent to the athlete. "
        f"Write a structured coaching analysis:\n"
        f"1. WHAT WORKED: key wins, positive trends, effective strategies (last 60 days)\n"
        f"2. WHAT STALLED: plateaus, missed sessions, recurring issues\n"
        f"3. GOAL TRAJECTORY: are we on track for stated goals? Any goal adjustments needed?\n"
        f"4. OPEN QUESTIONS for the athlete (things you need to understand better)\n"
        f"5. PROPOSED CHANGES: adjustments to coaching approach or program for next 60 days\n\n"
        f"DATA:\n"
        f"  Lift activity: {lift_summary}\n"
        f"  Long-term goals: {goals}\n"
        f"  Recent life context:\n{life_summary}\n"
        f"  Recent coach observations:\n{log_summary}\n"
        f"  Open watch items:\n" + "\n".join(open_items or ["  none"]) + "\n\n"
        f"Keep this under 500 words. Be honest and analytical."
    )

    if dry_run:
        print(f"  [DRY RUN] Would generate steer co draft and send opening Telegram.")
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Step 1: Generate internal draft
    try:
        draft_result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": draft_prompt}],
        )
        draft = draft_result.content[0].text.strip()
        upsert_coach_state("STEER_CO_DRAFT", draft[:800], "HIGH")
        print("  Steer co draft written to Coach State.")
    except Exception as e:
        print(f"  Steer co draft failed: {e}")
        return

    # Step 2: Send opening Telegram message
    opening_prompt = (
        f"You are {ATHLETE_NAME}'s strength coach. It's been about 2 months — time for a steer co. "
        f"Write a short Telegram message (3-4 sentences) to open the conversation. "
        f"Explain what a steer co is (quick check-in to align on what's working and goals). "
        f"Ask one opener question: how he's feeling about the program overall. "
        f"Mention you'll have a few follow-up questions over the next day or two. "
        f"Direct tone. No fluff.\n"
        f"Write it now:"
    )
    try:
        opening_result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": opening_prompt}],
        )
        opening_msg = opening_result.content[0].text.strip()
        from telegram_utils import send_telegram_message
        if send_telegram_message(opening_msg):
            print(f"  Steer co opening sent: {opening_msg[:80]}")
        else:
            print("  Steer co opening: Telegram send failed.")
    except Exception as e:
        print(f"  Steer co opening failed: {e}")


def run_steer_co_finalize(dry_run: bool = False) -> None:
    """
    Synthesize the steer co conversation + send a comprehensive email.
    Call this ~48h after _initiate_steer_co(), or manually via --steer-co-finalize.

    Reads STEER_CO_DRAFT from Coach State + recent Telegram log (last 48h),
    synthesizes everything into a comprehensive coaching document,
    and sends it as an email.
    """
    from datetime import datetime as _dt, timedelta
    from memory import (read_coach_state, upsert_coach_state, read_telegram_log_since,
                        read_athlete_profile, read_long_term_goals)
    from gmail import send_email

    today = date.today()
    print(f"[{today}] Finalizing steer co...")

    coach_state = read_coach_state()
    draft = coach_state.get("STEER_CO_DRAFT", {}).get("summary", "")
    if not draft:
        print("  Steer co finalize: no draft found in Coach State — run --think first.")
        return

    # Read recent Telegram conversation (last 3 days)
    cutoff = today - timedelta(days=3)
    try:
        tg_logs = read_telegram_log_since(cutoff, limit=80)
        tg_text = "\n".join(
            f"  [{e.get('Timestamp', '')[:10]}] {e.get('Direction', '')}: {e.get('Message', '')[:200]}"
            for e in tg_logs
        )
    except Exception:
        tg_text = "Telegram log unavailable."

    profile = read_athlete_profile() or ""
    goals = read_long_term_goals() or ""

    synth_prompt = (
        f"You are {ATHLETE_NAME}'s long-term strength coach. You just completed a steer co — "
        f"a bi-monthly alignment conversation with your athlete. "
        f"Now write the full steer co document to send him as a comprehensive email.\n\n"
        f"STRUCTURE (use these headers exactly):\n"
        f"1. The Last Two Months — what happened, honestly\n"
        f"2. What's Working — keep doing these things\n"
        f"3. What Needs to Change — honest assessment\n"
        f"4. Goals Update — are we still aiming at the right targets? any revisions?\n"
        f"5. The Next Two Months — what we're focusing on, why\n"
        f"6. My Commitments to You — what you (the coach) will specifically do\n"
        f"7. What I Need From You — what requires athlete action\n\n"
        f"Use the internal draft and the conversation for grounding. "
        f"Write in the coach's voice — honest, direct, specific. Not generic motivational fluff.\n\n"
        f"INTERNAL COACHING DRAFT:\n{draft}\n\n"
        f"CONVERSATION (recent Telegram):\n{tg_text}\n\n"
        f"ATHLETE PROFILE:\n{profile}\n\n"
        f"LONG-TERM GOALS:\n{goals}\n\n"
        f"Target length: 600-900 words. This goes in an email — prose, not bullet points. "
        f"Make it feel like a real coaching conversation document, not a corporate report."
    )

    if dry_run:
        print(f"  [DRY RUN] Would synthesize steer co and send email.")
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": synth_prompt}],
        )
        steer_co_doc = result.content[0].text.strip()
    except Exception as e:
        print(f"  Steer co synthesis failed: {e}")
        return

    # Send as email
    try:
        subject = f"Steer Co — {today.strftime('%B %Y')}"
        send_email(subject=subject, body=steer_co_doc)
        print(f"  Steer co email sent: {subject}")
        # Mark steer co done
        upsert_coach_state("LAST_STEER_CO", str(today), "HIGH")
        # Clear the draft
        upsert_coach_state("STEER_CO_DRAFT", "", "LOW")
    except Exception as e:
        print(f"  Steer co email send failed: {e}")


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
