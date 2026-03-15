"""
Main entry point for the strength coach agent.

Usage:
  python src/run_coach.py              # Full run: analyze + send email
  python src/run_coach.py --dry-run    # Analyze + print email, do not send
  python src/run_coach.py --week 8     # Override current week number
  python src/run_coach.py --setup      # Set up Coach Memory Sheet (first-time only)
  python src/run_coach.py --no-sync    # Skip writing new data to Coach Memory (read-only)
  python src/run_coach.py --weekly     # Force a weekly summary email with charts
  python src/run_coach.py --think      # Run strategic planning pass only (no email)
  python src/run_coach.py --proactive  # Lightweight check-in: read memory, optionally send Telegram
"""

import argparse
import sys
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8")

import anthropic

from config import ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL, KEY_LIFTS, compute_current_week, resolve_program_start_date


# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Strength Coach Agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print email to terminal, do not send")
    parser.add_argument("--week", type=int, default=None,
                        help="Override current week number")
    parser.add_argument("--setup", action="store_true",
                        help="Set up Coach Memory Sheet structure and exit")
    parser.add_argument("--no-sync", action="store_true",
                        help="Skip writing new data to Coach Memory (read-only run)")
    parser.add_argument("--weekly", action="store_true",
                        help="Force a weekly summary email with charts (normally auto on Sundays)")
    parser.add_argument("--think", action="store_true",
                        help="Run strategic planning pass only — updates Coach Memory, no email sent")
    parser.add_argument("--proactive", action="store_true",
                        help="Lightweight check-in: read memory, reason, optionally send Telegram. No email.")
    parser.add_argument("--nudge", action="store_true",
                        help="Evening nudge: if today's session is unlogged, send a Telegram reminder.")
    parser.add_argument("--export", action="store_true",
                        help="Export all Coach Memory tabs to JSON (stdout or file).")
    parser.add_argument("--brief", action="store_true",
                        help="Pre-session brief: send targeted session prep via Telegram.")
    parser.add_argument("--post-session", action="store_true",
                        help="Post-session check-in: acknowledge logged session or ask how it went.")
    parser.add_argument("--evening-protocol", action="store_true",
                        help="Evening protocol: ask if tomorrow's plan is still on via Telegram.")
    parser.add_argument("--weekly-schedule", action="store_true",
                        help="Sunday schedule discovery: ask about this week's training plan via Telegram.")
    parser.add_argument("--steer-co-finalize", action="store_true",
                        help="Synthesize steer co conversation + send comprehensive email.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

def generate_analysis(system_prompt: str, user_message: str) -> str:
    """
    First pass: ask Claude to classify events, check open follow-ups, and set today's agenda.
    This is the 'thinking' step — it doesn't go in the email, it informs it.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    analysis_request = (
        "Before writing the coaching email, do this structured pre-analysis:\n\n"
        "EVENT TRIAGE:\n"
        "- LANDMARK (significant for weeks — PRs, milestones, injuries, major decisions): [list or 'none']\n"
        "- SIGNAL (pattern worth addressing — recurring behavior, multi-week trend, athlete question): [list or 'none']\n"
        "- NOISE (one-off disruption — travel miss, minor scheduling — acknowledge briefly, don't dwell): [list or 'none']\n\n"
        "PERIODIZATION CHECK:\n"
        "- What phase are we in? What does that mean for load, volume, and intensity today?\n"
        "- Is the athlete's recent performance consistent with where they should be at this stage?\n"
        "- Any phase-transition signals? (e.g. approaching deload, peak, program end)\n\n"
        "OPEN FOLLOW-UPS CHECK: [From your watch list — what needs checking today? What might be resolved? What is stale?]\n\n"
        "WHAT MATTERS TODAY: [1-2 things max — be ruthlessly selective. The athlete's attention is limited.]\n\n"
        "COACH'S OWN AGENDA: [What will you push today independent of athlete input? Think: ignored trends, "
        "long-term phase, health factors he's not tracking (sleep, carbs, VO2 max), follow-ups due.]\n\n"
        "FOCUS UPDATES NEEDED: [Any new items to start tracking? Anything resolved? "
        "Format: TRACKING/LANDMARK/FOLLOWUP/RESOLVED: description]\n\n"
        "Be direct and honest. This is your internal thinking only."
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=700,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_message + f"\n\n---\n\n{analysis_request}"}
        ]
    )
    return message.content[0].text, message.usage


def generate_email(system_prompt: str, user_message: str, analysis: str = "") -> tuple[str, object]:
    """Send the prompt to Claude and return (email_text, usage)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    full_user_message = user_message
    if analysis:
        full_user_message += (
            f"\n\n---\n\nYOUR PRE-ANALYSIS\n{analysis}"
            "\n\n---\n\nNow write the coaching email based on your analysis above."
        )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[
            {"role": "user", "content": full_user_message}
        ]
    )

    return message.content[0].text, message.usage


def _compute_cost(usage_list: list) -> float:
    """Compute USD cost from a list of usage objects. Sonnet: $3/M in, $15/M out."""
    total = 0.0
    for u in usage_list:
        if u:
            total += (getattr(u, "input_tokens", 0) / 1_000_000 * 3.0)
            total += (getattr(u, "output_tokens", 0) / 1_000_000 * 15.0)
    return total


# ---------------------------------------------------------------------------
# Email reply preprocessing — extract structured facts via Haiku (same as Telegram processor)
# ---------------------------------------------------------------------------

def preprocess_email_replies(replies: list[dict], dry_run: bool = False) -> int:
    """
    Run a Haiku pass on each email reply to extract structured facts into memory.
    Reuses the same categories as the Telegram processor.
    Returns number of facts dispatched.
    """
    if not replies:
        return 0

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    dispatched = 0

    for reply in replies:
        body = reply.get("body", "").strip()
        if not body:
            continue

        prompt = (
            "Extract structured facts from this athlete's email reply to their coach.\n"
            "Use the same format as Telegram processing:\n"
            "CATEGORY | DATE | FACT\n\n"
            "Categories: SCHEDULE_CHANGE | PENDING_CATCHUP | LIFE_EVENT | PREFERENCE | "
            "LIFT_UPDATE | MOOD_PERFORMANCE | HEALTH_DATA | PROGRAM_REQUEST | QUESTION | NOISE\n\n"
            "Rules:\n"
            "- DATE: use today's date if not specified\n"
            "- One line per fact\n"
            "- NOISE: skip unless useful\n"
            "- Only output lines in the format above, nothing else\n\n"
            f"Email reply:\n{body[:600]}"
        )

        try:
            result = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            output = result.content[0].text.strip()
            if dry_run:
                print(f"  [DRY RUN] Email reply facts:\n{output}")
                continue

            # Dispatch facts using the processor's dispatcher
            try:
                from processor import _dispatch_events, _parse_processor_output
                events = _parse_processor_output(output)
                if events:
                    dispatched += _dispatch_events(events, dry_run=False)
                    print(f"  [EmailReply] {len(events)} fact(s) extracted and dispatched")
            except Exception as e:
                print(f"  [EmailReply] Dispatch failed (non-fatal): {e}")
        except Exception as e:
            print(f"  [EmailReply] Haiku pass failed (non-fatal): {e}")

    return dispatched


# ---------------------------------------------------------------------------
# Extract [TELEGRAM: ...] proactive alert from email text
# ---------------------------------------------------------------------------

def extract_telegram_alert(email_text: str) -> tuple[str, str]:
    """
    Look for [TELEGRAM: message] at the end of the email.
    Returns (clean_email_text, telegram_message).
    The marker is stripped from the email before sending.
    """
    import re
    pattern = r'\[TELEGRAM:\s*(.*?)\]'
    match = re.search(pattern, email_text, re.IGNORECASE | re.DOTALL)
    if match:
        tg_msg = match.group(1).strip()
        clean = re.sub(pattern, '', email_text, flags=re.IGNORECASE | re.DOTALL).strip()
        return clean, tg_msg
    return email_text, ""


# ---------------------------------------------------------------------------
# Coach focus markers: parse + write back to Coach Memory
# ---------------------------------------------------------------------------

def parse_coach_focus_markers(email_text: str) -> tuple[str, list[dict]]:
    """
    Extract [TRACKING: ...], [LANDMARK: ...], [FOLLOWUP: ...], [RESOLVED: ...] markers.
    Returns (clean_email_text, list of {category, item} dicts).
    Markers are stripped from the email before the athlete sees it.
    """
    import re
    markers = []
    categories = ["TRACKING", "LANDMARK", "FOLLOWUP", "CONCERN", "RESOLVED"]
    clean = email_text
    for cat in categories:
        # Use [^\]]* (no inner brackets) to avoid greedy capture when item contains
        # a ] character (e.g. set/rep notation like [3x5] or bracketed lift names).
        pattern = rf'\[{cat}:\s*([^\]]*)\]'
        for match in re.finditer(pattern, email_text, re.IGNORECASE):
            markers.append({"category": cat, "item": match.group(1).strip()})
        clean = re.sub(pattern, '', clean, flags=re.IGNORECASE)
    return clean.strip(), markers


