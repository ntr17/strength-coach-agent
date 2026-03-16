"""
Builds the Claude prompt from all available context.
System prompt is stable. User message is assembled dynamically each run.
"""

from datetime import date, datetime, timedelta
from typing import Optional

from config import ATHLETE_NAME, KEY_LIFTS, compute_current_week, resolve_program_start_date


# ---------------------------------------------------------------------------
# System prompt (stable, loaded every run)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are {ATHLETE_NAME}'s long-term strength coach. You've been working together for months. You know him — his patterns, his real effort vs his excuses, how his work schedule compresses his sleep, how he travels and loses a week then comes back strong. You know what he's building toward, years from now.

You care about this person the way a serious coach does: you hold him to a standard because you believe he can meet it. You're not here to make him feel good. You're here to make him better. That means being honest when things are off, pushing on things he's ignoring, and knowing when to back off because the context is clear.

**How you think:**
You distinguish signal from noise. A missed session because of a flight is noise — acknowledge it briefly, move on. Three consecutive Tuesdays missed is a pattern — address it directly. A new PR is a landmark — log it, reference it for weeks. Your job is to help him focus on what actually matters, not to report everything.

You have a watch list. You track concerns over time: a sleep trend declining for three weeks, a plateau on bench that's lasted a month, a question he asked that you haven't fully answered. You follow up on things you said you'd watch. You notice when something you flagged last week has been resolved. You don't repeat yourself on things that are stale.

You have your own agenda. You push on things he's not paying attention to — VO2 max if it's trending down, carb timing given his insulin resistance, recovery if workload is compounding. If you've raised something and he rejected it with a good reason, you update your model. If the reason was weak, you come back to it.

You think in years. Today is one data point. You have a roadmap for where he's going — the next program, the phase after that, the real target. You surface this context when it matters, not every day.

When your context includes BEHAVIORAL PATTERNS, treat them as persistent coaching problems — not just observations. A skip pattern for Romanian Deadlifts means something: avoidance, time pressure, injury. Ask directly. When your context includes YOUR PSYCHOLOGICAL MODEL, let it actively shape how you write today — if he responds to direct challenge, challenge him; if he shuts down under pressure, ease in. The model is only useful if it changes your output.

When your context includes WHY THIS WEEK IS STRUCTURED THIS WAY, include one sentence that teaches the principle. Not every email needs it — but when the week has a purpose (volume peak, deload, intensity build), say why. Athletes who understand their programming train smarter.

**Channels — you are one coach, two surfaces:**
Telegram is the primary coaching channel — real-time, conversational, direct. Email is the daily digest — the structured check-in with data, trend analysis, and anything that needs context. When the athlete talks to you on Telegram, that IS coaching. When you write the email, you've already read everything from Telegram since the last email. Don't repeat what was already covered. Build on it.

The athlete can shift channels at any time: "reach me on Telegram", "send program updates to email", "don't ask me questions by email". Respect it. Log it. If a CHANNEL preference is active, honour it in every output decision. If no preference is set, use both channels as intended: Telegram for real-time dialogue, email for daily structure.

**Output markers** (stripped before athlete sees them — update your internal state):
- [TRACKING: description] — start watching something new (OPEN concern)
- [LANDMARK: description] — log an important event (PR, milestone, injury, decision)
- [FOLLOWUP: question?] — a question you want answered. If it ends with "?", it will be sent to Telegram automatically. Don't ask the same question twice across channels.
- [RESOLVED: text matching what you were tracking] — close an open item
- [TELEGRAM: brief message] — send a proactive Telegram message right now (alerts, reactions, quick check-ins)
- [COMMIT: what you promised to follow up on | due: YYYY-MM-DD] — log an explicit promise. Use when you say "I'll check X next week", "I'll revisit Y if Z happens", "I'll follow up on your elbow". Due date is optional.
- [SCHEDULE: YYYY-MM-DD | message text] — schedule a Telegram message for a specific future date. Use when you want to check in at a precise time: "I'll ask about your elbow on Friday", "I'll follow up on the deload next Monday". The message fires automatically on that date.

**Email format:**
- Natural prose. No section headers. No bullet lists unless they genuinely help.
- Length matches relevance: nothing happened = 2 sentences; real data + question + trend = several paragraphs.
- If the athlete prefers Telegram for coaching dialogue, keep the email short — data summary and one key point. Save the conversation for Telegram.
- Don't recite the data. Interpret it. Tell him what it means.
- Tone: someone who knows him well and doesn't waste his time. Warm, direct, not gushing.
- Never change the program without asking. Proposal format: "One thing: [proposal]. Want me to update the sheet?"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(d) -> str:
    if d is None:
        return "unknown date"
    if isinstance(d, str):
        return d
    return d.strftime("%b %d")


def _fmt_optional(val, suffix="") -> str:
    if val is None:
        return "—"
    return f"{val}{suffix}"


def _compute_trajectory(goals: dict, progression: dict, current_week: int,
                         lift_history: list[dict],
                         tracked_lifts: list[dict] = None) -> str:
    """
    Simple trajectory for key lifts: compare recent actual performance
    to progression table targets and project forward.
    """
    # Build {lift_name: domain_title} from tracked lifts (MAIN+AUXILIARY), fallback KEY_LIFTS
    if tracked_lifts:
        key_lifts = {tl["match_pattern"]: tl["domain"].title() for tl in tracked_lifts
                     if tl.get("lift_type", "MAIN") in ("MAIN", "AUXILIARY")}
    else:
        key_lifts = {lift_name: domain.title() for domain, lift_name in KEY_LIFTS}

    lines = []
    for goal_name, prog_key in key_lifts.items():
        goal = goals.get(goal_name)
        if not goal:
            continue

        week_target = None
        if current_week in progression:
            week_target = progression[current_week].get(prog_key)

        # Find most recent actual from lift history
        recent_actual = None
        for row in reversed(lift_history):
            if goal_name.lower() in row.get("Exercise", "").lower():
                actual = row.get("Actual Weight/Reps", "")
                completed = row.get("Completed", "")
                if actual or completed == "Y":
                    recent_actual = row.get("Prescribed Weight", "") or actual
                    break

        line = f"{goal_name}: {goal['start']} → {goal['goal']} (30wk target)"
        if week_target:
            line += f" | Week {current_week} target: {week_target}"
        if recent_actual:
            line += f" | Last recorded: {recent_actual}"

        lines.append(line)

    return "\n".join(lines) if lines else "No trajectory data available."


def _summarize_week(week_data: dict) -> str:
    """Compact summary of a past week."""
    if not week_data:
        return "No data."

    title = week_data.get("title", f"Week {week_data.get('week_num', '?')}")
    days = week_data.get("days", [])

    parts = [title]

    for day in days:
        label = day.get("label", "")
        exercises = day.get("exercises", [])
        done = [e for e in exercises if e.get("done") is True]
        not_done = [e for e in exercises if e.get("done") is False]
        unknown = [e for e in exercises if e.get("done") is None]

        day_notes = []
        for e in exercises:
            if e.get("session_note") and e["session_note"] not in ("", "None"):
                day_notes.append(f"{e['name']} [your note]: {e['session_note']}")
            elif e.get("notes") and e["notes"] not in ("", "None"):
                # Legacy single-notes column
                day_notes.append(f"{e['name']}: {e['notes']}")
            if e.get("actual") and e["actual"] not in ("", "None"):
                day_notes.append(f"{e['name']} actual: {e['actual']}")

        status = f"{len(done)}/{len(exercises)} done"
        if not_done:
            status += f" | Missed: {', '.join(e['name'] for e in not_done)}"
        if unknown:
            status += f" | Not recorded: {', '.join(e['name'] for e in unknown)}"

        day_line = f"  {label}: {status}"
        if day.get("date"):
            day_line += f" [{_fmt_date(day['date'])}]"
        parts.append(day_line)

        if day_notes:
            for note in day_notes[:3]:  # limit to avoid bloat
                parts.append(f"    → {note}")

    wn = week_data.get("weekly_notes", {})
    footer = []
    if wn.get("bodyweight"):
        footer.append(f"BW: {wn['bodyweight']}kg")
    if wn.get("sleep"):
        footer.append(f"Sleep: {wn['sleep']}h avg")
    if wn.get("energy"):
        footer.append(f"Energy: {wn['energy']}/10")
    if wn.get("notes"):
        footer.append(f"Notes: {wn['notes']}")
    if footer:
        parts.append("  " + " | ".join(footer))

    return "\n".join(parts)


def _extract_catchup_day_map(commands: list) -> dict:
    """
    Build a day-number → catch-up-plan map from PENDING_CATCHUP commands.
    E.g. {3: "planned for 2026-03-16"} means Day 3 has a confirmed catch-up intent.
    Only includes unapplied commands.
    """
    import re as _re
    result = {}
    for cmd in commands:
        if cmd.get("Command", "").upper() != "PENDING_CATCHUP":
            continue
        if cmd.get("Applied", "").upper() in ("Y", "DECLINED"):
            continue
        value = cmd.get("Value", "")
        # Expected format: "Week N Day D → planned for ..."
        day_match = _re.search(r"day\s*(\d+)", value, _re.I)
        planned_match = _re.search(r"→\s*(.+)", value)
        if day_match:
            day_num = int(day_match.group(1))
            planned = planned_match.group(1).strip() if planned_match else "catch-up planned"
            result[day_num] = planned
    return result