def write_coach_focus_updates(updates: list[dict]) -> None:
    """Write coach focus marker updates to Coach Memory (non-fatal)."""
    if not updates:
        return
    try:
        from memory import append_coach_focus, update_coach_focus_status
        today = str(date.today())
        for u in updates:
            category = u["category"]
            item = u["item"]
            if category == "RESOLVED":
                found = update_coach_focus_status(item, "RESOLVED", last_mentioned=today)
                if not found:
                    print(f"    [Focus] RESOLVED marker didn't match any open item: '{item[:60]}'")
                # Also attempt to resolve any matching Commitment
                try:
                    from memory import resolve_commitment
                    resolve_commitment(item[:80])
                except Exception:
                    pass
            else:
                append_coach_focus(category, item, last_mentioned=today)
                print(f"    [Focus] {category}: {item[:80]}")
    except Exception as e:
        print(f"  Coach focus update failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Weekly recap day — reads from Athlete Preferences, falls back to Sunday
# ---------------------------------------------------------------------------

_DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
    "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}

def _get_recap_weekday(athlete_prefs: list[dict]) -> int:
    """
    Read the preferred weekly recap day from Athlete Preferences.
    Looks for a SCHEDULE preference containing 'weekly_recap_day'.
    Returns weekday int (0=Monday … 6=Sunday). Default: 6 (Sunday).
    """
    for pref in athlete_prefs:
        if pref.get("Category", "").upper() != "SCHEDULE":
            continue
        text = pref.get("Preference", "").lower()
        if "weekly_recap_day" not in text:
            continue
        # Expected format: "weekly_recap_day: Sunday" or "weekly recap day: friday"
        parts = text.split(":")
        if len(parts) >= 2:
            day_str = parts[-1].strip()
            if day_str in _DAY_NAMES:
                return _DAY_NAMES[day_str]
    return 6  # default: Sunday


# ---------------------------------------------------------------------------
# Plateau detection: find stalled lifts and run deep dives
# ---------------------------------------------------------------------------

def detect_plateaus_and_deep_dive(lift_history: list[dict], system_prompt: str,
                                   tracked_lifts: list[dict] = None) -> dict:
    """
    Check 1RM trajectory for each key lift. If plateaued, fetch full history
    and run a focused analysis. Returns {lift_name: analysis_text}.
    Only checks MAIN lifts (plateau detection is for primary lifts only).
    """
    from planner import run_lift_deep_dive
    from memory import read_lift_history_for_exercise

    plateau_dives = {}

    # Use dynamic tracked lifts (MAIN only), fall back to KEY_LIFTS
    if tracked_lifts:
        lifts_to_check = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                          if tl.get("lift_type", "MAIN") == "MAIN"]
    else:
        lifts_to_check = KEY_LIFTS

    for _domain, lift in lifts_to_check:
        readings = []
        for row in lift_history:
            if lift.lower() not in row.get("Exercise", "").lower():
                continue
            est = row.get("Est 1RM", "")
            if not est:
                continue
            try:
                readings.append(float(est))
            except (ValueError, TypeError):
                pass

        if len(readings) < 3:
            continue

        recent = readings[-3:]
        spread = max(recent) - min(recent)
        if max(recent) > 0 and spread / max(recent) < 0.01:
            print(f"    → Plateau detected for {lift}, running deep dive...")
            full_history = read_lift_history_for_exercise(lift)
            analysis = run_lift_deep_dive(lift, full_history, system_prompt)
            if analysis:
                plateau_dives[lift] = analysis

    return plateau_dives


# ---------------------------------------------------------------------------
# Coach State writer — pure Python, no LLM calls
# ---------------------------------------------------------------------------

def write_coach_state_summaries(
    memory_data: dict,
    projections: dict,
    program_data: dict,
    week_num: int,
    dry_run: bool = False,
    is_weekly_summary: bool = False,
) -> None:
    """
    Write compressed domain summaries to the Coach State tab.
    Called at the end of each run so next run starts from a bounded context.
    Mostly pure Python; NUTRITION domain uses a lightweight Haiku call (weekly only).
    """
    try:
        from memory import upsert_coach_state

        # --- PROGRAM domain ---
        prog_proj = projections.get("program_projection")
        if prog_proj:
            prog_summary = (
                f"Week {prog_proj['week_num']}/{prog_proj['total_weeks']} "
                f"({prog_proj['pct_complete']}% complete, {prog_proj['weeks_remaining']} weeks left, "
                f"ends {prog_proj['estimated_end_date']})"
            )
            _write_state(upsert_coach_state, "PROGRAM", prog_summary, "HIGH", dry_run)

        # --- Lift domains (MAIN lifts from tracked_lifts registry, fallback KEY_LIFTS) ---
        tracked_lifts = memory_data.get("tracked_lifts")
        main_lifts = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                      if tl.get("lift_type", "MAIN") == "MAIN"] if tracked_lifts else KEY_LIFTS
        lift_proj_map = {p["exercise"].upper(): p for p in projections.get("lift_projections", []) if p}
        for domain, lift_name in main_lifts:
            proj = lift_proj_map.get(domain) or lift_proj_map.get(lift_name.upper())
            if not proj:
                continue
            curr = proj["current_1rm"]
            rate = proj["rate_per_week"]
            end_proj = proj.get("projected_end_1rm")
            on_track = proj.get("on_track")
            target = proj.get("target_1rm")

            parts = [f"est 1RM {curr}kg", f"trend {rate:+.2f}kg/wk"]
            if end_proj is not None:
                parts.append(f"projected end: {end_proj}kg")
            if target:
                parts.append(f"target: {target}kg")
            if on_track is True:
                parts.append("ON TRACK")
            elif on_track is False:
                wtt = proj.get("weeks_to_target")
                wr = prog_proj["weeks_remaining"] if prog_proj else None
                if wtt and wr:
                    parts.append(f"BEHIND ({wtt:.0f}wk needed, {wr}wk left)")
                else:
                    parts.append("BEHIND TARGET")

            confidence = "HIGH" if proj.get("data_points", 0) >= 6 else "MEDIUM"
            _write_state(upsert_coach_state, domain, " | ".join(parts), confidence, dry_run)

        # --- HEALTH domain ---
        bw_proj = projections.get("bw_projection")
        health_log = memory_data.get("health_log", [])
        health_parts = []
        if bw_proj:
            health_parts.append(
                f"BW {bw_proj['current_bw']}kg | trend {bw_proj['rate_per_week']:+.2f}kg/wk "
                f"({bw_proj['trend_direction']}) | 2wk avg {bw_proj['2wk_avg']}kg"
            )
        if health_log:
            recent = health_log[-14:]
            sleep_vals = []
            for e in recent:
                try:
                    sleep_vals.append(float(e.get("Sleep (hrs)", "") or ""))
                except (ValueError, TypeError):
                    pass
            if sleep_vals:
                health_parts.append(f"sleep avg {sum(sleep_vals)/len(sleep_vals):.1f}h (14d)")
        if health_parts:
            _write_state(upsert_coach_state, "HEALTH", " | ".join(health_parts),
                         "HIGH" if bw_proj else "MEDIUM", dry_run)

        # --- SCHEDULE domain ---
        current_week_data = program_data.get("current_week", {})
        sessions = current_week_data.get("sessions", [])
        if sessions:
            done = sum(1 for s in sessions if s.get("completed"))
            total = len(sessions)
            sched_summary = f"Week {week_num}: {done}/{total} days completed"
            _write_state(upsert_coach_state, "SCHEDULE", sched_summary, "HIGH", dry_run)

        # --- NUTRITION domain (Haiku, weekly only — derived insight, not critical daily) ---
        # Skip on daily runs to save budget (~$0.002/run × 30d = $0.06/mo)
        if is_weekly_summary:
            try:
                from health_agent import generate_nutrition_summary
                health_log_for_nutrition = memory_data.get("health_log", [])
                if health_log_for_nutrition:
                    fatigue = projections.get("fatigue") if projections else None
                    athlete_profile = memory_data.get("athlete_profile", "")
                    nutrition_summary, nutrition_cost = generate_nutrition_summary(
                        health_log_for_nutrition, fatigue, athlete_profile
                    )
                    if nutrition_summary:
                        _write_state(upsert_coach_state, "NUTRITION", nutrition_summary, "MEDIUM", dry_run)
                    if nutrition_cost and not dry_run:
                        from memory import log_coach_run
                        log_coach_run(
                            observations=f"NUTRITION Haiku pass (weekly)",
                            email_summary=f"[NUTRITION] {(nutrition_summary or '')[:200]}",
                            cost_usd=nutrition_cost,
                        )
                        print(f"    [NUTRITION] Haiku cost: ${nutrition_cost:.4f}")
            except Exception as e:
                print(f"  NUTRITION Coach State write failed (non-fatal): {e}")

    except Exception as e:
        print(f"  Coach State write failed (non-fatal): {e}")


def _write_state(fn, domain: str, summary: str, confidence: str, dry_run: bool) -> None:
    if dry_run:
        print(f"    [DRY RUN] Coach State | {domain}: {summary[:100]}")
    else:
        fn(domain, summary, confidence)
        print(f"    [Coach State] {domain}: {summary[:80]}")


# ---------------------------------------------------------------------------
# Write-back: check if agent wants to propose a program change
# ---------------------------------------------------------------------------

def check_for_write_back_proposals(email_text: str) -> str:
    """
    Look for the agent's proposal pattern at the end of the email.
    Pattern: "One thing: [proposal]. Want me to update the sheet?"

    Returns the proposal text if found, empty string otherwise.
    """
    lower = email_text.lower()
    if "want me to update the sheet" in lower or "want me to update the program" in lower:
        sentences = email_text.replace("\n", " ").split(".")
        for s in sentences:
            if "want me to update" in s.lower():
                return s.strip()
    return ""


def _extract_commit_markers(email_text: str) -> tuple[str, list[dict]]:
    """
    Extract [COMMIT: description | due: YYYY-MM-DD] markers from email text.
    Returns (clean_text, list of {commitment, due_date} dicts).
    Format: [COMMIT: I'll check your elbow recovery next week | due: 2026-03-22]
    The due: part is optional.
    """
    import re
    commits = []
    pattern = r'\[COMMIT:\s*(.*?)\]'
    for match in re.finditer(pattern, email_text, re.IGNORECASE | re.DOTALL):
        raw = match.group(1).strip()
        due_date = ""
        if " | due: " in raw.lower():
            # rsplit on the lowercased version to find the split point, then
            # extract from original to preserve commitment text casing.
            raw_lower = raw.lower()
            split_idx = raw_lower.rfind(" | due: ")
            commitment = raw[:split_idx].strip()
            due_date = raw[split_idx + len(" | due: "):].strip()
        else:
            commitment = raw
        if commitment:
            commits.append({"commitment": commitment, "due_date": due_date})
    clean = re.sub(pattern, '', email_text, flags=re.IGNORECASE | re.DOTALL).strip()
    return clean, commits


def log_pending_proposal(proposal_text: str, existing_commands: list[dict]) -> None:
    """
    Log a write-back proposal to the Commands tab so it persists to the next run.
    Skips if an identical or very similar PENDING_PROPOSAL already exists.
    """
    # Check for duplicates — avoid re-logging the same proposal
    proposal_lower = proposal_text.lower()[:80]
    for cmd in existing_commands:
        if cmd.get("Command", "").upper() == "PENDING_PROPOSAL":
            if cmd.get("Applied", "").upper() != "Y":
                existing_val = cmd.get("Value", "").lower()[:80]
                # Simple similarity: if 60%+ of words overlap, treat as duplicate
                words_new = set(proposal_lower.split())
                words_old = set(existing_val.split())
                if words_new and words_old:
                    overlap = len(words_new & words_old) / len(words_new | words_old)
                    if overlap > 0.6:
                        return  # already logged

    try:
        from memory import append_command
        append_command("PENDING_PROPOSAL", proposal_text)
        print(f"    [Proposal logged to Commands]: {proposal_text[:80]}")
    except Exception as e:
        print(f"    Proposal logging failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def detect_difficulty_patterns(program_data: dict,
                                telegram_log: list[dict] = None) -> list[dict]:
    """
    Scan session notes across recent weeks for per-lift difficulty signals.
    3+ consecutive easy signals → propose weight bump.
    3+ consecutive hard/failed signals → flag for deload/check.
    Returns list of {lift, signal, count, note} dicts.
    """
    EASY_KEYWORDS = {"easy", "light", "too easy", "felt light", "felt easy", "could do more"}
    HARD_KEYWORDS = {"failed", "fail", "missed", "couldn't", "too heavy", "struggled", "couldn't finish"}

    lift_signals: dict[str, list[str]] = {}  # lift_name → ["easy"|"hard", ...]
    lift_weeks: dict[str, set] = {}           # lift_name → set of week nums with signals

    current_week = program_data.get("current_week", {})
    recent_weeks = program_data.get("recent_weeks", [])
    all_weeks = recent_weeks + ([current_week] if current_week else [])

    for week in all_weeks:
        week_num = week.get("week_num", 0)
        for day in week.get("days", []):
            for ex in day.get("exercises", []):
                name = (ex.get("name") or "").strip()
                if not name:
                    continue
                note = (ex.get("session_note") or ex.get("notes") or "").lower()
                done = ex.get("done")

                signal = None
                if done is False:
                    signal = "hard"
                elif note:
                    if any(kw in note for kw in EASY_KEYWORDS):
                        signal = "easy"
                    elif any(kw in note for kw in HARD_KEYWORDS):
                        signal = "hard"

                if signal:
                    lift_signals.setdefault(name, []).append(signal)
                    lift_weeks.setdefault(name, set()).add(week_num)

    # Supplement with Telegram MOOD_PERFORMANCE notes
    if telegram_log:
        for entry in telegram_log:
            msg = (entry.get("Message") or "").lower()
            for lift_name in list(lift_signals.keys()):
                if lift_name.lower() in msg:
                    if any(kw in msg for kw in EASY_KEYWORDS):
                        lift_signals[lift_name].append("easy")
                    elif any(kw in msg for kw in HARD_KEYWORDS):
                        lift_signals[lift_name].append("hard")

    flags = []
    for lift_name, signals in lift_signals.items():
        if len(signals) < 3:
            continue
        # Require signals from at least 2 different weeks to avoid one-week flukes
        if len(lift_weeks.get(lift_name, set())) < 2:
            continue
        recent = signals[-6:]
        easy_count = recent.count("easy")
        hard_count = recent.count("hard")
        if easy_count >= 3:
            flags.append({
                "lift": lift_name,
                "signal": "easy",
                "count": easy_count,
                "note": f"{lift_name}: {easy_count}/{len(recent)} recent sessions felt easy/light — consider weight bump",
            })
        elif hard_count >= 3:
            flags.append({
                "lift": lift_name,
                "signal": "hard",
                "count": hard_count,
                "note": f"{lift_name}: {hard_count}/{len(recent)} sessions failed/too heavy — check deload need",
            })
    return flags


def run_think(week_num: int = None, dry_run: bool = False):
    """Run the strategic planning pass only. No email sent."""
    from sheets import read_program_data
    from memory import read_all
    from planner import run_planning_pass, run_telegram_summarization, run_annual_arc

    if week_num is None:
        week_num = compute_current_week(resolve_program_start_date())

    today = date.today()
    print(f"[{today}] Running strategic planning pass for Week {week_num}...")

    program_data = read_program_data(week_num=week_num)
    memory_data = read_all()

    # 1. Summarize old Telegram log entries → TELEGRAM_HISTORY Coach State
    print("  Compressing Telegram log history...")
    run_telegram_summarization(memory_data, dry_run=dry_run)

    # 2. Update ATHLETE_MODEL Coach State — quarterly psychological model
    _update_athlete_model(memory_data, dry_run=dry_run)

    # 3. Archive old rows from Lift History and Health Log (rows > 1 year old)
    from memory import archive_old_rows, TAB_LIFT_HISTORY, TAB_HEALTH_LOG
    cutoff = today.replace(year=today.year - 1)
    if not dry_run:
        print(f"  Archiving rows older than {cutoff} from Lift History and Health Log...")
        moved_lifts = archive_old_rows(TAB_LIFT_HISTORY, before_date=cutoff)
        moved_health = archive_old_rows(TAB_HEALTH_LOG, before_date=cutoff)
        if moved_lifts or moved_health:
            print(f"  Archived: {moved_lifts} lift rows, {moved_health} health rows.")
        else:
            print("  Nothing to archive yet.")
    else:
        print(f"  [dry-run] Would archive rows older than {cutoff} from Lift History and Health Log.")

    # 4. Annual arc — year-level retrospective + 12-month forward view (monthly gate)
    print("  Updating annual arc...")
    run_annual_arc(memory_data, dry_run=dry_run)

    # 5. Strategic planning pass (informed by annual arc now in Coach State)
    memory_data = read_all()  # re-read so planning prompt includes freshly written ANNUAL_ARC
    run_planning_pass(program_data, memory_data, week_num, dry_run=dry_run)
    print("Planning pass complete.")

    # 6. Bi-monthly steer co — initiate if ~60 days since last one
    from planner import _initiate_steer_co
    memory_data = read_all()  # re-read for fresh Coach State
    _initiate_steer_co(memory_data, dry_run=dry_run)


def _update_athlete_model(memory_data: dict, dry_run: bool = False) -> None:
    """
    Update the ATHLETE_MODEL Coach State domain — the coach's psychological model
    of the athlete. Called quarterly (via run_think). Budget-conscious: Haiku, max 400 tokens.
    Captures: response to feedback, psychological patterns, known weaknesses, what motivates.
    """
    from memory import read_coach_state, upsert_coach_state

    coach_state = memory_data.get("coach_state") or read_coach_state()
    existing_model = coach_state.get("ATHLETE_MODEL", {}).get("summary", "")
    last_updated = coach_state.get("ATHLETE_MODEL", {}).get("last_updated", "")

    # Only update quarterly (every ~90 days) to save budget
    if last_updated:
        try:
            from datetime import datetime as _dt
            days_since = (date.today() - _dt.strptime(last_updated[:10], "%Y-%m-%d").date()).days
            if days_since < 85:
                print(f"  ATHLETE_MODEL: last updated {days_since}d ago — skipping (quarterly update).")
                return
        except (ValueError, TypeError):
            pass

    # Build input context
    profile = memory_data.get("athlete_profile", "")
    coach_log = memory_data.get("coach_log", [])
    focus = memory_data.get("coach_focus", [])
    planning_notes = memory_data.get("planning_notes", [])

    log_snippets = "\n".join(
        f"  [{e.get('Date', '')}] {e.get('Key Observations', '')[:150]}"
        for e in coach_log[-20:]
    )
    focus_snippets = "\n".join(
        f"  [{f.get('Category', '')}] {f.get('Item', '')[:120]}"
        for f in focus[-15:]
        if f.get("Status", "") == "OPEN"
    )
    plan_snippet = planning_notes[-1].get("notes", "")[:400] if planning_notes else ""

    prompt = (
        f"Based on coaching history, build a psychological model of this athlete.\n\n"
        f"ATHLETE PROFILE:\n{profile[:400]}\n\n"
        f"RECENT COACH OBSERVATIONS:\n{log_snippets}\n\n"
        f"CURRENT WATCH LIST:\n{focus_snippets}\n\n"
        f"LAST PLANNING NOTES:\n{plan_snippet}\n\n"
        f"EXISTING MODEL (update, don't discard):\n{existing_model or '(none yet)'}\n\n"
        f"Output 4-6 sentences covering: how the athlete responds to direct feedback, "
        f"known psychological patterns (excuses, motivation triggers, avoidance), "
        f"what coaching approach works best, weaknesses to keep watching."
    )

    if dry_run:
        print("  [DRY RUN] Would update ATHLETE_MODEL Coach State.")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        model_text = result.content[0].text.strip()
        upsert_coach_state("ATHLETE_MODEL", model_text, "MEDIUM")
        print(f"  ATHLETE_MODEL Coach State written ({len(model_text)} chars).")
    except Exception as e:
        print(f"  ATHLETE_MODEL update failed (non-fatal): {e}")


def detect_rpe_patterns(lift_history: list[dict],
                        existing_commands: list[dict] = None) -> list[dict]:
    """
    Scan Lift History notes for RPE data (written as "RPE N" or "@RPEN" by the processor).
    If 3+ consecutive sessions of a lift show RPE consistently >= 2 above/below a neutral
    baseline (RPE 7), generate auto-regulation proposals.

    Returns list of {lift, signal, avg_rpe, count, proposal} dicts.
    Budget: pure Python, zero LLM calls.
    """
    import re as _re

    RPE_NEUTRAL = 7.0
    OVERLOAD_THRESHOLD = 1.5   # consistently above neutral → too heavy
    UNDERLOAD_THRESHOLD = 1.5  # consistently below neutral → too light
    MIN_SESSIONS = 3

    lift_rpe: dict[str, list[float]] = {}

    for row in lift_history:
        exercise = row.get("Exercise", "").strip()
        if not exercise:
            continue
        notes = (row.get("Notes", "") or "").strip()
        # Match "RPE 8", "RPE8", "@RPE8.5", "@RPE 8"
        rpe_match = _re.search(r"@?RPE\s*(\d+(?:\.\d+)?)", notes, _re.IGNORECASE)
        if rpe_match:
            try:
                rpe_val = float(rpe_match.group(1))
                lift_rpe.setdefault(exercise, []).append(rpe_val)
            except (ValueError, TypeError):
                pass

    proposals = []
    existing_proposal_text = " ".join(
        c.get("Value", "").lower() for c in (existing_commands or [])
        if c.get("Command", "").upper() == "PENDING_PROPOSAL"
        and c.get("Applied", "").upper() not in ("Y", "DECLINED")
    )

    for exercise, rpe_values in lift_rpe.items():
        if len(rpe_values) < MIN_SESSIONS:
            continue
        recent = rpe_values[-MIN_SESSIONS:]
        avg = sum(recent) / len(recent)
        diff = avg - RPE_NEUTRAL

        if exercise.lower() in existing_proposal_text:
            continue  # proposal already pending

        if diff >= OVERLOAD_THRESHOLD:
            proposals.append({
                "lift": exercise,
                "signal": "overload",
                "avg_rpe": round(avg, 1),
                "count": len(recent),
                "proposal": (
                    f"RPE auto-regulation for {exercise}: avg RPE {avg:.1f} over last "
                    f"{len(recent)} sessions (neutral=7, yours={avg:.1f} — too heavy). "
                    f"Propose reducing working weight by 5% to bring RPE back to 7-8 range. "
                    f"Want me to update the program sheet?"
                ),
            })
        elif diff <= -UNDERLOAD_THRESHOLD:
            proposals.append({
                "lift": exercise,
                "signal": "underload",
                "avg_rpe": round(avg, 1),
                "count": len(recent),
                "proposal": (
                    f"RPE auto-regulation for {exercise}: avg RPE {avg:.1f} over last "
                    f"{len(recent)} sessions (neutral=7, yours={avg:.1f} — too light). "
                    f"Propose increasing working weight by 2.5-5% to create adequate stimulus. "
                    f"Want me to update the program sheet?"
                ),
            })

    return proposals