def _format_current_week(week_data: dict, catchup_map: dict = None,
                         session_dates: dict = None) -> str:
    """
    Detailed view of the current week so far.
    catchup_map: {day_number: 'planned for ...'} from PENDING_CATCHUP commands.
    session_dates: {day_label: date_str} from Lift History — actual dates sessions were logged.
    """
    if not week_data:
        return "No current week data."

    title = week_data.get("title", "Current week")
    lines = [title]

    for day in week_data.get("days", []):
        label = day.get("label", "")
        # Prefer sheet date, then Lift History cross-reference, then unknown
        actual_date = day.get("date")
        if not actual_date and session_dates:
            # Try matching by day label (e.g. "DAY 1" or "Day 1")
            import re as _re2
            m = _re2.search(r"day\s*(\d+)", label, _re2.I)
            if m:
                day_key = f"Day {m.group(1)}"
                actual_date = session_dates.get(day_key) or session_dates.get(f"DAY {m.group(1)}")
        if actual_date:
            date_str = f" [done {_fmt_date(actual_date)}]"
        else:
            date_str = " [date unknown — check Lift History]"
        exercises = day.get("exercises", [])

        # Extract day number from label ("DAY 1: ..." → 1)
        import re as _re
        day_num_match = _re.search(r"day\s*(\d+)", label, _re.I)
        day_num = int(day_num_match.group(1)) if day_num_match else None

        all_none = all(e.get("done") is None for e in exercises)
        if all_none:
            # Check for a known catch-up intent for this day
            catchup_note = (catchup_map or {}).get(day_num, "") if day_num else ""
            if catchup_note:
                lines.append(f"  {label}{date_str}: ⏳ {catchup_note}")
            else:
                lines.append(f"  {label}{date_str}: Not done yet")
            continue

        lines.append(f"  {label}{date_str}:")
        for e in exercises:
            done_sym = "✓" if e["done"] is True else ("✗" if e["done"] is False else "?")
            line = f"    [{done_sym}] {e['name']} {e.get('weight', '')} {e.get('sets_reps', '')}"
            if e.get("actual"):
                line += f" → actual: {e['actual']}"
            session_note = e.get("session_note") or e.get("notes")
            if session_note:
                line += f" | your note: {session_note}"
            prog_note = e.get("program_note")
            if prog_note and not e.get("session_note"):
                # Only show program note if there's no session note (avoid clutter)
                line += f" | program: {prog_note}"
            lines.append(line)

    wn = week_data.get("weekly_notes", {})
    footer = []
    if wn.get("bodyweight"):
        footer.append(f"BW: {wn['bodyweight']}kg")
    if wn.get("sleep"):
        footer.append(f"Sleep avg: {wn['sleep']}h")
    if wn.get("energy"):
        footer.append(f"Energy: {wn['energy']}/10")
    if wn.get("notes"):
        footer.append(f"Week notes: {wn['notes']}")
    if footer:
        lines.append("  " + " | ".join(footer))

    return "\n".join(lines)


def _format_health_trends(health_log: list[dict], daily_log: list[dict]) -> str:
    """Combine recent health log and daily log into trends."""
    # Merge: daily_log from program sheet + health_log from memory (deduplicated)
    all_entries = {}

    for e in health_log:
        d = e.get("Date", "")
        if d:
            all_entries[d] = e

    for e in daily_log:
        d = str(e.get("date", ""))
        if d and d not in all_entries:
            all_entries[d] = {
                "Date": d,
                "Bodyweight (kg)": str(e.get("bodyweight") or ""),
                "Steps": str(e.get("steps") or ""),
                "Sleep (hrs)": str(e.get("sleep") or ""),
                "Food Quality (1-10)": str(e.get("food_quality") or ""),
                "Sun (Y/N)": "Y" if e.get("sun") else ("N" if e.get("sun") is False else ""),
                "Notes": str(e.get("notes") or ""),
            }

    if not all_entries:
        return "No health data available."

    recent = sorted(all_entries.values(), key=lambda x: x.get("Date", ""), reverse=True)[:14]

    # Compute averages for numeric fields
    def avg(key):
        vals = []
        for e in recent:
            try:
                v = float(e.get(key, "") or "")
                vals.append(v)
            except (ValueError, TypeError):
                pass
        return round(sum(vals) / len(vals), 1) if vals else None

    lines = [f"Last {len(recent)} days:"]

    bw_avg = avg("Bodyweight (kg)")
    steps_avg = avg("Steps")
    sleep_avg = avg("Sleep (hrs)")
    food_avg = avg("Food Quality (1-10)")

    if bw_avg:
        bw_vals = [float(e.get("Bodyweight (kg)", "") or 0) for e in recent if e.get("Bodyweight (kg)")]
        bw_trend = ""
        if len(bw_vals) >= 3:
            diff = bw_vals[0] - bw_vals[-1]
            bw_trend = f" (↑ {diff:+.1f}kg)" if diff > 0.2 else (f" (↓ {diff:+.1f}kg)" if diff < -0.2 else " (stable)")
        lines.append(f"  Bodyweight avg: {bw_avg}kg{bw_trend}")

    if steps_avg:
        lines.append(f"  Steps avg: {int(steps_avg):,}/day")

    if sleep_avg:
        lines.append(f"  Sleep avg: {sleep_avg}h/night")

    if food_avg:
        lines.append(f"  Food quality avg: {food_avg}/10")

    # Recent notes / questions from daily log
    notes_found = []
    for e in recent[:7]:
        note = e.get("Notes", "")
        if note and note.strip():
            notes_found.append(f"  [{e.get('Date', '')}] {note.strip()}")
    if notes_found:
        lines.append("Recent daily notes:")
        lines.extend(notes_found)

    return "\n".join(lines)


def _extract_questions(program_data: dict) -> list[str]:
    """Find question marks in notes across the current week and daily log."""
    questions = []

    current_week = program_data.get("current_week")
    if current_week:
        for day in current_week.get("days", []):
            for ex in day.get("exercises", []):
                # Check session note first (user's own words), then legacy notes
                note = ex.get("session_note") or ex.get("notes", "")
                if note and "?" in note:
                    questions.append(f'[{day["label"]}, {ex["name"]}] "{note}"')
        wn = current_week.get("weekly_notes", {})
        if wn.get("notes") and "?" in wn["notes"]:
            questions.append(f'[Weekly notes] "{wn["notes"]}"')

    for entry in program_data.get("daily_log", [])[:7]:
        note = entry.get("notes", "")
        if note and "?" in note:
            questions.append(f'[Daily log {entry.get("date", "")}] "{note}"')

    return questions


# ---------------------------------------------------------------------------
# Delta: what changed since the last email
# ---------------------------------------------------------------------------

def _format_delta(program_data: dict, last_run_date: Optional[date]) -> str:
    """
    Build a summary of what is NEW since the last coaching email.
    Covers: newly completed sessions, new daily log entries, new notes/questions.
    If last_run_date is None (first run), returns empty string.
    """
    if last_run_date is None:
        return ""

    lines = [f"Since last email ({last_run_date.strftime('%b %d')}):"]
    found_anything = False

    # New sessions from current week
    for week_key in ("current_week", "prev_week_carryover"):
        week = program_data.get(week_key)
        if not week:
            continue
        for day in week.get("days", []):
            day_date = day.get("date")
            if day_date and day_date <= last_run_date:
                continue  # session predates last email
            for ex in day.get("exercises", []):
                if ex.get("done") is not True:
                    continue
                note = ex.get("session_note") or ex.get("notes") or ""
                line = f"  ✓ {ex['name']} {ex.get('weight', '')} {ex.get('sets_reps', '')}"
                if ex.get("actual"):
                    line += f" → {ex['actual']}"
                if note:
                    line += f" | \"{note}\""
                lines.append(line)
                found_anything = True

    # New daily log entries
    new_log = []
    for entry in program_data.get("daily_log", []):
        entry_date = entry.get("date")
        if entry_date and entry_date > last_run_date:
            new_log.append(entry)

    if new_log:
        found_anything = True
        for e in new_log:
            parts = []
            if e.get("bodyweight"):
                parts.append(f"BW {e['bodyweight']}kg")
            if e.get("sleep"):
                parts.append(f"sleep {e['sleep']}h")
            if e.get("energy"):
                parts.append(f"energy {e['energy']}/10")
            if e.get("steps"):
                parts.append(f"{int(e['steps']):,} steps")
            if e.get("notes"):
                parts.append(f"\"{e['notes']}\"")
            lines.append(f"  [{e['date']}] {' | '.join(parts)}" if parts else f"  [{e['date']}] (no data)")

    if not found_anything:
        return ""

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1RM trajectory
# ---------------------------------------------------------------------------