def compute_session_quality(program_data: dict, lift_history: list[dict]) -> dict:
    """
    Compute a session quality score for the most recent completed session.
    Pure Python — no LLM calls.

    Score = completion_pct * 0.4 + rpe_alignment * 0.4 + mood_modifier * 0.2
    Where:
      completion_pct: fraction of exercises done (0-1)
      rpe_alignment: 1 - abs(avg_rpe - 7.5) / 7.5 (how close to ideal RPE zone 7-8)
      mood_modifier: detected from session notes (positive = 1.0, neutral = 0.7, negative = 0.4)

    Returns dict with keys: score (0-100), completion_pct, rpe_alignment, mood, session_label.
    """
    import re as _re

    POSITIVE_WORDS = {"great", "good", "strong", "solid", "easy", "felt good", "best"}
    NEGATIVE_WORDS = {"bad", "tired", "exhausted", "failed", "struggled", "sick", "hurt"}

    current_week = program_data.get("current_week", {})
    sessions = []
    for day in current_week.get("days", []):
        if any(ex.get("done") for ex in day.get("exercises", [])):
            sessions.append(day)

    if not sessions:
        return {}

    # Use the most recently completed session
    last_session = sessions[-1]
    exercises = last_session.get("exercises", [])
    session_label = last_session.get("label", "session")

    # Completion %
    total = len(exercises)
    done = sum(1 for ex in exercises if ex.get("done"))
    completion_pct = done / total if total else 0

    # RPE alignment: check exercise notes directly first (most reliable path),
    # then fall back to lift_history lookup (column name may vary).
    rpe_values = []
    for ex in exercises:
        note = (ex.get("session_note") or ex.get("notes") or "").strip()
        m = _re.search(r"@?RPE\s*(\d+(?:\.\d+)?)", note, _re.IGNORECASE)
        if m:
            try:
                rpe_values.append(float(m.group(1)))
            except (ValueError, TypeError):
                pass
    if not rpe_values:
        session_day = last_session.get("label", "")
        for row in reversed(lift_history[-30:]):
            if session_day.lower() in row.get("Day", "").lower():
                notes = (row.get("Notes", "") or "").strip()
                m = _re.search(r"@?RPE\s*(\d+(?:\.\d+)?)", notes, _re.IGNORECASE)
                if m:
                    try:
                        rpe_values.append(float(m.group(1)))
                    except (ValueError, TypeError):
                        pass

    rpe_alignment = 0.7  # neutral default if no RPE data
    if rpe_values:
        avg_rpe = sum(rpe_values) / len(rpe_values)
        rpe_alignment = max(0.0, 1.0 - abs(avg_rpe - 7.5) / 7.5)

    # Mood modifier from session notes
    session_note = " ".join(
        (ex.get("session_note") or ex.get("notes") or "").lower()
        for ex in exercises
    )
    mood = "neutral"
    if any(w in session_note for w in POSITIVE_WORDS):
        mood = "positive"
        mood_mod = 1.0
    elif any(w in session_note for w in NEGATIVE_WORDS):
        mood = "negative"
        mood_mod = 0.4
    else:
        mood_mod = 0.7

    score = round((completion_pct * 0.4 + rpe_alignment * 0.4 + mood_mod * 0.2) * 100)

    return {
        "score": score,
        "completion_pct": round(completion_pct * 100),
        "rpe_alignment": round(rpe_alignment * 100),
        "mood": mood,
        "session_label": session_label,
    }


def run_proactive(dry_run: bool = False):
    """
    Lightweight proactive check-in. No email, no program sheet, no Tier 0 archives.
    Reads compressed memory (6 tabs incl. health_log), reasons with Claude Haiku,
    optionally sends Telegram. Also runs HealthAgent health-specific proactive check.
    Called: (a) directly via --proactive flag 2x/day, (b) by run() when SKIP_UNTIL is active.
    SKIP_UNTIL only blocks the email pipeline — this pass always runs.
    """
    from memory import (read_coach_state, read_coach_focus, read_athlete_preferences,
                        read_telegram_log, read_commands, read_health_log,
                        read_athlete_profile, read_life_context, read_commitments,
                        log_coach_run)
    from prompt import build_proactive_prompt

    today = date.today()
    print(f"[{today}] Running proactive check-in pass...")

    coach_state = read_coach_state()
    health_log = read_health_log(limit=14)
    memory_data = {
        "coach_state":         coach_state,
        "coach_focus":         read_coach_focus(),
        "athlete_preferences": read_athlete_preferences(),
        "telegram_log":        read_telegram_log(),
        "commands":            read_commands(),
        "health_log":          health_log,
        "life_context":        read_life_context(limit=15),   # for travel context detection
        "commitments":         read_commitments(),
    }

    # Load current week so proactive pass knows what's scheduled today
    program_data_p = None
    try:
        from sheets import read_program_data
        program_data_p = read_program_data(week_num=compute_current_week(resolve_program_start_date()), lookback=0)
    except Exception as e:
        print(f"  Proactive: program load failed (non-fatal): {e}")

    system_prompt, user_message = build_proactive_prompt(memory_data, program_data=program_data_p)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": user_message}],
        system=system_prompt,
    )
    output = response.content[0].text.strip()
    print(f"  Coach reasoning: {output[:150]}")

    _, tg_alert = extract_telegram_alert(output)
    _, focus_updates = parse_coach_focus_markers(output)

    today_str = str(today)
    if tg_alert:
        if dry_run:
            print(f"  [DRY RUN] Would send Telegram: {tg_alert}")
        else:
            # Dedup: one proactive message per day
            last_sent = coach_state.get("LAST_PROACTIVE", {}).get("summary", "")
            if last_sent == today_str:
                print(f"  Proactive dedup: already sent today — skipping.")
            else:
                from telegram_utils import send_telegram_message
                sent = send_telegram_message(tg_alert)
                if sent:
                    print(f"  Proactive Telegram sent: {tg_alert[:80]}")
                    from memory import upsert_coach_state
                    upsert_coach_state("LAST_PROACTIVE", today_str, "HIGH")
                    log_coach_run(
                        observations="Proactive check-in pass",
                        email_summary=f"[PROACTIVE] {tg_alert}",
                    )
    else:
        print("  Proactive pass: no outreach needed.")

    if focus_updates and not dry_run:
        write_coach_focus_updates(focus_updates)

    # --- HealthAgent proactive check (Haiku, once per day) ---
    try:
        from health_agent import run_health_proactive
        # Dedup: only once per day — HealthAgent can run on both 08:00 and 14:00 passes
        last_health = coach_state.get("LAST_HEALTH_PROACTIVE", {}).get("summary", "")
        if last_health == today_str and not dry_run:
            print("  HealthAgent proactive: already sent today — skipping.")
        else:
            athlete_profile = ""
            try:
                from memory import read_athlete_profile
                athlete_profile = read_athlete_profile()
            except Exception:
                pass
            health_tg = run_health_proactive(health_log, coach_state, athlete_profile)
            if health_tg:
                if dry_run:
                    print(f"  [DRY RUN] HealthAgent would send: {health_tg}")
                else:
                    from telegram_utils import send_telegram_message
                    sent = send_telegram_message(health_tg)
                    if sent:
                        print(f"  HealthAgent proactive sent: {health_tg[:80]}")
                        from memory import upsert_coach_state
                        upsert_coach_state("LAST_HEALTH_PROACTIVE", today_str, "HIGH")
                        log_coach_run(
                            observations="Health proactive check-in",
                            email_summary=f"[HEALTH_PROACTIVE] {health_tg}",
                        )
            else:
                print("  HealthAgent proactive: no health outreach needed.")
    except Exception as e:
        print(f"  HealthAgent proactive failed (non-fatal): {e}")

    # --- Schedule discovery fallback: if it hasn't happened in 6+ days, ask now ---
    # This catches missed Sunday discoveries (e.g. it's already midday Sunday, or a weekday catch-up)
    try:
        run_weekly_schedule_discovery(dry_run=dry_run)
    except Exception as e:
        print(f"  Schedule discovery fallback failed (non-fatal): {e}")


def _send_weekly_digest(memory_data: dict, program_data: dict,
                        projections: dict, week_num: int) -> None:
    """
    Push a compact weekly digest to Telegram after the Sunday email.
    Key numbers only — no coaching prose. Athlete can read the email for the full story.
    """
    from telegram_utils import send_telegram_message

    lines = [f"📊 Week {week_num} digest:"]

    # Session completion
    current_week = program_data.get("current_week", {})
    sessions = current_week.get("sessions", [])
    if sessions:
        done = sum(1 for s in sessions if s.get("completed"))
        lines.append(f"  Sessions: {done}/{len(sessions)} completed")

    # Est 1RM per main lift
    lift_projs = projections.get("lift_projections", [])
    if lift_projs:
        for p in lift_projs:
            if p and p.get("current_1rm"):
                rate_str = f"{p['rate_per_week']:+.1f}kg/wk" if p.get("rate_per_week") is not None else ""
                track = " ✓" if p.get("on_track") else (" ✗" if p.get("on_track") is False else "")
                lines.append(f"  {p['exercise']}: {p['current_1rm']}kg est. 1RM {rate_str}{track}")

    # Fatigue
    fatigue = projections.get("fatigue")
    if fatigue:
        flag = " ⚠ deload needed" if fatigue.get("deload_recommended") else ""
        lines.append(f"  Fatigue TSB: {fatigue['TSB']:+.1f} ({fatigue['readiness'].split(' — ')[0]}){flag}")

    # Bodyweight from health log
    health_log = memory_data.get("health_log", [])
    bw_entries = [e for e in health_log if e.get("Bodyweight (kg)")]
    if bw_entries:
        try:
            latest_bw = float(bw_entries[-1]["Bodyweight (kg)"])
            lines.append(f"  Bodyweight: {latest_bw}kg")
        except (ValueError, TypeError):
            pass

    # Goal proximity
    gp = projections.get("goal_proximity", [])
    for alert in gp:
        if alert["urgent"]:
            lines.append(f"  🏆 {alert['lift']} goal REACHED ({alert['current_1rm']}kg)")
        else:
            lines.append(f"  🎯 {alert['lift']} {alert['gap']}kg from goal")

    msg = "\n".join(lines)
    sent = send_telegram_message(msg)
    if sent:
        print(f"  Weekly Telegram digest sent ({len(lines)} lines)")