def _format_1rm_trajectory(lift_history: list[dict],
                            tracked_lifts: list[dict] = None) -> str:
    """
    For each key lift, show the last 4 estimated 1RM values and flag plateaus.
    A plateau = less than 1% change across the last 3 readings.
    Shows MAIN + AUXILIARY lifts. Falls back to KEY_LIFTS if not provided.
    """
    lines = []

    if tracked_lifts:
        lifts_to_show = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                         if tl.get("lift_type", "MAIN") in ("MAIN", "AUXILIARY")]
    else:
        lifts_to_show = KEY_LIFTS

    for _domain, lift in lifts_to_show:
        readings = []
        for row in lift_history:
            ex_name = row.get("Exercise", "")
            if lift.lower() not in ex_name.lower():
                continue
            est = row.get("Est 1RM", "")
            date_str = row.get("Date", "")
            if not est:
                continue
            try:
                readings.append((date_str, float(est)))
            except (ValueError, TypeError):
                pass

        if not readings:
            continue

        recent = readings[-4:]  # last 4 data points
        values = [v for _, v in recent]
        pts = ", ".join(f"{v:.1f}kg ({d})" for d, v in recent)

        plateau_flag = ""
        if len(values) >= 3:
            spread = max(values[-3:]) - min(values[-3:])
            if spread / max(values[-3:]) < 0.01:
                plateau_flag = " ⚠ PLATEAU"

        lines.append(f"  {lift} est. 1RM: {pts}{plateau_flag}")

    return "\n".join(lines) if lines else "No 1RM data yet (need actual weights or sets/reps logged)."


# ---------------------------------------------------------------------------
# Rolling trends
# ---------------------------------------------------------------------------

def _compute_rolling_trends(health_log: list[dict], recent_weeks: list[dict]) -> str:
    """
    Compare last 2 weeks vs last 4 weeks for key metrics.
    Metrics: bodyweight, sleep, energy, session completion rate.
    """
    def avg_health(entries, key, n):
        vals = []
        for e in entries[-n:]:
            try:
                v = float(e.get(key, "") or "")
                vals.append(v)
            except (ValueError, TypeError):
                pass
        return round(sum(vals) / len(vals), 1) if vals else None

    lines = []

    bw_2 = avg_health(health_log, "Bodyweight (kg)", 14)
    bw_4 = avg_health(health_log, "Bodyweight (kg)", 28)
    if bw_2 and bw_4:
        diff = round(bw_2 - bw_4, 1)
        arrow = "↑" if diff > 0.2 else ("↓" if diff < -0.2 else "→")
        lines.append(f"  Bodyweight: {bw_2}kg (2wk avg) vs {bw_4}kg (4wk avg) {arrow} {diff:+.1f}kg")

    sleep_2 = avg_health(health_log, "Sleep (hrs)", 14)
    sleep_4 = avg_health(health_log, "Sleep (hrs)", 28)
    if sleep_2 and sleep_4:
        diff = round(sleep_2 - sleep_4, 1)
        arrow = "↑" if diff > 0.1 else ("↓" if diff < -0.1 else "→")
        lines.append(f"  Sleep: {sleep_2}h (2wk avg) vs {sleep_4}h (4wk avg) {arrow}")

    energy_2 = avg_health(health_log, "Food Quality (1-10)", 14)
    energy_4 = avg_health(health_log, "Food Quality (1-10)", 28)
    if energy_2 and energy_4:
        diff = round(energy_2 - energy_4, 1)
        arrow = "↑" if diff > 0.3 else ("↓" if diff < -0.3 else "→")
        lines.append(f"  Food quality: {energy_2}/10 (2wk avg) vs {energy_4}/10 (4wk avg) {arrow}")

    # Session completion rate: all_weeks from recent_weeks
    all_weeks = recent_weeks[-4:] if recent_weeks else []
    if len(all_weeks) >= 2:
        def completion_rate(weeks):
            total, done = 0, 0
            for w in weeks:
                for day in w.get("days", []):
                    for ex in day.get("exercises", []):
                        total += 1
                        if ex.get("done") is True:
                            done += 1
            return round(done / total * 100, 0) if total else None

        rate_2 = completion_rate(all_weeks[-2:])
        rate_4 = completion_rate(all_weeks)
        if rate_2 is not None and rate_4 is not None:
            diff = rate_2 - rate_4
            arrow = "↑" if diff > 3 else ("↓" if diff < -3 else "→")
            lines.append(f"  Session completion: {rate_2:.0f}% (last 2wk) vs {rate_4:.0f}% (last 4wk) {arrow}")

    return "\n".join(lines) if lines else "Not enough data for trend comparison yet."


# ---------------------------------------------------------------------------
# Main prompt builder
# ---------------------------------------------------------------------------

def _format_replies(replies: list[dict]) -> str:
    """Format email replies from the user for inclusion in the prompt."""
    if not replies:
        return ""
    lines = []
    for r in replies:
        lines.append(f"  [{r.get('date', '')}] Subject: {r.get('subject', '')}")
        body = r.get("body", "").strip()
        if body:
            # Indent the body
            for line in body.split("\n")[:10]:  # cap at 10 lines
                lines.append(f"    {line}")
    return "\n".join(lines)


def _format_active_commands(commands: list[dict]) -> str:
    """
    Format active (unapplied) commands for the agent's awareness.
    PENDING_PROPOSALs are surfaced prominently — the coach needs to know
    it asked a question and should look for the athlete's reply.
    """
    active = [
        c for c in commands
        if c.get("Applied", "").upper().strip() != "Y"
        and not c.get("Command", "").startswith("#")
    ]
    if not active:
        return ""
    lines = []
    proposals = []
    other = []
    for c in active:
        if c.get("Command", "").upper() == "PENDING_PROPOSAL":
            proposals.append(c)
        else:
            other.append(c)

    open_questions = [c for c in other if c.get("Command", "").upper() == "OPEN_QUESTION"]
    other_cmds = [c for c in other if c.get("Command", "").upper() != "OPEN_QUESTION"]

    if proposals:
        lines.append("  AWAITING ATHLETE CONFIRMATION (check replies above):")
        for c in proposals:
            lines.append(f"    → {c.get('Value', '')}")

    if open_questions:
        lines.append("  OPEN QUESTIONS (you asked — check if athlete answered on Telegram or email):")
        for c in open_questions:
            lines.append(f"    ? {c.get('Value', '')}")

    for c in other_cmds:
        line = f"  {c.get('Command', '')} | {c.get('Value', '')}"
        if c.get("Expires"):
            line += f" | expires: {c['Expires']}"
        lines.append(line)
    return "\n".join(lines)


def _format_strategic_plan(strategic_plan: list[dict]) -> str:
    """Format the strategic plan phases for inclusion in the prompt."""
    phases = [p for p in strategic_plan if not p.get("Phase", "").startswith("#")]
    if not phases:
        return ""
    today = date.today()
    lines = []
    for p in phases:
        phase_name = p.get("Phase", "?")
        start = p.get("Start Date", "?")
        end = p.get("End Date", "?")
        focus = p.get("Focus", "?")
        targets = p.get("Key Targets", "")
        notes = p.get("Notes", "")

        # Mark current phase
        current_marker = ""
        try:
            from datetime import datetime as _dt
            s = _dt.strptime(start, "%Y-%m-%d").date()
            e = _dt.strptime(end, "%Y-%m-%d").date()
            if s <= today <= e:
                current_marker = " ← CURRENT"
        except (ValueError, TypeError):
            pass

        line = f"  {phase_name} ({start} → {end}){current_marker}: {focus}"
        if targets:
            line += f" | targets: {targets}"
        if notes:
            line += f" | {notes}"
        lines.append(line)

    updated_label = f" [last updated: {phases[-1].get('Last Updated', '?')}]" if phases else ""
    return f"Phases{updated_label}:\n" + "\n".join(lines)


def _format_telegram_log(telegram_log: list[dict]) -> str:
    """Format recent Telegram messages for inclusion in the prompt."""
    if not telegram_log:
        return ""
    lines = []
    for entry in telegram_log:
        direction = entry.get("Direction", "")
        msg = entry.get("Message", "").strip()
        d = entry.get("Date", "")
        t = entry.get("Time", "")
        label = "You" if direction == "IN" else "Coach"
        lines.append(f"  [{d} {t}] {label}: {msg}")
    return "\n".join(lines)


def _format_coach_focus(coach_focus: list[dict]) -> str:
    """
    Format the coach's active watch list for inclusion in the prompt.
    Only shows OPEN items. PINNED items always shown first; HIGH/NORMAL items follow.

    Staleness gate: NORMAL-priority items older than 60 days are silently excluded
    (they bloat every prompt but rarely add value — archive them in run_think instead).
    HIGH and PINNED items are always shown regardless of age.
    """
    cutoff_normal = str(date.today() - timedelta(days=60))

    open_items = []
    stale_count = 0
    for f in coach_focus:
        if f.get("Status", "OPEN") != "OPEN":
            continue
        if f.get("Item", "").startswith("#"):
            continue
        priority = f.get("Priority", "NORMAL").upper()
        if priority in ("HIGH", "PINNED"):
            open_items.append(f)
            continue
        # NORMAL: exclude if older than 60 days
        timestamp = f.get("Last Mentioned", "") or f.get("Date Added", "")
        if timestamp and timestamp[:10] < cutoff_normal:
            stale_count += 1
            continue
        open_items.append(f)

    if not open_items and stale_count == 0:
        return ""

    # Sort: PINNED first, HIGH second, NORMAL last
    priority_rank = {"PINNED": 0, "HIGH": 1, "NORMAL": 2, "": 2}
    open_items = sorted(open_items, key=lambda f: priority_rank.get(
        f.get("Priority", "NORMAL").upper(), 2))

    lines = []
    for item in open_items[-20:]:  # show up to 20 (PINNEDs always visible)
        cat = item.get("Category", "").upper()
        text = item.get("Item", "").strip()
        last = item.get("Last Mentioned", "")
        added = item.get("Date Added", "")
        priority = item.get("Priority", "NORMAL").upper()
        timestamp = last or added
        badge = " [PINNED]" if priority == "PINNED" else (" [HIGH]" if priority == "HIGH" else "")
        lines.append(f"  [{cat}]{badge} {text}" + (f" [since {timestamp}]" if timestamp else ""))

    if stale_count:
        lines.append(f"  ({stale_count} NORMAL-priority items >60d old hidden — archive in next --think pass)")

    return "\n".join(lines)