def run(week_num: int = None, dry_run: bool = False, no_sync: bool = False,
        force_weekly: bool = False):
    from sheets import read_program_data
    from memory import (read_all, sync_sessions_to_history, sync_health_log,
                        log_coach_run, get_last_run_date, check_skip_today,
                        expire_stale_focus_items)
    from prompt import build_prompt
    from gmail import read_recent_replies

    # Auto-compute week if not overridden
    if week_num is None:
        week_num = compute_current_week(resolve_program_start_date())

    today = date.today()
    print(f"[{today}] Running coach for Week {week_num}...")

    # --- Check for skip command ---
    skip_until = check_skip_today()
    if skip_until:
        print(f"  SKIP_UNTIL active (emails paused until {skip_until}) — running proactive check-in instead.")
        run_proactive(dry_run=dry_run)
        return None

    # 1. Expire stale Coach Focus items (Priority-aware: PINNED=never, HIGH=90d, NORMAL=30d)
    try:
        expired = expire_stale_focus_items()
        if expired:
            print(f"  → {expired} stale focus item(s) expired")
    except Exception as e:
        print(f"  Stale focus expiry failed (non-fatal): {e}")

    # 2. Process unprocessed Telegram messages (classify → structured facts → memory)
    print("  Processing Telegram messages...")
    try:
        from processor import process_telegram_messages
        process_telegram_messages(dry_run=dry_run)
    except Exception as e:
        print(f"  Telegram processor failed (non-fatal): {e}")

    # 3. Read program sheet
    print("  Reading program sheet...")
    program_data = read_program_data(week_num=week_num)

    # 4. Read coach memory
    print("  Reading coach memory...")
    memory_data = read_all()

    # --- Check for program completion ---
    program_complete = False
    try:
        from memory import get_active_program_info
        prog_info = get_active_program_info()
        if prog_info:
            total_weeks = int(prog_info.get("total_weeks") or 0)
            if total_weeks and week_num >= total_weeks:
                program_complete = True
                print(f"  *** PROGRAM COMPLETE: Week {week_num}/{total_weeks} ***")
    except Exception as e:
        print(f"  Program completion check failed (non-fatal): {e}")

    # --- Determine email type (after memory load so we can read Athlete Preferences) ---
    # Recap day is read from Athlete Preferences (SCHEDULE | weekly_recap_day: <dayname>)
    # Falls back to Sunday (weekday 6). Athlete can change this via Telegram.
    recap_weekday = _get_recap_weekday(memory_data.get("athlete_preferences", []))
    is_weekly_summary = force_weekly or (today.weekday() == recap_weekday)
    if is_weekly_summary:
        day_name = ["Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday"][recap_weekday]
        print(f"  Weekly summary mode ({day_name} or --weekly flag).")

    # 5. Compute projections (pure Python — facts for prompt + Coach State)
    print("  Computing projections...")
    try:
        from projections import run_all_projections
        projections = run_all_projections(memory_data, program_data=program_data)
        if projections.get("formatted"):
            print(f"    → {projections['formatted'].count(chr(10)) + 1} projection line(s) computed")
        # Volume spike alerting
        spikes = projections.get("volume_spikes", [])
        if spikes and not dry_run and not no_sync:
            from memory import append_coach_focus
            existing_focus = memory_data.get("coach_focus", [])
            open_items_text = [f.get("Item", "").lower() for f in existing_focus
                               if f.get("Status", "OPEN") == "OPEN"]
            for spike in spikes:
                note = (f"{spike['lift']}: volume spike {spike['pct_increase']}% "
                        f"({spike['from_tonnage']}→{spike['to_tonnage']}kg tonnage "
                        f"{spike['from_week']}→{spike['to_week']}) — injury risk window")
                if not any(spike['lift'].lower() in t and "volume spike" in t for t in open_items_text):
                    append_coach_focus("CONCERN", note, priority="HIGH", last_mentioned=str(today))
                    print(f"    [Volume spike] {note[:80]}")
        # Deload auto-detection + smart proposal
        fatigue = projections.get("fatigue")
        if fatigue and fatigue.get("deload_recommended") and not dry_run and not no_sync:
            from memory import append_coach_focus
            deload_note = (f"TSB={fatigue['TSB']:+.1f} (ATL={fatigue['ATL']:.1f} CTL={fatigue['CTL']:.1f}) "
                           f"— {fatigue['readiness']}. Accumulated fatigue exceeds fitness buffer.")
            print(f"    [Deload signal] {deload_note}")
            existing_focus = memory_data.get("coach_focus", [])
            open_items_text = [f.get("Item", "").lower() for f in existing_focus
                               if f.get("Status", "OPEN") == "OPEN"]
            if "deload" not in " ".join(open_items_text):
                append_coach_focus("CONCERN", deload_note, priority="HIGH", last_mentioned=str(today))
            # Auto-draft a deload proposal and log as PENDING_PROPOSAL
            existing_commands = memory_data.get("commands", [])
            deload_proposal = (
                f"Deload week proposal (auto-triggered by fatigue model): "
                f"Reduce all working sets to 60% of current load, cut total volume by 40%, "
                f"maintain movement patterns. TSB={fatigue['TSB']:+.1f} indicates accumulated "
                f"fatigue — a 5-7 day recovery week will restore readiness before the next block. "
                f"Want me to adjust the program sheet for this week?"
            )
            log_pending_proposal(deload_proposal, existing_commands)
            try:
                from telegram_utils import send_telegram_message
                send_telegram_message(
                    f"⚠️ Fatigue alert: TSB={fatigue['TSB']:+.1f} ({fatigue['readiness']}). "
                    f"I've drafted a deload week proposal — reply 'yes' to apply it, or we'll discuss in tonight's email."
                )
            except Exception:
                pass
    except Exception as e:
        print(f"  Projections failed (non-fatal): {e}")
        projections = {}

    # 5. Get last run date for delta detection
    last_run_date = get_last_run_date()
    if last_run_date:
        print(f"  Last email: {last_run_date} — computing delta...")

    # 6. Read email replies (since last run)
    print("  Checking for email replies...")
    replies = read_recent_replies(after_date=last_run_date, max_results=5)
    if replies:
        print(f"    → {len(replies)} reply(ies) found")
        # Preprocess replies: extract structured facts via Haiku (same as Telegram processor)
        if not no_sync:
            n = preprocess_email_replies(replies, dry_run=dry_run)
            if n:
                print(f"    → {n} fact(s) extracted from email replies")

    # 7. Sync new data to memory (unless --no-sync)
    if not no_sync:
        print("  Syncing new session data to history...")
        new_sessions = sync_sessions_to_history(program_data)
        if new_sessions:
            print(f"    → {len(new_sessions)} new exercise completions logged")

        print("  Syncing health log...")
        new_health = sync_health_log(program_data)
        if new_health:
            print(f"    → {len(new_health)} new health entries logged")

    # 8. Override telegram_log with entries since last email (email should see full dialogue)
    if last_run_date:
        try:
            from memory import read_telegram_log_since
            memory_data["telegram_log"] = read_telegram_log_since(last_run_date)
            print(f"    → {len(memory_data['telegram_log'])} Telegram message(s) since last email")
        except Exception as e:
            print(f"  Telegram log filter failed (non-fatal): {e}")

    # 8b. Build prompt (initial pass, without plateau dives)
    print("  Building prompt...")
    # Goal proximity alerting (within 5kg of a target → landmark + Telegram)
    goal_proximity = projections.get("goal_proximity", [])
    if goal_proximity and not dry_run and not no_sync:
        from memory import append_coach_focus
        try:
            from telegram_utils import send_telegram_message
        except Exception:
            send_telegram_message = None
        existing_focus = memory_data.get("coach_focus", [])
        open_items_text = [f.get("Item", "").lower() for f in existing_focus
                           if f.get("Status", "OPEN") == "OPEN"]
        for alert in goal_proximity:
            lift = alert["lift"]
            if alert["urgent"]:
                msg = (f"🏆 {lift} GOAL REACHED: {alert['current_1rm']}kg est. 1RM "
                       f"(target was {alert['target']}kg) — milestone achieved!")
                focus_cat = "LANDMARK"
            else:
                msg = (f"🎯 {lift} closing in on goal: {alert['current_1rm']}kg / {alert['target']}kg target "
                       f"— {alert['gap']}kg to go")
                focus_cat = "TRACKING"
            if lift.lower() not in " ".join(open_items_text) or alert["urgent"]:
                append_coach_focus(focus_cat, msg, priority="HIGH", last_mentioned=str(today))
                print(f"    [Goal proximity] {msg}")
            if alert["urgent"] and send_telegram_message:
                send_telegram_message(msg)

    _prompt_kwargs = dict(
        last_run_date=last_run_date,
        replies=replies,
        is_weekly_summary=is_weekly_summary,
        projections_text=projections.get("formatted", ""),
        program_complete=program_complete,
        tonnage_by_lift=projections.get("tonnage_by_lift"),
        cross_program=projections.get("cross_program", ""),
        goal_proximity=goal_proximity,
    )
    system_prompt, user_message = build_prompt(program_data, memory_data, **_prompt_kwargs)

    # 9. Plateau detection + per-lift deep dives + auto-intervention
    print("  Checking for plateaus...")
    lift_history = memory_data.get("lift_history", [])
    plateau_dives = detect_plateaus_and_deep_dive(
        lift_history, system_prompt, tracked_lifts=memory_data.get("tracked_lifts"))
    if plateau_dives:
        # Rebuild prompt with deep dive context included
        system_prompt, user_message = build_prompt(
            program_data, memory_data,
            **{**_prompt_kwargs, "plateau_deep_dives": plateau_dives},
        )
        # Auto-intervention: log PENDING_PROPOSAL for each plateaued lift
        # so the coach has a concrete action item rather than just awareness
        if not dry_run and not no_sync:
            existing_commands = memory_data.get("commands", [])
            existing_focus = memory_data.get("coach_focus", [])
            open_proposal_text = " ".join(
                c.get("Value", "").lower() for c in existing_commands
                if c.get("Command", "").upper() == "PENDING_PROPOSAL"
                and c.get("Applied", "").upper() not in ("Y", "DECLINED")
            )
            for lift_name, analysis_text in plateau_dives.items():
                if lift_name.lower() in open_proposal_text:
                    print(f"    [Plateau] Proposal already pending for {lift_name} — skipping")
                    continue
                proposal = (
                    f"Plateau intervention for {lift_name}: "
                    f"lift has stalled for 3+ weeks. Proposed fix based on deep-dive analysis: "
                    f"{analysis_text[:200].strip()} "
                    f"— want me to apply a loading/technique change to the program sheet?"
                )
                log_pending_proposal(proposal, existing_commands)
                print(f"    [Plateau intervention] Auto-proposal logged for {lift_name}")

    # 9b. Outcome learning loop: detect easy/hard difficulty patterns per lift
    print("  Running outcome learning loop...")
    try:
        difficulty_flags = detect_difficulty_patterns(
            program_data=program_data,
            telegram_log=memory_data.get("telegram_log", []),
        )
        if difficulty_flags:
            print(f"    → {len(difficulty_flags)} difficulty pattern(s) detected")
            if not dry_run and not no_sync:
                existing_focus = memory_data.get("coach_focus", [])
                open_items_text = [f.get("Item", "").lower() for f in existing_focus
                                   if f.get("Status", "OPEN") == "OPEN"]
                today_str = str(today)
                for flag in difficulty_flags:
                    # Skip if already flagged for this lift
                    already = any(flag["lift"].lower() in t for t in open_items_text)
                    if not already:
                        from memory import append_coach_focus
                        append_coach_focus("TRACKING", flag["note"],
                                           priority="HIGH", last_mentioned=today_str)
                        print(f"    [Outcome loop] {flag['note'][:80]}")
            elif dry_run:
                for flag in difficulty_flags:
                    print(f"    [DRY RUN] Outcome loop: {flag['note']}")
        else:
            print("    → No difficulty patterns found")
    except Exception as e:
        print(f"  Outcome learning loop failed (non-fatal): {e}")

    # 9c. RPE auto-regulation: scan lift history for RPE patterns → auto-proposals
    print("  Checking RPE patterns...")
    try:
        rpe_proposals = detect_rpe_patterns(
            lift_history=lift_history,
            existing_commands=memory_data.get("commands", []),
        )
        if rpe_proposals:
            print(f"    → {len(rpe_proposals)} RPE auto-regulation proposal(s)")
            if not dry_run and not no_sync:
                existing_commands = memory_data.get("commands", [])
                for p in rpe_proposals:
                    log_pending_proposal(p["proposal"], existing_commands)
                    print(f"    [RPE] {p['lift']}: {p['signal']} (avg RPE {p['avg_rpe']})")
            elif dry_run:
                for p in rpe_proposals:
                    print(f"    [DRY RUN] RPE {p['signal']}: {p['proposal'][:80]}")
        else:
            print("    → No RPE auto-regulation needed")
    except Exception as e:
        print(f"  RPE pattern check failed (non-fatal): {e}")

    # 9d. Session quality score: pure Python, stored in Coach State
    print("  Computing session quality score...")
    try:
        sq = compute_session_quality(program_data, lift_history)
        if sq:
            sq_summary = (
                f"Score {sq['score']}/100 | completion {sq['completion_pct']}% | "
                f"RPE alignment {sq['rpe_alignment']}% | mood {sq['mood']} | {sq['session_label']}"
            )
            # Append rolling history: read previous value, keep last 5 scores
            import re as _re_sq
            prev_state = memory_data.get("coach_state", {})
            prev_sq = prev_state.get("SESSION_QUALITY", {}).get("summary", "")
            history_match = _re_sq.search(r'History:\s*\[([^\]]*)\]', prev_sq)
            prev_scores = [s.strip() for s in history_match.group(1).split(",") if s.strip()] if history_match else []
            prev_scores.append(str(sq['score']))
            prev_scores = prev_scores[-5:]  # keep last 5
            avg_score = round(sum(float(s) for s in prev_scores) / len(prev_scores), 1)
            sq_summary += f" | History: [{', '.join(prev_scores)}] avg={avg_score}"
            print(f"    → Session quality: {sq_summary}")
            if not dry_run and not no_sync:
                from memory import upsert_coach_state
                upsert_coach_state("SESSION_QUALITY", sq_summary, "HIGH")
        else:
            print("    → No completed session found for quality scoring")
    except Exception as e:
        print(f"  Session quality scoring failed (non-fatal): {e}")

    # 10. Analysis pass (reasoning before writing)
    print("  Running analysis pass...")
    analysis, analysis_usage = generate_analysis(system_prompt, user_message)
    if dry_run:
        print("\n--- ANALYSIS ---")
        print(analysis)
        print("--- END ANALYSIS ---\n")

    # 9. Generate email
    print("  Generating email with Claude...")
    email_text, email_usage = generate_email(system_prompt, user_message, analysis=analysis)
    run_cost = _compute_cost([analysis_usage, email_usage])
    print(f"  Email pipeline cost: ~${run_cost:.4f}")

    # 11. Extract output markers (Telegram alert + coach focus updates)
    email_text, tg_alert = extract_telegram_alert(email_text)
    if tg_alert:
        print(f"  [Telegram alert detected]: {tg_alert}")

    email_text, focus_updates = parse_coach_focus_markers(email_text)
    if focus_updates:
        print(f"  [Coach focus updates]: {len(focus_updates)} item(s)")
        if not no_sync and not dry_run:
            write_coach_focus_updates(focus_updates)
        elif dry_run:
            for u in focus_updates:
                print(f"    [DRY RUN] {u['category']}: {u['item'][:80]}")

    # Extract [COMMIT: ...] markers — coach promises logged to Commitments tab
    email_text, commit_items = _extract_commit_markers(email_text)
    if commit_items:
        print(f"  [Commitments]: {len(commit_items)} new commitment(s)")
        if not no_sync and not dry_run:
            try:
                from memory import append_commitment
                for ci in commit_items:
                    append_commitment(ci["commitment"], due_date=ci.get("due_date", ""))
                    print(f"    [Commit] {ci['commitment'][:80]}")
            except Exception as e:
                print(f"    Commitment logging failed (non-fatal): {e}")
        elif dry_run:
            for ci in commit_items:
                print(f"    [DRY RUN] COMMIT: {ci['commitment'][:80]}")

    # Bridge FOLLOWUP questions to Telegram + log as OPEN_QUESTION for cross-channel tracking
    followup_questions = [
        u["item"] for u in focus_updates
        if u["category"] == "FOLLOWUP" and "?" in u["item"]
    ]
    if followup_questions:
        for q in followup_questions:
            print(f"  [Question bridge → Telegram]: {q[:70]}")
            if not dry_run and not no_sync:
                try:
                    from memory import log_open_question
                    from telegram_utils import send_telegram_message
                    log_open_question(q, source="EMAIL")
                    send_telegram_message(q)
                except Exception as e:
                    print(f"    Question bridge failed (non-fatal): {e}")
            elif dry_run:
                print(f"    [DRY RUN] Would send to Telegram + log as OPEN_QUESTION")

    # 12. Check for write-back proposals — log to Commands so they persist
    proposal = check_for_write_back_proposals(email_text)
    if proposal:
        print(f"\n  [Write-back proposal detected]: {proposal}")
        print("  → Logging to Commands tab — will check for confirmation next run.")
        if not dry_run and not no_sync:
            existing_commands = memory_data.get("commands", [])
            log_pending_proposal(proposal, existing_commands)

    # 13. Generate charts (on Fridays / weekly summary)
    charts = None
    if is_weekly_summary:
        print("  Generating charts...")
        try:
            from charts import generate_1rm_chart, generate_volume_chart, generate_bodyweight_chart
            chart_list = []
            c1 = generate_1rm_chart(memory_data.get("lift_history", []),
                                    tracked_lifts=memory_data.get("tracked_lifts"))
            if c1:
                chart_list.append((c1, "chart-1rm"))
            c2 = generate_volume_chart(
                program_data.get("recent_weeks", []),
                program_data.get("current_week"),
            )
            if c2:
                chart_list.append((c2, "chart-volume"))
            c3 = generate_bodyweight_chart(memory_data.get("health_log", []))
            if c3:
                chart_list.append((c3, "chart-bw"))
            charts = chart_list if chart_list else None
            print(f"    → {len(chart_list)} chart(s) generated")
        except ImportError:
            print("    → matplotlib not installed, skipping charts")

    # 14. Output
    if dry_run:
        print("\n" + "=" * 60)
        print(f"COACHING EMAIL — {today}")
        print("=" * 60)
        print(email_text)
        if charts:
            print(f"\n[{len(charts)} chart(s) would be attached inline]")
        if tg_alert:
            print(f"\n[Telegram alert would send]: {tg_alert}")
        print("=" * 60)
        print("[DRY RUN — email not sent]")
    else:
        from gmail import send_email
        week_label = f"Week {week_num}"
        if is_weekly_summary:
            subject = f"{week_label} — Weekly Summary — {today.strftime('%b %d')}"
        else:
            subject = f"{week_label} — {today.strftime('%b %d')}"
        print(f"  Sending email: '{subject}'...")
        send_email(subject=subject, body=email_text, charts=charts or [])
        print("  Email sent.")

        # Send proactive Telegram alert if coach flagged one
        if tg_alert:
            try:
                from telegram_utils import send_telegram_message
                send_telegram_message(tg_alert)
                print(f"  Telegram alert sent: {tg_alert[:80]}")
            except Exception as e:
                print(f"  Telegram alert failed (non-fatal): {e}")

        # Weekly Sunday digest: push key numbers to Telegram
        if is_weekly_summary:
            try:
                _send_weekly_digest(
                    memory_data=memory_data,
                    program_data=program_data,
                    projections=projections,
                    week_num=week_num,
                )
            except Exception as e:
                print(f"  Weekly Telegram digest failed (non-fatal): {e}")

    # 15. Write Coach State summaries (bounded Tier 1 memory for next run)
    print("  Writing Coach State summaries...")
    write_coach_state_summaries(
        memory_data=memory_data,
        projections=projections,
        program_data=program_data,
        week_num=week_num,
        dry_run=dry_run or no_sync,
        is_weekly_summary=is_weekly_summary,
    )

    # Write LAST_EMAIL domain so Telegram bot knows what was said and what was asked
    if not dry_run and not no_sync:
        try:
            from memory import upsert_coach_state
            email_digest = email_text[:300].replace("\n", " ").strip()
            if followup_questions:
                asked = " | Asked: " + "; ".join(followup_questions[:3])
                email_digest = email_digest[:250] + asked
            upsert_coach_state("LAST_EMAIL", email_digest, "HIGH")
            print("  LAST_EMAIL Coach State written.")
        except Exception as e:
            print(f"  LAST_EMAIL state write failed (non-fatal): {e}")

    # 16. Log the run to Coach Memory
    if not no_sync and not dry_run:
        first_sentence = email_text.split(".")[0].strip()
        log_coach_run(
            observations=first_sentence[:200],
            email_summary=email_text[:500],
            cost_usd=run_cost,
        )
        print("  Run logged to Coach Memory.")

    return email_text