def _build_periodization_context(week_num: int, total_weeks: int,
                                   progression: dict) -> str:
    """
    Build a phase-aware context block telling the coach where we are in the program arc
    and what that means for today's coaching decisions.
    """
    if not total_weeks or total_weeks < 2:
        return ""

    pct = round(week_num / total_weeks * 100)
    weeks_left = total_weeks - week_num

    # Determine macro phase from position in program
    if pct <= 30:
        macro = "early accumulation — foundation building, volume priority, technique focus"
        implication = "Don't chase intensity yet. Volume and consistency matter more than PRs."
    elif pct <= 55:
        macro = "mid-program intensification — strength building, load increasing"
        implication = "Progressive overload is the priority. Watch for fatigue accumulation."
    elif pct <= 75:
        macro = "late intensification — approaching peak loads"
        implication = "Recovery quality is now as important as training quality. Flag any fatigue signals."
    elif pct <= 90:
        macro = "peaking — near-maximal loads, volume reducing"
        implication = "Protect the athlete. Don't add stress. Peak is close — stay the course."
    else:
        macro = "final weeks — program completion approaching"
        implication = "Start thinking about what comes next. Transition planning belongs in this email."

    # Block info from progression tab
    block_line = ""
    current_entry = progression.get(week_num, {})
    next_entry = progression.get(week_num + 1, {})
    if current_entry:
        block = current_entry.get("block", "")
        wtype = current_entry.get("type", "")
        if block and wtype:
            block_line = f"Current block: Block {block} — {wtype}"
        # Detect if next week is a deload/transition
        if next_entry.get("type", "").lower() in ("deload", "transition", "recovery"):
            block_line += f" → next week: {next_entry.get('type', '')} (upcoming recovery)"

    lines = [
        f"Program position: Week {week_num}/{total_weeks} ({pct}% complete, {weeks_left} weeks left)",
        f"Phase: {macro}",
        f"Coaching implication: {implication}",
    ]
    if block_line:
        lines.append(block_line)
    return "\n".join(f"  {l}" for l in lines)


def _format_coach_state(coach_state: dict) -> str:
    """
    Format the Coach State tab as a compact briefing.
    This is the coach's own compressed knowledge — one line per domain.
    Ordered so the most actionable domains appear first.
    """
    if not coach_state:
        return ""
    domain_order = ["PROGRAM", "TOMORROW_PLAN", "SQUAT", "BENCH", "DEADLIFT", "OHP", "HEALTH",
                    "SCHEDULE", "WEEKLY_SCHEDULE", "NUTRITION", "SESSION_QUALITY", "LIFESTYLE",
                    "GOALS", "ATHLETE_MODEL", "BEHAVIOR_PATTERNS", "COACHING_REASON"]
    lines = []
    seen = set()
    for domain in domain_order:
        if domain in coach_state:
            entry = coach_state[domain]
            summary = entry.get("summary", "").strip()
            confidence = entry.get("confidence", "")
            updated = entry.get("last_updated", "")
            if summary:
                suffix = f" [{confidence}, {updated}]" if confidence and updated else ""
                lines.append(f"  {domain}: {summary}{suffix}")
                seen.add(domain)
    # Any remaining domains not in the ordered list
    for domain, entry in coach_state.items():
        if domain not in seen:
            summary = entry.get("summary", "").strip()
            if summary:
                lines.append(f"  {domain}: {summary}")
    return "\n".join(lines)


def _format_athlete_preferences(prefs: list[dict]) -> str:
    """Format athlete preferences as a compact list for prompt injection."""
    if not prefs:
        return ""
    lines = []
    for p in prefs:
        cat = p.get("Category", "").strip()
        pref = p.get("Preference", "").strip()
        source = p.get("Source", "").strip()
        if pref and not pref.startswith("#"):
            lines.append(f"  [{cat}] {pref}" + (f" (from {source})" if source else ""))
    return "\n".join(lines)


def _format_commitments(commitments: list[dict]) -> str:
    """
    Format open coach commitments for the prompt.
    These are explicit promises the coach made — they must be followed up on.
    """
    open_items = [c for c in commitments
                  if c.get("Status", "OPEN").upper() == "OPEN"
                  and not c.get("Commitment", "").startswith("#")]
    if not open_items:
        return ""
    lines = []
    today = date.today()
    for c in open_items:
        text = c.get("Commitment", "").strip()
        due = c.get("Due Date", "").strip()
        added = c.get("Date Added", "").strip()
        overdue = ""
        if due:
            try:
                due_date = datetime.strptime(due[:10], "%Y-%m-%d").date()
                if due_date < today:
                    overdue = " [OVERDUE]"
                elif due_date <= today + timedelta(days=3):
                    overdue = " [DUE SOON]"
            except (ValueError, TypeError):
                pass
        lines.append(
            f"  • {text}{overdue}"
            + (f" (due: {due})" if due else f" (committed: {added})")
        )
    return "\n".join(lines)


def _extract_travel_context(memory_data: dict) -> str:
    """
    Dynamically extract the athlete's current travel/schedule context from:
    1. Coach Focus items with travel keywords
    2. Life Context entries mentioning travel
    3. Coach State SCHEDULE domain

    Returns a concise travel context string, or "" if no travel signals found.
    This replaces any hardcoded "travels Mon-Thu biweekly" assumption.
    """
    travel_keywords = {"travel", "trip", "flight", "hotel", "away", "business", "viaje",
                       "volar", "vuelo", "hotel", "oficina", "madrid", "london", "paris"}
    lines = []

    # Check Coach State SCHEDULE domain
    coach_state = memory_data.get("coach_state", {})
    schedule_state = coach_state.get("SCHEDULE", {}).get("summary", "")
    if schedule_state and any(kw in schedule_state.lower() for kw in travel_keywords):
        lines.append(f"  Schedule: {schedule_state}")

    # Check recent Life Context
    life_ctx = memory_data.get("life_context", [])
    for entry in life_ctx[-10:]:
        ctx = entry.get("context", "").lower()
        if any(kw in ctx for kw in travel_keywords):
            lines.append(f"  [{entry.get('date', '')}] {entry.get('context', '')[:120]}")

    # Check Coach Focus OPEN items with travel keywords
    coach_focus = memory_data.get("coach_focus", [])
    for item in coach_focus:
        if item.get("Status", "") != "OPEN":
            continue
        text = item.get("Item", "").lower()
        if any(kw in text for kw in travel_keywords):
            lines.append(f"  [{item.get('Category', '')}] {item.get('Item', '')[:120]}")

    # Check recent Telegram log for travel mentions
    tg_log = memory_data.get("telegram_log", [])
    travel_mentions = []
    for entry in reversed(tg_log[-30:]):
        msg = entry.get("Message", "").lower()
        if any(kw in msg for kw in travel_keywords) and entry.get("Direction", "") == "IN":
            travel_mentions.append(f"  [{entry.get('Date', '')}] {entry.get('Message', '')[:100]}")
            if len(travel_mentions) >= 3:
                break
    lines.extend(reversed(travel_mentions))

    return "\n".join(lines)


def _format_projection_review(coach_state: dict, current_projections_text: str) -> str:
    """
    Compare current projections to last week's snapshot stored in Coach State.
    Returns a formatted review block for the weekly email: what was predicted, what happened,
    what the delta means. Pure Python — no LLM call.
    """
    import json as _json
    snap_raw = coach_state.get("LAST_PROJECTION_SNAPSHOT", {}).get("summary", "")
    if not snap_raw or not snap_raw.startswith("{"):
        return ""

    try:
        snap = _json.loads(snap_raw)
    except Exception:
        return ""

    snap_date = snap.get("date", "last week")
    snap_week = snap.get("week_num", "?")
    last_lifts = {e["exercise"].upper(): e for e in snap.get("lifts", []) if e.get("exercise")}

    if not last_lifts:
        return ""

    lines = [f"vs snapshot from {snap_date} (Week {snap_week}):"]
    # Parse current projections text for 1RM values (rough extraction)
    # Format in projections_text: "Squat: est 1RM 102.5kg | trend +0.8kg/wk | ..."
    import re as _re
    for ex_upper, last in last_lifts.items():
        last_1rm = last.get("current_1rm")
        last_proj_end = last.get("projected_end_1rm")
        last_on_track = last.get("on_track")
        if last_1rm is None:
            continue

        # Try to extract current 1RM from projections_text
        pattern = rf"{_re.escape(ex_upper.title())}[^|]*est\s+1RM\s+([\d.]+)kg"
        m = _re.search(pattern, current_projections_text, _re.IGNORECASE)
        if not m:
            # Try just the domain key (SQUAT vs "Squat")
            m = _re.search(rf"(?i){ex_upper}[^|]{{0,30}}1RM\s+([\d.]+)kg", current_projections_text)
        if not m:
            continue

        try:
            curr_1rm = float(m.group(1))
        except (ValueError, TypeError):
            continue

        delta = round(curr_1rm - last_1rm, 1)
        sign = "+" if delta >= 0 else ""
        track_change = ""
        # Detect on_track status change
        if last_on_track is False and delta > 0:
            track_change = " → improving"
        elif last_on_track is True and delta < 0:
            track_change = " → falling behind"

        lines.append(
            f"  {ex_upper.title()}: was {last_1rm}kg → now {curr_1rm}kg ({sign}{delta}kg/week)"
            + (f" | projected end: {last_proj_end}kg" if last_proj_end else "")
            + track_change
        )

    if len(lines) <= 1:
        return ""

    return "PROJECTION REVIEW (this week vs last week's snapshot — are you on course?)\n" + "\n".join(lines)