# ---------------------------------------------------------------------------
# Pre-session brief: sent ~1h before scheduled session
# ---------------------------------------------------------------------------

def run_brief(dry_run: bool = False):
    """
    Pre-session brief: sends a short, focused Telegram message before training.
    Reads today's scheduled exercises + Coach State to give a targeted prep note.
    Deduped via LAST_BRIEF Coach State — only fires once per day.
    """
    from sheets import read_program_data
    from memory import read_coach_state, upsert_coach_state, read_commitments

    today = date.today()
    week_num = compute_current_week(resolve_program_start_date())
    print(f"[{today}] Running pre-session brief (Week {week_num})...")

    # Dedup: only send once per day
    coach_state = read_coach_state()
    last_brief = coach_state.get("LAST_BRIEF", {}).get("summary", "")
    if last_brief == str(today):
        print("  Brief: already sent today — skipping.")
        return

    try:
        program_data = read_program_data(week_num=week_num, lookback=0)
    except Exception as e:
        print(f"  Brief: program load failed: {e}")
        return

    current_week = program_data.get("current_week", {})
    today_str = today.strftime("%A")
    today_sessions = [
        day for day in current_week.get("days", [])
        if today_str.lower() in day.get("label", "").lower()
    ]

    if not today_sessions:
        print("  Brief: no session scheduled today — skipping.")
        return

    # Check if session already done
    already_done = any(
        any(ex.get("done") for ex in day.get("exercises", []))
        for day in today_sessions
    )
    if already_done:
        print("  Brief: today's session already logged — skipping.")
        return

    # Build a concise session overview
    session_lines = []
    for day in today_sessions:
        exercises = day.get("exercises", [])
        main_lifts = [ex for ex in exercises if ex.get("weight")]
        for ex in main_lifts[:4]:  # top 4 lifts
            session_lines.append(
                f"  {ex.get('name', '?')}: {ex.get('weight', '?')} × {ex.get('sets_reps', '?')}"
            )

    # Pull relevant Coach State context (squat/bench/deload signal etc.)
    state_notes = []
    for domain in ("SQUAT", "BENCH", "DEADLIFT", "OHP", "HEALTH"):
        s = coach_state.get(domain, {}).get("summary", "")
        if s:
            state_notes.append(f"  {domain}: {s[:100]}")

    # Open commitments due today or this week
    open_commitments = read_commitments("OPEN")
    commitment_note = ""
    if open_commitments:
        commitment_note = " | ".join(c["Commitment"][:80] for c in open_commitments[:2])

    # Build brief via Haiku (cheap, targeted)
    session_text = "\n".join(session_lines) or "session details unavailable"
    state_text = "\n".join(state_notes) or ""
    prompt = (
        f"You are {ATHLETE_NAME}'s coach. Write a brief (2-3 sentences max) pre-session "
        f"Telegram message. Be direct and specific — what to focus on today, one cue or reminder.\n\n"
        f"TODAY'S SESSION ({today_str}):\n{session_text}\n\n"
        f"COACH STATE CONTEXT:\n{state_text}\n\n"
        f"Write the message now. No greeting, no sign-off. Short and useful."
    )

    if dry_run:
        print(f"  [DRY RUN] Brief would cover: {session_text[:80]}")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        brief_msg = result.content[0].text.strip()

        # Prepend commitment note if present
        if commitment_note:
            brief_msg += f"\n\n(Also tracking: {commitment_note})"

        from telegram_utils import send_telegram_message
        sent = send_telegram_message(brief_msg)
        if sent:
            print(f"  Brief sent: {brief_msg[:80]}")
            upsert_coach_state("LAST_BRIEF", str(today), "HIGH")
        else:
            print("  Brief: Telegram send failed.")
    except Exception as e:
        print(f"  Brief failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Post-session check-in: sent ~90min after expected session end
# ---------------------------------------------------------------------------

def run_post_session(dry_run: bool = False):
    """
    Post-session check-in: sent ~90 minutes after the expected end of today's session.
    If session was already logged → acknowledge performance briefly.
    If not logged yet → ask how it went (different from nudge — warmer, not just a reminder).
    Deduped via LAST_POST_SESSION Coach State.
    """
    from sheets import read_program_data
    from memory import read_coach_state, upsert_coach_state

    today = date.today()
    week_num = compute_current_week(resolve_program_start_date())
    print(f"[{today}] Running post-session check-in (Week {week_num})...")

    coach_state = read_coach_state()
    last_post = coach_state.get("LAST_POST_SESSION", {}).get("summary", "")
    if last_post == str(today):
        print("  Post-session: already ran today — skipping.")
        return

    try:
        program_data = read_program_data(week_num=week_num, lookback=0)
    except Exception as e:
        print(f"  Post-session: program load failed: {e}")
        return

    current_week = program_data.get("current_week", {})
    today_str = today.strftime("%A")
    today_sessions = [
        day for day in current_week.get("days", [])
        if today_str.lower() in day.get("label", "").lower()
    ]

    if not today_sessions:
        print("  Post-session: no session scheduled today — skipping.")
        return

    # Check completion state
    any_complete = any(
        any(ex.get("done") for ex in day.get("exercises", []))
        for day in today_sessions
    )

    session_label = today_sessions[0].get("label", "today's session")

    if any_complete:
        # Session was logged — get some basic stats and send a quick acknowledgment
        exercises = today_sessions[0].get("exercises", [])
        done_count = sum(1 for ex in exercises if ex.get("done"))
        total_count = len(exercises)
        msg = (
            f"Saw you logged {session_label} ({done_count}/{total_count} exercises). "
            f"How did it feel? Any notes on how the weights moved?"
        )
    else:
        # Session not logged — warm check-in (not a nudge)
        msg = (
            f"Hey — did you get {session_label} in today? "
            f"Even a quick 'yes, done' or 'skipped because X' helps me track things."
        )

    if dry_run:
        print(f"  [DRY RUN] Would send post-session: {msg}")
        return

    try:
        from telegram_utils import send_telegram_message
        sent = send_telegram_message(msg)
        if sent:
            print(f"  Post-session check-in sent: {msg[:80]}")
            upsert_coach_state("LAST_POST_SESSION", str(today), "HIGH")
    except Exception as e:
        print(f"  Post-session check-in failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Session completion nudge
# ---------------------------------------------------------------------------

def run_nudge(dry_run: bool = False):
    """
    Evening check: look at today's program, see if any sessions are unlogged.
    If they are and it's past 19:00 UTC (20:00 Spain), send a gentle Telegram push.
    Skips if a session was already completed or if Telegram was already sent today.
    """
    from sheets import read_program_data
    from memory import read_coach_state, upsert_coach_state

    today = date.today()
    week_num = compute_current_week(resolve_program_start_date())
    print(f"[{today}] Running session nudge check (Week {week_num})...")

    try:
        program_data = read_program_data(week_num=week_num, lookback=0)
    except Exception as e:
        print(f"  Nudge: program load failed: {e}")
        return

    current_week = program_data.get("current_week", {})
    today_str = today.strftime("%A")  # e.g. "Monday"
    today_sessions = []
    for day in current_week.get("days", []):
        day_label = day.get("label", "")
        # Match sessions scheduled for today (rough day-of-week match)
        if today_str.lower() in day_label.lower():
            today_sessions.append(day)

    if not today_sessions:
        print("  Nudge: no session scheduled today — skipping.")
        return

    # Check if any session is incomplete (no done=True exercises)
    any_complete = False
    for day in today_sessions:
        done_count = sum(1 for ex in day.get("exercises", []) if ex.get("done"))
        if done_count > 0:
            any_complete = True
            break

    if any_complete:
        print("  Nudge: session already logged today — skipping.")
        return

    # Dedup: check if we already sent a nudge today
    coach_state = read_coach_state()
    last_nudge = coach_state.get("LAST_NUDGE", {}).get("summary", "")
    if last_nudge == str(today):
        print(f"  Nudge: already sent today ({today}) — skipping.")
        return

    # Build nudge message
    session_labels = [day.get("label", "session") for day in today_sessions]
    session_str = " + ".join(session_labels)
    nudge_msg = (
        f"Hey — {session_str} is still unlogged. "
        f"Did you train today? If yes, just tell me what you did. "
        f"If you skipped, that's fine — just let me know so I can track it."
    )

    if dry_run:
        print(f"  [DRY RUN] Would send nudge: {nudge_msg}")
        return

    try:
        from telegram_utils import send_telegram_message
        sent = send_telegram_message(nudge_msg)
        if sent:
            print(f"  Nudge sent: {nudge_msg[:80]}")
            upsert_coach_state("LAST_NUDGE", str(today), "HIGH")
    except Exception as e:
        print(f"  Nudge send failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Sunday schedule discovery
# ---------------------------------------------------------------------------

def run_weekly_schedule_discovery(dry_run: bool = False):
    """
    Sunday morning: send a Telegram message asking about this week's training schedule.
    References the known WEEKLY_SCHEDULE pattern from Coach State.
    The reply comes through the live Railway bot, which updates WEEKLY_SCHEDULE via the processor.
    Deduped via LAST_SCHEDULE_DISCOVERY Coach State domain (one per Sunday).
    """
    from sheets import read_program_data
    from memory import read_coach_state, upsert_coach_state

    today = date.today()
    week_num = compute_current_week(resolve_program_start_date())
    print(f"[{today}] Checking weekly schedule discovery (Week {week_num})...")

    # Dedup: only send once per rolling 6-day window
    # (Normally runs Sunday morning; falls back to next proactive pass if missed)
    coach_state = read_coach_state()
    last_disc_str = coach_state.get("LAST_SCHEDULE_DISCOVERY", {}).get("summary", "")
    if last_disc_str:
        try:
            last_disc = date.fromisoformat(last_disc_str[:10])
            if (today - last_disc).days < 6:
                print(f"  Schedule discovery: done {last_disc_str[:10]} ({(today - last_disc).days}d ago) — skipping.")
                return
        except (ValueError, TypeError):
            pass

    # Load this week's remaining sessions (not next week — we're mid-week if not Sunday)
    is_sunday = today.weekday() == 6
    try:
        week_data = read_program_data(week_num=week_num, lookback=0)
        days = week_data.get("current_week", {}).get("days", [])
        # Show only sessions not yet done
        remaining_days = [d for d in days if not any(ex.get("done") is True for ex in d.get("exercises", []))]
        session_labels = [d.get("label", f"Day {i+1}") for i, d in enumerate(remaining_days)]
    except Exception:
        session_labels = []

    known_schedule = coach_state.get("WEEKLY_SCHEDULE", {}).get("summary", "")
    sessions_text = ", ".join(session_labels[:4]) if session_labels else "see program"
    days_left = 7 - today.weekday() if not is_sunday else 7
    week_context = "new week" if is_sunday else f"week already started ({days_left} days left incl. today)"

    # Build message via Haiku
    if known_schedule:
        prompt = (
            f"You are {ATHLETE_NAME}'s coach. It's {today.strftime('%A')} — {week_context}. "
            f"Write a short Telegram message (2-3 sentences) asking him to confirm or update his "
            f"training plan for the remaining days. Reference the known pattern but stay open — things change.\n\n"
            f"Known schedule pattern: {known_schedule}\n"
            f"Remaining sessions this week: {sessions_text}\n\n"
            f"Style: direct, specific. Mention what you know. Ask if it still works, or if anything "
            f"changes (travel, work, energy). No greeting, no sign-off.\n"
            f"Write it now:"
        )
    else:
        prompt = (
            f"You are {ATHLETE_NAME}'s coach. It's {today.strftime('%A')} — {week_context}. "
            f"Write a short Telegram message (2-3 sentences) asking when he plans to train the rest of this week. "
            f"You don't have a stored schedule yet, so ask openly.\n\n"
            f"Remaining sessions this week: {sessions_text}\n\n"
            f"Style: direct. Explain you want to plan around his actual availability. "
            f"Ask which days work and roughly what time. No greeting, no sign-off.\n"
            f"Write it now:"
        )

    if dry_run:
        print(f"  [DRY RUN] Schedule discovery (known_schedule={bool(known_schedule)})")
        print(f"  [DRY RUN] Prompt: {prompt[:200]}")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = result.content[0].text.strip()

        from telegram_utils import send_telegram_message
        sent = send_telegram_message(msg)
        if sent:
            print(f"  Schedule discovery sent: {msg[:80]}")
            upsert_coach_state("LAST_SCHEDULE_DISCOVERY", str(today), "HIGH")
        else:
            print("  Schedule discovery: Telegram send failed.")
    except Exception as e:
        print(f"  Schedule discovery failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Evening protocol: plan check + TOMORROW_PLAN for bot to use
# ---------------------------------------------------------------------------

def _is_on_vacation(life_context: list[dict]) -> bool:
    """
    Return True if the most recent vacation-related Life Context entry indicates
    an ACTIVE vacation (not a return announcement or a stale mention).

    Logic (newest-first):
    1. If the entry has a return signal ("back from", "returned", etc.) → False
    2. If the entry is > 14 days old → treat as stale/expired → False
    3. Otherwise → True (active vacation)

    This prevents both "back from vacation" and old entries from triggering vacation mode.
    """
    vacation_keywords = ("vacation", "holiday", "vacaciones", "holidays", "de vacaciones")
    return_keywords = ("back from", "returned from", "back home", "de vuelta",
                       "regresé", "already back", "got back", "came back", "just back",
                       "back to training", "resuming", "volviendo")

    today = date.today()

    for entry in reversed(life_context[-10:]):
        text = entry.get("context", "").lower()
        if not any(k in text for k in vacation_keywords):
            continue
        # Found the most recent vacation mention — check for return signal first
        if any(k in text for k in return_keywords):
            return False  # "back from vacation" → vacation over
        # Check age — vacations don't last forever without an explicit update
        entry_date_str = entry.get("date", "")
        if entry_date_str:
            try:
                entry_date = date.fromisoformat(entry_date_str[:10])
                if (today - entry_date).days > 14:
                    return False  # stale mention (>14 days) → treat as inactive
            except (ValueError, TypeError):
                pass
        return True  # recent vacation mention without return signal → active

    return False


def run_evening_protocol(dry_run: bool = False):
    """
    Evening protocol (19:00 UTC = 20:00 Spain).

    Sends ONE Telegram message asking if tomorrow's plan is still on — naming the session explicitly.
    The full protocol (Message 2) is generated by the live Railway bot in response to the user's reply.

    Also writes TOMORROW_PLAN to Coach State so the bot and morning brief have context.
    Deduped via LAST_EVENING_PROTOCOL Coach State domain.
    """
    from sheets import read_program_data
    from memory import read_coach_state, upsert_coach_state, read_life_context

    today = date.today()
    tomorrow = today + timedelta(days=1)
    week_num = compute_current_week(resolve_program_start_date())
    print(f"[{today}] Running evening protocol (Week {week_num})...")

    # Dedup: only send once per day
    coach_state = read_coach_state()
    last_evening = coach_state.get("LAST_EVENING_PROTOCOL", {}).get("summary", "")
    if last_evening == str(today):
        print("  Evening protocol: already sent today — skipping.")
        return

    # Vacation check
    try:
        life_context = read_life_context(limit=10)
        if _is_on_vacation(life_context):
            vacation_msg = (
                "You're on vacation — enjoy it. Try to walk 30-45 min if you feel like it. "
                "Eat well tonight (protein + vegetables). Check in when you're back and we'll get going again."
            )
            if dry_run:
                print(f"  [DRY RUN] Vacation mode: {vacation_msg}")
                return
            from telegram_utils import send_telegram_message
            if send_telegram_message(vacation_msg):
                print("  Vacation message sent.")
                upsert_coach_state("LAST_EVENING_PROTOCOL", str(today), "HIGH")
            return
    except Exception as e:
        print(f"  Vacation check failed (non-fatal): {e}")

    # Load program data
    try:
        program_data = read_program_data(week_num=week_num, lookback=0)
    except Exception as e:
        print(f"  Evening protocol: program load failed: {e}")
        return

    current_week = program_data.get("current_week", {})
    days = current_week.get("days", [])

    # Find next incomplete session (first day where no exercises are marked done=True)
    next_session = None
    for day in days:
        exercises = day.get("exercises", [])
        done_count = sum(1 for ex in exercises if ex.get("done") is True)
        if done_count == 0 and exercises:
            next_session = day
            break

    # If no incomplete session in current week, try next week
    if not next_session and tomorrow.weekday() == 0:  # Sunday → check next week
        try:
            next_week_data = read_program_data(week_num=week_num + 1, lookback=0)
            next_week_days = next_week_data.get("current_week", {}).get("days", [])
            if next_week_days:
                next_session = next_week_days[0]
        except Exception:
            pass

    if not next_session:
        print("  Evening protocol: all sessions complete or no upcoming session — skipping.")
        return  # Don't write dedup — will try again next day

    # Build session summary for the message
    label = next_session.get("label", "next session")
    exercises = next_session.get("exercises", [])
    main_lifts = [ex for ex in exercises if ex.get("weight")][:3]
    lift_summary = ", ".join(
        f"{ex.get('name', '?')} {ex.get('weight', '')} {ex.get('sets_reps', '')}".strip()
        for ex in main_lifts
    )

    # Pull weekly schedule context
    schedule_ctx = coach_state.get("WEEKLY_SCHEDULE", {}).get("summary", "")

    prompt = (
        f"You are {ATHLETE_NAME}'s strength coach. Write ONE short Telegram message (1-2 sentences) "
        f"asking if tomorrow's training session is still on. Name the session and its key lifts specifically. "
        f"Be direct — no greeting, no sign-off.\n\n"
        f"Tomorrow's session: {label}\n"
        f"Key lifts: {lift_summary or 'see program'}\n"
        f"Known schedule pattern: {schedule_ctx or 'not yet established'}\n\n"
        f"Example style: \"Tomorrow is Day 3 — squat 102.5kg 5×5, bench 80kg 4×6. Still on?\"\n"
        f"Write it now:"
    )

    # Store tomorrow's plan for the bot and brief to reference
    tomorrow_plan_summary = f"{label} | {lift_summary}" if lift_summary else label

    if dry_run:
        print(f"  [DRY RUN] Evening protocol for: {tomorrow_plan_summary}")
        print(f"  [DRY RUN] Prompt: {prompt[:200]}")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = result.content[0].text.strip()

        from telegram_utils import send_telegram_message
        sent = send_telegram_message(msg)
        if sent:
            print(f"  Evening protocol sent: {msg[:80]}")
            upsert_coach_state("LAST_EVENING_PROTOCOL", str(today), "HIGH")
            upsert_coach_state("TOMORROW_PLAN", tomorrow_plan_summary, "HIGH")
        else:
            print("  Evening protocol: Telegram send failed.")
    except Exception as e:
        print(f"  Evening protocol failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------

def run_export(output_file: str = None, dry_run: bool = False):
    """
    Export all Coach Memory tabs to JSON.
    Writes to output_file if provided, otherwise prints to stdout.
    Safe read-only operation — no writes to sheets.
    """
    import json
    from memory import read_all, read_lift_history, read_health_log, read_telegram_log

    today = date.today()
    print(f"[{today}] Exporting Coach Memory...")

    # Tier 1 + Tier 0 combined
    data = read_all()
    data["lift_history_full"] = read_lift_history(limit=1000)
    data["health_log_full"] = read_health_log(limit=365)
    data["telegram_log_full"] = read_telegram_log(limit=500)

    export = {
        "exported_at": str(today),
        "data": {k: v for k, v in data.items()},
    }

    json_str = json.dumps(export, indent=2, default=str)

    if dry_run or not output_file:
        print(f"Export: {len(json_str)} bytes | {sum(len(v) if isinstance(v, list) else 1 for v in data.values())} records")
        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(json_str)
            print(f"  Exported to: {output_file}")
        else:
            print(json_str[:2000] + ("..." if len(json_str) > 2000 else ""))
    else:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(json_str)
        print(f"  Exported {len(json_str)} bytes → {output_file}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()

    if args.setup:
        from memory import setup_memory_sheet
        print("Setting up Coach Memory Sheet...")
        setup_memory_sheet()
        sys.exit(0)

    week_num = args.week or None

    try:
        if args.think:
            run_think(week_num=week_num, dry_run=args.dry_run)
        elif args.proactive:
            run_proactive(dry_run=args.dry_run)
        elif args.nudge:
            run_nudge(dry_run=args.dry_run)
        elif args.brief:
            run_brief(dry_run=args.dry_run)
        elif getattr(args, "post_session", False):
            run_post_session(dry_run=args.dry_run)
        elif getattr(args, "evening_protocol", False):
            run_evening_protocol(dry_run=args.dry_run)
        elif getattr(args, "weekly_schedule", False):
            run_weekly_schedule_discovery(dry_run=args.dry_run)
        elif getattr(args, "steer_co_finalize", False):
            from planner import run_steer_co_finalize
            run_steer_co_finalize(dry_run=args.dry_run)
        elif args.export:
            from datetime import datetime as _dt
            fname = f"coach_export_{_dt.today().strftime('%Y%m%d')}.json"
            run_export(output_file=fname, dry_run=args.dry_run)
        else:
            run(
                week_num=week_num,
                dry_run=args.dry_run,
                no_sync=args.no_sync,
                force_weekly=args.weekly,
            )
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        raise