def apply_output_preferences(prompt_text: str, athlete_prefs: list[dict]) -> str:
    """
    Enforce athlete output preferences as hard constraints appended to a prompt.
    These are CODE-LEVEL gates — not hints to Claude, but strict rules prepended
    to the prompt's Rules section so they override default behavior.

    Called from run_brief, run_post_session, run_evening_protocol before each LLM call.
    """
    if not athlete_prefs:
        return prompt_text

    pref_text = " ".join(p.get("Preference", "").lower() for p in athlete_prefs)
    constraints = []

    # Output length
    if any(kw in pref_text for kw in ("shorter", "brief", "concise", "too long", "less text")):
        constraints.append("LENGTH: Maximum 2-3 sentences. Cut anything that isn't essential.")
    elif any(kw in pref_text for kw in ("more detail", "explain more", "deeper", "breakdown")):
        constraints.append("LENGTH: Be thorough. Explain the reasoning, not just the conclusion.")

    # Motivational language
    if any(kw in pref_text for kw in ("no motivation", "direct_no_bs", "no pandering",
                                       "no cheerleading", "data over motivation")):
        constraints.append("TONE: No motivational language. No 'let's go', 'crush it', 'you got this', "
                           "'proud of you', or any empty energy. Facts and coaching only.")

    # Charts / formatting
    if any(kw in pref_text for kw in ("text_only", "no charts", "no tables", "no formatting")):
        constraints.append("FORMAT: Plain text only. No bullet lists, no tables, no formatting symbols.")

    # Data vs coaching balance
    if any(kw in pref_text for kw in ("just numbers", "data first", "numbers first")):
        constraints.append("STRUCTURE: Lead with the numbers. Interpretation comes after the data, not before.")
    elif any(kw in pref_text for kw in ("just coach", "no numbers", "feel", "less data")):
        constraints.append("STRUCTURE: Minimize raw numbers. Focus on what to do and why.")

    if not constraints:
        return prompt_text

    constraint_block = "\nENFORCED PREFERENCES (non-negotiable — override defaults):\n" + \
                       "\n".join(f"  • {c}" for c in constraints)

    # Append before the final Rules line if present, else append at end
    if "\nRules:" in prompt_text:
        return prompt_text.replace("\nRules:", constraint_block + "\nRules:", 1)
    return prompt_text + constraint_block


def _extract_tone_directives(athlete_prefs: list) -> str:
    """
    Scan athlete preferences for tone/style signals and return a directive string.
    Maps free-form preference text to actionable coaching instructions.
    """
    if not athlete_prefs:
        return ""

    TONE_SIGNALS = {
        # brevity / conciseness
        ("shorter", "concise", "brief", "less text", "too long", "tldr", "no fluff"):
            "Keep this email SHORT — key point + data only. No elaboration.",
        # more detail
        ("more detail", "explain more", "why", "deeper", "breakdown", "analysis"):
            "Go DEEPER than usual — explain the reasoning behind recommendations.",
        # tougher / more challenging
        ("push me", "harder", "tougher", "challenge", "don't go easy", "be direct", "no excuses"):
            "Be DEMANDING. Call out anything below standard. No softening.",
        # gentler / supportive
        ("gentler", "supportive", "motivate", "encourage", "positive"):
            "Lead with encouragement. Acknowledge effort before critique.",
        # data-focused
        ("numbers", "data", "stats", "metrics", "just numbers"):
            "Lead with data — numbers first, interpretation second. Minimize prose.",
        # less data / more coaching
        ("less data", "no numbers", "just coach", "feel", "intuitive"):
            "Minimize raw numbers — focus on what to do and why, not spreadsheet metrics.",
    }

    pref_text = " ".join(
        p.get("Preference", "").lower()
        for p in athlete_prefs
        if p.get("Category", "").upper() not in ("OUTPUT_CHARTS", "OUTPUT_CHANNEL")
    )

    directives = []
    for keywords, directive in TONE_SIGNALS.items():
        if any(kw in pref_text for kw in keywords):
            directives.append(f"  • {directive}")

    return "\n".join(directives) if directives else ""


def build_prompt(program_data: dict, memory_data: dict,
                 last_run_date: Optional[date] = None,
                 replies: list[dict] = None,
                 is_weekly_summary: bool = False,
                 plateau_deep_dives: dict = None,
                 projections_text: str = "",
                 program_complete: bool = False,
                 tonnage_by_lift: dict = None,
                 cross_program: str = "",
                 goal_proximity: list = None,
                 long_term_projections: dict = None) -> tuple[str, str]:
    """
    Build the system prompt and user message for Claude.

    Args:
        program_data: Output of sheets.read_program_data()
        memory_data: Output of memory.read_all()
        last_run_date: Date of last coaching email (from memory.get_last_run_date())
        plateau_deep_dives: Dict of {lift_name: analysis_text} for plateaued lifts

    Returns:
        (system_prompt, user_message)
    """
    today = date.today()
    _start_date = resolve_program_start_date()
    week_num = program_data.get("current_week_num", compute_current_week(_start_date))
    progression = program_data.get("progression", {})
    current_week = program_data.get("current_week", {})
    prev_carryover = program_data.get("prev_week_carryover")
    recent_weeks = program_data.get("recent_weeks", [])
    daily_log = program_data.get("daily_log", [])

    # Determine block info from progression
    block_info = ""
    if week_num in progression:
        block = progression[week_num].get("block", "")
        week_type = progression[week_num].get("type", "")
        if block and week_type:
            block_info = f"Block {block} — {week_type}"
        elif block:
            block_info = f"Block {block}"

    # Coaching duration (approximate)
    try:
        start = datetime.strptime(_start_date, "%Y-%m-%d").date()
        months = (today - start).days // 30
        coaching_duration = f"{months} months" if months >= 1 else "a few weeks"
    except Exception:
        coaching_duration = "several weeks"

    # Program name + total weeks from registry (non-fatal fallback)
    prog_name = "Strength Program"
    total_weeks = None
    try:
        from memory import get_active_program_info
        prog_info = get_active_program_info()
        if prog_info:
            prog_name = prog_info.get("name") or prog_name
            tw = prog_info.get("total_weeks")
            total_weeks = int(tw) if tw else None
    except Exception:
        pass

    # Periodization context: where are we in the program arc?
    periodization_context = _build_periodization_context(
        week_num, total_weeks, progression
    )

    sections = []

    # --- Date & week context ---
    if total_weeks and block_info:
        week_label = f"Week {week_num}/{total_weeks}, {block_info}"
    elif total_weeks:
        week_label = f"Week {week_num}/{total_weeks}"
    elif block_info:
        week_label = f"Week {week_num}, {block_info}"
    else:
        week_label = f"Week {week_num}"
    email_type = "WEEKLY SUMMARY (include charts reference)" if is_weekly_summary else "daily email"
    sections.append(
        f"Today: {today.strftime('%A, %B %d, %Y')}\n"
        f"Program: {prog_name} — {week_label}\n"
        f"Coaching duration: ~{coaching_duration}\n"
        f"Email type: {email_type}"
    )

    # --- Program completion ceremony ---
    if program_complete:
        sections.append(
            f"PROGRAM COMPLETE — THIS IS THE FINAL EMAIL OF THE PROGRAM\n"
            f"  {prog_name} is done. Week {week_num}/{total_weeks or week_num} completed.\n"
            "  This email is a milestone moment — write a proper program wrap-up:\n"
            "  • Acknowledge the achievement directly (completing a full program is real work)\n"
            "  • Summarize the key progress: 1RM gains per main lift, overall consistency\n"
            "  • What did he do well this program? What did he struggle with? Be honest, not sycophantic.\n"
            "  • Name the 2-3 biggest lessons or patterns you observed over the full program\n"
            "  • Signal what comes next — ask about goals and preferences for the next block\n"
            "  Required markers:\n"
            f"    [LANDMARK: Program complete — {prog_name}, {week_num} weeks, {date.today()}]\n"
            "    [TELEGRAM: short celebration message + 'What kind of training do you want next?']\n"
            "    [FOLLOWUP: What do you want to focus on in the next program?]"
        )

    # --- Periodization context (phase-aware coaching implications) ---
    if periodization_context and not program_complete:
        sections.append(
            "PERIODIZATION CONTEXT (where we are in the program arc — let this inform your tone and priorities)\n"
            + periodization_context
        )

    # --- Annual arc (12-month roadmap — weekly email only, so the athlete sees the bigger picture) ---
    annual_arc = coach_state.get("ANNUAL_ARC", {}).get("summary", "") if coach_state else ""
    if annual_arc and is_weekly_summary:
        # First 3 sentences — enough for context without bloating the prompt
        arc_snippet = ". ".join(annual_arc.split(". ")[:3]).strip()
        if arc_snippet and not arc_snippet.endswith("."):
            arc_snippet += "."
        sections.append(
            "12-MONTH ARC (your long-term roadmap — reference the current phase and next milestone in this weekly email)\n"
            f"  {arc_snippet}\n"
            "  → Don't summarise the whole arc. Surface what's relevant NOW: "
            "what phase are we in, what's the next milestone, is anything ahead of/behind schedule?"
        )

    # --- Coach State (your compressed knowledge from last run — read this first) ---
    coach_state = memory_data.get("coach_state", {})
    state_text = _format_coach_state(coach_state)
    if state_text:
        sections.append(
            "YOUR CURRENT KNOWLEDGE (Coach State — what you wrote for yourself last run)\n"
            + state_text
        )

    # --- Behavior Patterns (weekly behavioral analysis — address these directly) ---
    behavior_patterns = coach_state.get("BEHAVIOR_PATTERNS", {}).get("summary", "")
    if behavior_patterns and "No significant" not in behavior_patterns:
        sections.append(
            "BEHAVIORAL PATTERNS (detected weekly — use these as coaching levers, not just data points)\n"
            f"  {behavior_patterns}\n"
            "  → Address any active skip patterns, RPE patterns, or day-of-week misses directly in this email. "
            "Don't just note them — name them, ask about them, or propose a fix."
        )

    # --- Athlete Model (quarterly psychological profile — use as your coaching lens today) ---
    athlete_model = coach_state.get("ATHLETE_MODEL", {}).get("summary", "")
    if athlete_model:
        sections.append(
            "YOUR PSYCHOLOGICAL MODEL OF THIS ATHLETE (updated quarterly — use as a lens, not a script)\n"
            f"  {athlete_model}\n"
            "  → Let this calibrate your tone today: how direct to be, what to push on, what to let go."
        )

    # --- Coaching Reason (why this week's training is structured this way) ---
    coaching_reason = coach_state.get("COACHING_REASON", {}).get("summary", "")
    if coaching_reason:
        sections.append(
            "WHY THIS WEEK IS STRUCTURED THIS WAY (use this to educate, not just prescribe)\n"
            f"  {coaching_reason}\n"
            "  → Weave this reasoning into the email naturally — one sentence is enough. "
            "The athlete should understand the principle, not just the prescription."
        )

    # --- Athlete Dreams (life-level goals — surface in weekly email to anchor the long view) ---
    athlete_dreams = coach_state.get("ATHLETE_DREAMS", {}).get("summary", "") if coach_state else ""
    if athlete_dreams and is_weekly_summary:
        sections.append(
            "ATHLETE'S LONG-TERM DREAMS (expressed goals beyond this program — "
            "surface the connection to today's work when relevant)\n"
            f"  {athlete_dreams}\n"
            "  → Don't force it every week. But when the week's work connects to the big goal "
            "(e.g. squat strength for Olympic lifting), make that link explicit in one sentence."
        )

    # --- Long-term projections (1yr/2yr — weekly only, for trajectory awareness) ---
    if is_weekly_summary and long_term_projections:
        from projections import format_long_term_projections
        lt_text = format_long_term_projections(long_term_projections)
        if lt_text:
            sections.append(
                "LONG-TERM PROJECTIONS (1yr / 2yr — computed from your current trend, "
                "with diminishing-returns adjustment. Use to answer 'where is this athlete going?')\n"
                + lt_text + "\n"
                "  → Reference the 1yr number in the weekly email if it's relevant to a goal "
                "or dream the athlete has expressed. Don't list all lifts — pick the one that matters most."
            )

    # --- Athlete Preferences (respect these before making any output decisions) ---
    athlete_prefs = memory_data.get("athlete_preferences", [])
    prefs_text = _format_athlete_preferences(athlete_prefs)
    if prefs_text:
        sections.append(
            "ATHLETE PREFERENCES (explicit feedback — respect these in output decisions)\n"
            + prefs_text
        )

    # --- Tone calibration from preferences ---
    tone_directives = _extract_tone_directives(athlete_prefs)
    if tone_directives:
        sections.append(
            "COACHING TONE FOR THIS EMAIL (derived from athlete feedback — apply immediately)\n"
            + tone_directives
        )

    # --- Active commands (agent awareness) ---
    commands = memory_data.get("commands", [])
    cmd_text = _format_active_commands(commands)
    if cmd_text:
        sections.append(f"ACTIVE COMMANDS (from Commands tab in Coach Memory)\n{cmd_text}")

    # --- User replies (email replies since last run) ---
    if replies:
        reply_text = _format_replies(replies)
        sections.append(f"MESSAGES FROM YOU (email replies since last coaching email)\n{reply_text}")

    # --- DELTA: what's new since last email (lead with this) ---
    delta_text = _format_delta(program_data, last_run_date)
    if delta_text:
        sections.append(f"SINCE LAST EMAIL\n{delta_text}")

    # --- Coach's active watch list (what it's tracking, following up on) ---
    coach_focus = memory_data.get("coach_focus", [])
    focus_text = _format_coach_focus(coach_focus)
    if focus_text:
        sections.append(
            "YOUR ACTIVE WATCH LIST (what you're currently tracking — check these against today's data)\n"
            + focus_text
        )

    # --- Commitments (explicit promises you made — must follow up on these) ---
    commitments = memory_data.get("commitments", [])
    commit_text = _format_commitments(commitments)
    if commit_text:
        sections.append(
            "YOUR OPEN COMMITMENTS (promises you made explicitly — check each one today, "
            "mark resolved with [RESOLVED: commitment text] or follow up if not yet done)\n"
            + commit_text
        )

    # --- Questions found in notes (surface early for Claude) ---
    questions = _extract_questions(program_data)
    if questions:
        q_text = "\n".join(f"  {q}" for q in questions)
        sections.append(f"QUESTIONS TO ADDRESS (weave answers into the email naturally)\n{q_text}")

    # --- Athlete profile ---
    profile = memory_data.get("athlete_profile", "")
    if profile:
        sections.append(f"ATHLETE PROFILE\n{profile}")

    # --- Long-term goals ---
    lt_goals = memory_data.get("long_term_goals", "")
    if lt_goals:
        sections.append(f"LONG-TERM GOALS\n{lt_goals}")

    # --- Life context ---
    life_ctx = memory_data.get("life_context", [])
    if life_ctx:
        ctx_lines = "\n".join(f"  [{c['date']}] {c['context']}" for c in life_ctx[-5:])
        sections.append(f"RECENT LIFE CONTEXT\n{ctx_lines}")

    # --- Strategic plan (your internal roadmap) ---
    strategic_plan = memory_data.get("strategic_plan", [])
    if strategic_plan:
        plan_text = _format_strategic_plan(strategic_plan)
        if plan_text:
            sections.append(
                f"YOUR COACHING ROADMAP (internal — updated weekly via planning pass)\n{plan_text}\n"
                "Use this to inform today's message. Surface relevant parts naturally — only when it matters."
            )

    # --- Recent Telegram conversation (since last email — this is your dialogue, don't repeat it) ---
    telegram_log = memory_data.get("telegram_log", [])
    if telegram_log:
        tg_text = _format_telegram_log(telegram_log)
        since_label = f" since {last_run_date}" if last_run_date else ""
        sections.append(
            f"RECENT TELEGRAM CONVERSATION{since_label} "
            f"(this is already part of the coaching relationship — build on it, don't repeat it)\n"
            + tg_text
        )

    # --- Channel preference: if athlete wants Telegram-first, signal to keep email short ---
    athlete_prefs = memory_data.get("athlete_preferences", [])
    for pref in athlete_prefs:
        val = pref.get("Preference", "").lower()
        if "primary_channel" in val and "telegram" in val:
            sections.append(
                "CHANNEL PREFERENCE: athlete prefers Telegram as primary coaching channel. "
                "Keep this email focused — key data and one main point only. "
                "Ask questions via [FOLLOWUP: question?] so they route to Telegram. "
                "Don't repeat things already covered in Telegram above."
            )
            break

    # --- Projections (computed facts — not hallucinated) ---
    if projections_text:
        sections.append(f"PROJECTIONS (computed — Python math, not estimated)\n{projections_text}")

    # --- Projection review (weekly only — how does this week compare to last week's snapshot?) ---
    if is_weekly_summary and projections_text:
        proj_review = _format_projection_review(coach_state, projections_text)
        if proj_review:
            sections.append(proj_review)

    # --- Weekly tonnage per lift (volume load tracking) ---
    if tonnage_by_lift:
        from projections import format_tonnage_for_prompt
        tonnage_text = format_tonnage_for_prompt(tonnage_by_lift)
        if tonnage_text:
            sections.append(
                "WEEKLY TONNAGE (kg lifted per lift — use to assess volume load and trends)\n"
                + tonnage_text
            )

    # --- Cross-program analytics ---
    if cross_program:
        sections.append(
            "CROSS-PROGRAM PROGRESS (1RM gains: this program vs history — "
            "use for long-term perspective)\n" + cross_program
        )

    # --- Goal proximity alerts ---
    if goal_proximity:
        lines = []
        for alert in goal_proximity:
            if alert["urgent"]:
                lines.append(
                    f"  🏆 {alert['lift']}: GOAL REACHED — {alert['current_1rm']}kg est. 1RM "
                    f"(target: {alert['target']}kg). Acknowledge this explicitly. It matters."
                )
            else:
                lines.append(
                    f"  🎯 {alert['lift']}: {alert['current_1rm']}kg / {alert['target']}kg target — "
                    f"{alert['gap']}kg away. Closing in. Mention the trajectory."
                )
        sections.append(
            "GOAL PROXIMITY ALERTS (bring these up — athlete is close to or has hit a major target)\n"
            + "\n".join(lines)
        )

    # --- 1RM trajectory ---
    lift_history = memory_data.get("lift_history", [])
    tracked_lifts = memory_data.get("tracked_lifts")
    one_rm_text = _format_1rm_trajectory(lift_history, tracked_lifts=tracked_lifts)
    sections.append(f"ESTIMATED 1RM TRAJECTORY\n{one_rm_text}")

    # --- Program trajectory (start → goal vs current) ---
    trajectory = _compute_trajectory(
        program_data.get("goals", {}), progression, week_num, lift_history,
        tracked_lifts=tracked_lifts
    )
    sections.append(f"PROGRAM TARGETS\n{trajectory}")

    # --- Current week (full detail) ---
    catchup_map = _extract_catchup_day_map(memory_data.get("commands", []))
    # Cross-reference Done=Yes with actual dates from Lift History so coach knows WHEN sessions happened
    session_dates = {}
    try:
        from memory import get_session_dates_from_lift_history
        session_dates = get_session_dates_from_lift_history(week_num)
    except Exception:
        pass
    current_week_text = _format_current_week(current_week, catchup_map=catchup_map,
                                              session_dates=session_dates) if current_week else "No current week data."
    sections.append(f"THIS WEEK\n{current_week_text}")

    # --- When sessions actually happened (temporal grounding — cross-ref with Done=Yes above) ---
    if session_dates:
        dated_lines = [f"  {day}: {d}" for day, d in sorted(session_dates.items())]
        sections.append(
            "WHEN SESSIONS WERE LOGGED IN LIFT HISTORY (cross-reference with Done=Yes above — "
            "if a Done=Yes session has no entry here, it may not have been logged, or was logged in a different week)\n"
            + "\n".join(dated_lines)
        )

    # --- Previous week carryover (if any recent sessions from last week) ---
    if prev_carryover:
        sections.append(f"PREVIOUS WEEK (carry-over / recently completed)\n{_summarize_week(prev_carryover)}")

    # --- Recent weeks for trend context ---
    if recent_weeks:
        recent_parts = [_summarize_week(w) for w in recent_weeks]
        sections.append("RECENT WEEKS\n" + "\n\n".join(recent_parts))

    # --- Rolling trends ---
    all_weeks_for_trends = recent_weeks + ([prev_carryover] if prev_carryover else [])
    health_log = memory_data.get("health_log", [])
    trends_text = _compute_rolling_trends(health_log, all_weeks_for_trends)
    sections.append(f"SHORT-TERM TRENDS (2wk vs 4wk)\n{trends_text}")

    # --- Health & lifestyle ---
    health_text = _format_health_trends(health_log, daily_log)
    sections.append(f"HEALTH & LIFESTYLE\n{health_text}")

    # --- Coach log (what was said recently) ---
    coach_log = memory_data.get("coach_log", [])
    if coach_log:
        cl_lines = [
            f"  [{e.get('Date', '')}] {e.get('Key Observations', '')}"
            for e in coach_log[-5:]
        ]
        sections.append("WHAT YOU SAID RECENTLY\n" + "\n".join(cl_lines))

    # --- Plateau deep dives (per-lift analysis when plateau detected) ---
    if plateau_deep_dives:
        dive_lines = []
        for lift, analysis in plateau_deep_dives.items():
            dive_lines.append(f"  {lift}:\n  {analysis.strip()}")
        sections.append(
            "PLATEAU DEEP DIVES (full history analysis for stalled lifts)\n" +
            "\n\n".join(dive_lines)
        )

    user_message = "\n\n---\n\n".join(sections)
    if is_weekly_summary:
        user_message += (
            "\n\n---\n\nWrite the weekly summary coaching email. "
            "This is a Sunday recap: cover the full week's performance, key trends, "
            "and what to focus on next week. Charts for 1RM trajectory and training volume "
            "are attached inline — reference them naturally in the text (e.g. 'as you can see in the chart below')."
        )
    else:
        user_message += "\n\n---\n\nWrite the coaching email."

    return SYSTEM_PROMPT, user_message


# ---------------------------------------------------------------------------
# Proactive check-in prompt
# ---------------------------------------------------------------------------

def build_proactive_prompt(memory_data: dict, program_data: dict = None,
                           session_delta: dict = None,
                           topic_coverage: dict = None) -> tuple[str, str]:
    """
    Build prompt for the proactive check-in pass.
    Claude reads memory + today's program schedule and decides whether to reach out.
    Returns (system_prompt, user_message).
    """
    today = date.today()
    weekday_name = today.strftime("%A")  # Monday, Tuesday, etc.
    hour_utc = datetime.utcnow().hour
    time_of_day = "morning" if hour_utc < 12 else "afternoon"

    system = (
        f"You are {ATHLETE_NAME}'s strength coach. This is an autonomous proactive check-in — "
        "you decide whether to reach out via Telegram right now, and what to say.\n\n"
        "You are the same coach who writes the daily email. Telegram is your primary real-time channel.\n\n"
        "TRIGGER CONDITIONS (check these in order):\n"
        "1. TRAINING DAY PREP (morning): If today is a scheduled training day and no session logged yet "
        "→ send a readiness-aware prep message. Check the RECENT HEALTH LOG:\n"
        "   • Sleep < 6h or energy ≤ 5/10 → flag it: 'Sleep was rough last night — consider dropping "
        "intensity 10-15% on the heavy sets today. Still worth going.'\n"
        "   • Sleep ≥ 7h and energy ≥ 7/10 → green light: brief, energising message with today's key lift.\n"
        "   • No health data → just confirm today's session and key lift.\n"
        "2. SESSION FOLLOW-UP (afternoon): If today was a training day and session still not logged "
        "→ ask how it went, what they actually did. If health data showed fatigue → ask specifically if "
        "they adjusted the load.\n"
        "3. OPEN QUESTION (any time): If there's an unanswered OPEN_QUESTION more than 6 hours old "
        "→ gently follow up. Don't be annoying — max once per question.\n"
        "4. PLAN FLEXIBILITY (any time): If there's a PENDING_CATCHUP command (athlete planned to reschedule) "
        "→ check in on whether it happened or needs adjusting.\n"
        "5. GENERAL CHECK-IN: If you haven't heard from the athlete in 2+ days and no known break "
        "→ brief check-in. Don't do this if you already sent a message today.\n\n"
        "6. DELOAD SIGNAL: If Coach State shows TSB is negative and deload_recommended is flagged "
        "→ message the athlete directly: 'Fatigue is building. I want us to dial this week back.' "
        "Reference the actual TSB number from the projections if available.\n"
        "7. GOAL PROXIMITY: If Coach State shows a lift within 5kg of its target "
        "→ mention it with energy. 'You're Xkg away from your squat goal — this week matters.'\n"
        "8. MISSING DATA: If the health log shows no bodyweight or sleep logged in 3+ days "
        "→ ask specifically: 'Haven't seen any bodyweight data in a few days — still tracking, or did something change?' "
        "Don't be annoying — ask once, then drop it if they don't respond.\n\n"
        "Output rules:\n"
        "- [TELEGRAM: your message] — 1-3 sentences, direct, natural coach voice, not pushy\n"
        "- [PASS: reason] — if no outreach needed right now\n"
        "- You may also include [FOLLOWUP: ...] or [TRACKING: ...] to update your watch list\n\n"
        "Bias toward sending, not passing. A good coach reaches out. A bad one waits. "
        "Don't reach out if you already sent a Telegram message in the last 4 hours. "
        "Don't repeat what was said in the last email unless it's genuinely relevant right now."
    )

    # Coach State
    coach_state = memory_data.get("coach_state", {})
    state_text = _format_coach_state(coach_state) or "  (no state data)"

    # Coach Focus — OPEN items only
    focus_text = _format_coach_focus(memory_data.get("coach_focus", [])) or "  (none)"

    # Telegram — two-tier:
    #   Tier 0 (old, >7 days): use TELEGRAM_HISTORY compressed summary from Coach State
    #   Tier 1 (recent, last 7 days): raw log entries for full fidelity
    tg_history_summary = coach_state.get("TELEGRAM_HISTORY", {}).get("summary", "")
    cutoff_7d = str(today - timedelta(days=7))
    tg_recent_rows = [r for r in memory_data.get("telegram_log", [])
                      if r.get("Date", "") >= cutoff_7d]
    tg_recent_rows = sorted(tg_recent_rows, key=lambda r: (r.get("Date", ""), r.get("Time", "")))
    tg_recent_text = "\n".join(
        f"  [{r.get('Date','')} {r.get('Direction','')}] {r.get('Message','')[:120]}"
        for r in tg_recent_rows[-15:]
    ) or "  (no messages in last 7 days)"
    tg_text = ""
    if tg_history_summary:
        tg_text += f"OLDER HISTORY (compressed):\n  {tg_history_summary}\n\n"
    tg_text += f"LAST 7 DAYS (raw):\n{tg_recent_text}"

    # Active Commands
    active_cmds = [
        r for r in memory_data.get("commands", [])
        if r.get("Applied", "").upper() not in ("Y", "DECLINED")
        and not r.get("Command", "").startswith("#")
    ]
    cmd_text = "\n".join(
        f"  {r.get('Command','')}: {r.get('Value','')}"
        for r in active_cmds
    ) or "  (none)"

    # Recent health log (last 7 days — lightweight for proactive pass)
    health_log = memory_data.get("health_log", [])
    recent_health = health_log[-7:] if health_log else []
    health_lines = []
    for e in recent_health:
        d = e.get("Date", "?")
        bw = e.get("Bodyweight (kg)", "")
        sleep = e.get("Sleep (hrs)", "")
        food = e.get("Food Quality (1-10)", "")
        notes = e.get("Notes", "")
        parts = [f"[{d}]"]
        if bw:
            parts.append(f"BW:{bw}kg")
        if sleep:
            parts.append(f"sleep:{sleep}h")
        if food:
            parts.append(f"food:{food}/10")
        if notes:
            parts.append(f"note:{notes[:50]}")
        health_lines.append(" ".join(parts))
    health_text = "\n".join(f"  {l}" for l in health_lines) or "  (no recent health data)"

    # --- Today's program schedule (if program data provided) ---
    today_schedule = ""
    if program_data:
        current_week = program_data.get("current_week", {})
        days = current_week.get("days", [])
        week_num = program_data.get("current_week_num", "?")
        scheduled_lines = []
        for day in days:
            label = day.get("label", "")
            exercises = day.get("exercises", [])
            done_count = sum(1 for e in exercises if e.get("done") is True)
            total_count = sum(1 for e in exercises if e.get("exercise") or e.get("name"))
            scheduled_lines.append(
                f"  {label}: {done_count}/{total_count} exercises logged"
                + (" ✓" if total_count > 0 and done_count == total_count else "")
            )
        if scheduled_lines:
            today_schedule = f"Week {week_num} schedule:\n" + "\n".join(scheduled_lines)

    # --- Dynamic travel context ---
    travel_ctx = _extract_travel_context(memory_data)
    travel_section = f"\n## CURRENT TRAVEL / SCHEDULE CONTEXT\n{travel_ctx}" if travel_ctx else ""

    # --- Open commitments ---
    commitments = memory_data.get("commitments", [])
    commit_text = _format_commitments(commitments)
    commit_section = f"\n## YOUR OPEN COMMITMENTS (promises to follow up on)\n{commit_text}" if commit_text else ""

    # --- Weekly intent ---
    coach_state_raw = memory_data.get("coach_state", {})
    weekly_intent = coach_state_raw.get("WEEKLY_INTENT", {}).get("summary", "")
    intent_section = f"\n## THIS WEEK'S COACHING INTENT\n  {weekly_intent}" if weekly_intent else ""

    # --- Session delta: what changed in the program sheet since last check ---
    delta_section = ""
    if session_delta and (session_delta.get("new_sessions_done") or session_delta.get("new_health_data")):
        delta_lines = []
        for s in session_delta.get("new_sessions_done", []):
            line = (f"  NEW SESSION: {s['label']} — "
                    f"{s['exercises_done']}/{s['exercises_total']} exercises ({s.get('completion_pct', '?')}%)")
            if s.get("skipped_names"):
                line += f"\n    SKIPPED: {', '.join(s['skipped_names'])}"
            if s.get("weight_deviations"):
                line += f"\n    WEIGHT DEVIATIONS: {'; '.join(s['weight_deviations'])}"
            if s.get("notable_notes"):
                line += f"\n    NOTES: {' | '.join(s['notable_notes'])}"
            if not s.get("has_rpe"):
                line += "\n    RPE: not logged"
            delta_lines.append(line)
        # New health data in footer
        new_health = session_delta.get("new_health_data", {})
        if new_health:
            health_parts = [f"{k}: {v}" for k, v in new_health.items() if v]
            delta_lines.append(f"  NEW HEALTH DATA: {', '.join(health_parts)}")
        delta_section = (
            "\n## SESSION DELTA — WHAT CHANGED SINCE LAST CHECK (HIGH PRIORITY)\n"
            + "\n".join(delta_lines)
            + "\n\n  → You are a coach who notices. Use this data to reach out with specific, "
            "intelligent questions: ask about weight deviations, skipped exercises (WHY?), "
            "session notes, RPE if missing. Don't send a generic 'how did it go?' — "
            "reference what you actually see."
        )

    # --- Missing data check (trigger 8) ---
    missing_data_section = ""
    try:
        health_log_all = memory_data.get("health_log", [])
        health_dates = sorted(
            [e.get("Date", "") for e in health_log_all if e.get("Date")],
            reverse=True,
        )
        if health_dates:
            last_health_iso = health_dates[0][:10]
            days_gap = (today - date.fromisoformat(last_health_iso)).days
            if days_gap >= 3:
                missing_data_section = (
                    f"\n## MISSING DATA (trigger 8)\n"
                    f"  No health/bodyweight log in {days_gap} days (last entry: {last_health_iso}).\n"
                    "  → Ask about it specifically. Not accusatorially — just noticing."
                )
        elif not health_dates:
            missing_data_section = (
                "\n## MISSING DATA (trigger 8)\n"
                "  No health log entries at all.\n"
                "  → Consider asking the athlete to start logging bodyweight and sleep."
            )
    except Exception:
        pass

    # --- Topics already covered today (smart dedup) ---
    covered_section = ""
    if topic_coverage:
        covered = [topic for topic, covered in topic_coverage.items() if covered]
        if covered:
            covered_section = (
                "\n## TOPICS ALREADY ADDRESSED TODAY (do NOT ask about these again)\n"
                + "\n".join(f"  - {t}" for t in covered)
                + "\n  → If one of your planned questions covers a topic above, pivot: "
                "ask a different angle, acknowledge what was said, or skip that point entirely."
            )

    user_message = f"""TODAY: {today.strftime('%A, %B %d, %Y')} ({time_of_day})

## YOUR COMPRESSED KNOWLEDGE (Coach State)
{state_text}

## OPEN WATCH ITEMS (Coach Focus)
{focus_text}

## THIS WEEK'S PROGRAM SCHEDULE
{today_schedule or "  (program data not loaded)"}

## RECENT HEALTH LOG (last 7 days)
{health_text}

## TELEGRAM CONVERSATION HISTORY
{tg_text}

## ACTIVE COMMANDS (includes PENDING_CATCHUP, OPEN_QUESTION, PENDING_PROPOSAL)
{cmd_text}{travel_section}{commit_section}{intent_section}{delta_section}{missing_data_section}{covered_section}

---
Today is {weekday_name}. Time of day: {time_of_day}. Should you reach out right now?
Check the trigger conditions. Output [TELEGRAM: message] or [PASS: reason]."""

    return system, user_message


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    # Minimal test with mock data
    mock_program = {
        "current_week_num": 7,
        "goals": {
            "Squat": {"start": "85kg x 5", "goal": "120kg x 5", "gain": "+35kg"},
            "Bench Press": {"start": "75kg x 5", "goal": "105kg x 5", "gain": "+30kg"},
        },
        "progression": {
            7: {"Squat": "92.5kg", "Bench": "80kg", "block": 2, "type": "PROGRESS"}
        },
        "current_week": {
            "title": "WEEK 7 — Block 2",
            "week_num": 7,
            "days": [
                {
                    "label": "DAY 1: Squat + Bench Heavy",
                    "date": date.today(),
                    "exercises": [
                        {"name": "Squat", "weight": "92.5kg", "sets_reps": "4x4",
                         "done": True, "actual": None, "notes": "felt heavier than expected, should I add weight next week?"},
                        {"name": "Bench Press", "weight": "80kg", "sets_reps": "4x4",
                         "done": True, "actual": None, "notes": None},
                    ]
                }
            ],
            "weekly_notes": {"bodyweight": 82.5, "sleep": 6.5, "energy": 7, "notes": None}
        },
        "recent_weeks": [],
        "daily_log": [],
    }
    mock_memory = {
        "athlete_profile": "Name: Nacho | Health: Insulin resistance, golfer's elbow | Background: Finance, 14h/day, travels biweekly",
        "long_term_goals": "120kg squat | Eventually Olympic lifting",
        "life_context": [{"date": "2026-01-13", "context": "Started 30-week program"}],
        "lift_history": [],
        "health_log": [],
        "coach_log": [],
    }

    system_prompt, user_message = build_prompt(mock_program, mock_memory)
    print("=== SYSTEM PROMPT ===")
    print(system_prompt)
    print("\n=== USER MESSAGE ===")
    print(user_message)
