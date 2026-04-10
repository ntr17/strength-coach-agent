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

from config import ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL, CLAUDE_HAIKU, KEY_LIFTS, compute_current_week, resolve_program_start_date
CLAUDE_SONNET = CLAUDE_MODEL  # alias for clarity in bootstrap/cascade calls


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
    parser.add_argument("--meta", action="store_true",
                        help="Coach self-critique: analyse coaching quality + surface improvements via Telegram.")
    parser.add_argument("--init", action="store_true",
                        help="V17: Start or resume the Iteration 0 initialization interview via Telegram.")
    parser.add_argument("--close-day", action="store_true",
                        help="V17: Daily closing pass — classify day, produce DAILY_SUMMARY, check escalation thresholds.")
    parser.add_argument("--weekly-eval", action="store_true",
                        help="V17: Weekly evaluation — produce WEEKLY_SUMMARY from daily summaries.")
    parser.add_argument("--monthly-eval", action="store_true",
                        help="V17: Monthly evaluation — produce MONTHLY_SUMMARY from weekly summaries.")
    parser.add_argument("--annual-eval", action="store_true",
                        help="V17: Annual evaluation — produce ANNUAL_SUMMARY from monthly summaries.")
    parser.add_argument("--longterm-eval", action="store_true",
                        help="V17: Long-term evaluation (quarterly) — produce LONGTERM_PLAN from annual summary.")
    parser.add_argument("--sync-garmin", action="store_true",
                        help="Fetch Garmin recovery data for last 14 days → Health Log. No email, no Telegram.")
    parser.add_argument("--sync-sheet", action="store_true",
                        help="Run sheet delta sync: detect changes, update Coach State, resolve Commands. No email, no Telegram.")
    parser.add_argument("--bootstrap", action="store_true",
                        help="V17: Bootstrap cascade — synthesize WEEKLY/MONTHLY summaries from all available history, then run annual+longterm eval.")
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
                model=CLAUDE_HAIKU,
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
                # FOLLOWUP items also create a dated OPEN_QUESTION command.
                # This anchors when the question was asked and what context prompted it —
                # so the coach can say "I asked you about this on Tuesday after Day 2" not just "I asked".
                if category == "FOLLOWUP":
                    try:
                        from memory import append_command
                        append_command("OPEN_QUESTION", f"[asked {today}] {item}")
                    except Exception:
                        pass
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

        # --- PROJECTION SNAPSHOT (weekly only) — store for next week's review loop ---
        if is_weekly_summary and projections:
            try:
                import json as _json
                snap_parts = []
                for proj in projections.get("lift_projections", []):
                    if proj:
                        snap_parts.append({
                            "exercise": proj.get("exercise", ""),
                            "current_1rm": proj.get("current_1rm"),
                            "rate_per_week": proj.get("rate_per_week"),
                            "projected_end_1rm": proj.get("projected_end_1rm"),
                            "on_track": proj.get("on_track"),
                            "target_1rm": proj.get("target_1rm"),
                        })
                if snap_parts:
                    snap_json = _json.dumps({
                        "date": str(date.today()),
                        "week_num": week_num,
                        "lifts": snap_parts,
                    })
                    _write_state(upsert_coach_state, "LAST_PROJECTION_SNAPSHOT",
                                 snap_json, "HIGH", dry_run)
            except Exception as e:
                print(f"  Projection snapshot failed (non-fatal): {e}")

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


def extract_schedule_markers(text: str) -> tuple[str, list[dict]]:
    """
    Extract [SCHEDULE: YYYY-MM-DD | message text] markers from coach output.
    Returns (clean_text, list of {target_date, message} dicts).

    These allow the coach to schedule a specific Telegram message for a future date.
    Example: [SCHEDULE: 2026-03-20 | How's the elbow feeling after this week's pull volume?]
    """
    import re
    schedules = []
    pattern = r'\[SCHEDULE:\s*(.*?)\]'
    for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
        raw = match.group(1).strip()
        if " | " in raw:
            parts = raw.split(" | ", 1)
            target_date = parts[0].strip()
            message = parts[1].strip()
            if message:
                schedules.append({"target_date": target_date, "message": message})
    clean = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL).strip()
    return clean, schedules


def run_scheduled_messages(dry_run: bool = False) -> int:
    """
    Check the Commands tab for SCHEDULED_MESSAGE entries due today or overdue.
    For each one, send the Telegram message and mark it applied.
    Returns number of messages sent.

    Messages are stored as:  SCHEDULED_MESSAGE | YYYY-MM-DD | message text
    Written by extract_schedule_markers() when coach emits [SCHEDULE: date | msg].
    """
    from memory import read_commands
    today_str = str(date.today())
    commands = read_commands() if not dry_run else []
    sent_count = 0

    for cmd in commands:
        if cmd.get("Command", "").upper() != "SCHEDULED_MESSAGE":
            continue
        if cmd.get("Applied", "").upper() in ("Y", "DECLINED"):
            continue
        # Value format: "YYYY-MM-DD | message text"
        value = cmd.get("Value", "")
        if " | " not in value:
            continue
        parts = value.split(" | ", 1)
        target_date = parts[0].strip()[:10]
        message = parts[1].strip()
        if not message or target_date > today_str:
            continue  # not due yet

        if dry_run:
            print(f"  [DRY RUN] Would fire scheduled message for {target_date}: {message[:80]}")
            continue

        try:
            from telegram_utils import send_telegram_message
            sent = send_telegram_message(message)
            if sent:
                print(f"  [SCHEDULED_MESSAGE] Sent (due {target_date}): {message[:80]}")
                # Mark applied
                try:
                    from memory import update_command_applied
                    update_command_applied(cmd.get("_row_index"), "Y")
                except Exception:
                    pass
                sent_count += 1
        except Exception as e:
            print(f"  [SCHEDULED_MESSAGE] Send failed (non-fatal): {e}")

    return sent_count


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

    # 2b. Update BEHAVIOR_PATTERNS — weekly behavioral analysis (pure Python, no LLM)
    print("  Updating behavior patterns...")
    _update_behavior_patterns(memory_data, program_data=program_data, dry_run=dry_run)

    # 2c. Archive stale Coach Focus items (NORMAL priority, no activity in 90+ days)
    if not dry_run:
        try:
            from memory import read_coach_focus, update_coach_focus_status
            focus_items = read_coach_focus()
            cutoff_focus = str(today - timedelta(days=90))
            archived_focus = 0
            for item in focus_items:
                if item.get("Status", "OPEN") != "OPEN":
                    continue
                if item.get("Priority", "NORMAL").upper() in ("HIGH", "PINNED"):
                    continue
                timestamp = item.get("Last Mentioned", "") or item.get("Date Added", "")
                if timestamp and timestamp[:10] < cutoff_focus:
                    update_coach_focus_status(item.get("Item", "")[:80], "STALE")
                    archived_focus += 1
            if archived_focus:
                print(f"  Archived {archived_focus} stale Coach Focus items (>90d NORMAL).")
        except Exception as e:
            print(f"  Coach Focus archival failed (non-fatal): {e}")

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

    # 6. Program terminal summary — if ending within 1 week, write institutional memory
    try:
        from projections import run_all_projections
        proj_check = run_all_projections(memory_data, program_data=program_data)
        pp = proj_check.get("program_projection") or {}
        if pp.get("weeks_remaining", 99) <= 1:
            print("  Program ending — writing terminal summary...")
            _write_program_terminal_summary(memory_data, dry_run=dry_run)
    except Exception as e:
        print(f"  Terminal summary check failed (non-fatal): {e}")

    # 7. Bi-monthly steer co — initiate if ~60 days since last one
    from planner import _initiate_steer_co
    memory_data = read_all()  # re-read for fresh Coach State
    _initiate_steer_co(memory_data, dry_run=dry_run)

    # 8. V17 weekly health science pass — correlate sleep/HRV with training (pure Python)
    try:
        from health_science import run_weekly_health_science
        from memory import read_lift_history, read_health_log
        print("  Running weekly health science correlations...")
        run_weekly_health_science(
            health_log=read_health_log(limit=200),
            lift_history=read_lift_history(limit=500),
            dry_run=dry_run,
        )
    except Exception as e:
        print(f"  Weekly health science failed (non-fatal): {e}")

    # 9. V17 strength tracker — e1RM, stalls, volume buckets, push/pull balance
    try:
        from strength_tracker import run_weekly_strength_report
        from memory import read_lift_history as _rlifth
        print("  Running weekly strength analytics...")
        _st_lift_history = _rlifth(limit=500)
        _st_goals_text = memory_data.get("long_term_goals", "") if memory_data else ""
        _st_goals: dict = {}
        import re as _re_st
        for _pat, _lift in [
            (r"(\d+)\s*kg\s+squat", "squat"),
            (r"(\d+)\s*kg\s+bench", "bench press"),
            (r"(\d+)\s*kg\s+deadlift", "deadlift"),
            (r"(\d+)\s*kg\s+ohp", "overhead press"),
        ]:
            _m = _re_st.search(_pat, _st_goals_text, _re_st.IGNORECASE)
            if _m:
                try:
                    _st_goals[_lift] = float(_m.group(1))
                except (ValueError, IndexError):
                    pass
        run_weekly_strength_report(_st_lift_history, goals=_st_goals, dry_run=dry_run)
    except Exception as e:
        print(f"  Weekly strength analytics failed (non-fatal): {e}")

    # 10. V17 cardio zones analysis — runs weekly if Garmin is available
    try:
        from cardio_zones import CardioOrchestrator
        print("  Running cardio zone analysis...")
        CardioOrchestrator().run_cardio_analysis(days=21, dry_run=dry_run)
    except Exception as e:
        print(f"  Cardio zone analysis failed (non-fatal): {e}")

    # 11. V17 annual eval — runs on first Sunday of each month (monthly gate)
    try:
        if today.day <= 7:  # first week of month = run annual eval
            print("  First week of month — running annual_eval()...")
            from cascade_levels import annual_eval
            annual_eval(dry_run=dry_run)
    except Exception as e:
        print(f"  Annual eval failed (non-fatal): {e}")


def _write_program_terminal_summary(memory_data: dict, dry_run: bool = False) -> None:
    """
    Write a terminal program summary to Program History when the program is ending
    (weeks_remaining <= 1). Called from run_think().

    Captures: final lift numbers vs targets, key behavioral patterns, what worked,
    what didn't, and a recommendation for next program. Prevents cross-program amnesia.
    Uses Haiku — one-time cost, high long-term value.
    """
    from memory import read_coach_state, upsert_coach_state

    coach_state = memory_data.get("coach_state") or read_coach_state()

    # Only run once — gate on PROGRAM_TERMINAL_WRITTEN in Coach State
    terminal_flag = coach_state.get("PROGRAM_TERMINAL_WRITTEN", {}).get("summary", "")
    if terminal_flag and "written" in terminal_flag.lower():
        print("  Terminal summary: already written for this program — skipping.")
        return

    # Gather evidence
    lift_domains = ["SQUAT", "BENCH", "DEADLIFT", "OHP"]
    lift_states = {
        d: coach_state.get(d, {}).get("summary", "")
        for d in lift_domains
        if coach_state.get(d, {}).get("summary", "")
    }
    behavior_patterns = coach_state.get("BEHAVIOR_PATTERNS", {}).get("summary", "")
    athlete_model = coach_state.get("ATHLETE_MODEL", {}).get("summary", "")
    annual_arc = coach_state.get("ANNUAL_ARC", {}).get("summary", "")[:300]

    coach_log = memory_data.get("coach_log", [])
    log_snippets = "\n".join(
        f"  [{e.get('Date', '')}] {e.get('Key Observations', '')[:100]}"
        for e in coach_log[-10:]
    )

    lift_text = "\n".join(f"  {d}: {s}" for d, s in lift_states.items()) or "(no lift data)"

    prompt = (
        f"You are {ATHLETE_NAME}'s strength coach. This program is ending. Write a TERMINAL PROGRAM SUMMARY.\n\n"
        f"FINAL LIFT NUMBERS:\n{lift_text}\n\n"
        f"BEHAVIORAL PATTERNS THIS PROGRAM:\n  {behavior_patterns or '(none recorded)'}\n\n"
        f"PSYCHOLOGICAL MODEL:\n  {athlete_model or '(none recorded)'}\n\n"
        f"LONG-TERM ARC CONTEXT:\n  {annual_arc or '(none)'}\n\n"
        f"RECENT COACH LOG:\n{log_snippets or '  (none)'}\n\n"
        f"Write 6-8 sentences covering:\n"
        f"1. Final numbers vs targets — honest assessment of what was hit, what was missed\n"
        f"2. The 2-3 patterns that defined this program (training behavior, adherence, RPE tendencies)\n"
        f"3. What worked well — what should be carried forward to the next program\n"
        f"4. What didn't work — what should be changed or avoided\n"
        f"5. One concrete recommendation for the next program (volume, frequency, focus)\n"
        f"Be direct. This is your institutional memory. Future you will read this before designing the next block."
    )

    if dry_run:
        print("  [DRY RUN] Would write program terminal summary.")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        summary_text = result.content[0].text.strip()

        # Write to Program History via coach log (permanent record)
        try:
            from memory import log_coach_run
            log_coach_run(
                observations=f"[PROGRAM TERMINAL SUMMARY] {summary_text[:300]}",
                email_summary=summary_text,
            )
            print("  Terminal summary written to Coach Log.")
        except Exception as e:
            print(f"  Terminal summary log write failed (non-fatal): {e}")

        # Also write to Coach State as a domain for immediate access
        upsert_coach_state("PROGRAM_TERMINAL", summary_text[:600], "HIGH")
        upsert_coach_state("PROGRAM_TERMINAL_WRITTEN", "written", "HIGH")
        print(f"  PROGRAM_TERMINAL Coach State written ({len(summary_text)} chars).")
    except Exception as e:
        print(f"  Program terminal summary failed (non-fatal): {e}")


def _update_athlete_model(memory_data: dict, dry_run: bool = False) -> None:
    """
    Update the ATHLETE_MODEL Coach State domain — the coach's psychological model
    of the athlete. Called quarterly (via run_think). Budget-conscious: Haiku, max 400 tokens.
    Captures: response to feedback, psychological patterns, known weaknesses, what motivates.
    """
    from memory import read_coach_state, upsert_coach_state

    coach_state = memory_data.get("coach_state") or read_coach_state()
    _am_entry = coach_state.get("ATHLETE_MODEL", {})
    existing_model = _am_entry.get("summary", "")
    last_updated = _am_entry.get("last_updated", "")
    existing_confidence = _am_entry.get("confidence", _am_entry.get("Confidence", "")).upper()

    # Guard: do not overwrite HIGH confidence data (e.g. from iteration_zero) with MEDIUM confidence
    if existing_model and existing_confidence == "HIGH":
        print("  ATHLETE_MODEL: existing HIGH confidence data — skipping MEDIUM confidence overwrite.")
        return

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
    commands = memory_data.get("commands", [])

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

    # Coaching decisions: proposals accepted, declined, or ignored — reveals athlete's response pattern
    decision_snippets = []
    for cmd in commands[-30:]:
        cmd_type = cmd.get("Command", "").upper()
        applied = cmd.get("Applied", "").upper()
        value = cmd.get("Value", "")[:100]
        if cmd_type == "PENDING_PROPOSAL":
            if applied == "Y":
                decision_snippets.append(f"  [ACCEPTED] {value}")
            elif applied == "DECLINED":
                decision_snippets.append(f"  [DECLINED] {value}")
            elif applied not in ("Y", "DECLINED") and value:
                decision_snippets.append(f"  [IGNORED/PENDING] {value}")
    decisions_text = "\n".join(decision_snippets[-10:]) or "  (no proposal history yet)"

    # Resolved follow-ups — what the coach flagged and the athlete actually addressed
    resolved = "\n".join(
        f"  [RESOLVED] {f.get('Item', '')[:100]}"
        for f in focus
        if f.get("Status", "") == "RESOLVED"
    )[-500:] or "  (none)"

    prompt = (
        f"Based on coaching history, build a psychological model of this athlete.\n\n"
        f"ATHLETE PROFILE:\n{profile[:400]}\n\n"
        f"RECENT COACH OBSERVATIONS:\n{log_snippets}\n\n"
        f"CURRENT WATCH LIST:\n{focus_snippets}\n\n"
        f"COACHING DECISIONS (what was proposed, and how the athlete responded):\n{decisions_text}\n\n"
        f"WHAT THE ATHLETE ACTUALLY RESOLVED (vs what stayed open):\n{resolved}\n\n"
        f"LAST PLANNING NOTES:\n{plan_snippet}\n\n"
        f"EXISTING MODEL (update, don't discard):\n{existing_model or '(none yet)'}\n\n"
        f"Output 5-7 sentences covering:\n"
        f"1. How the athlete responds to direct feedback vs indirect suggestion\n"
        f"2. Known psychological patterns: avoidance, excuses, motivation triggers\n"
        f"3. Response to proposals — does he accept, push back, or ignore? Is there a pattern?\n"
        f"4. What coaching approach works best for him specifically\n"
        f"5. Persistent weaknesses the coach should keep returning to (even if athlete resists)\n"
        f"Be honest. This is your institutional memory. It only helps if it's accurate, not flattering."
    )

    if dry_run:
        print("  [DRY RUN] Would update ATHLETE_MODEL Coach State.")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        model_text = result.content[0].text.strip()
        upsert_coach_state("ATHLETE_MODEL", model_text, "MEDIUM")
        print(f"  ATHLETE_MODEL Coach State written ({len(model_text)} chars).")
    except Exception as e:
        print(f"  ATHLETE_MODEL update failed (non-fatal): {e}")


def _update_behavior_patterns(memory_data: dict, program_data: dict = None,
                              dry_run: bool = False) -> None:
    """
    Accumulate behavioral patterns into BEHAVIOR_PATTERNS Coach State domain.
    Called weekly from run_think. Pure Python analysis — no LLM calls.

    Detects:
    - Skip patterns per exercise (which exercises the athlete tends to skip)
    - Weight deviation tendency (consistently above/below program)
    - Session completion rate trend (improving/declining over 4 weeks)
    - RPE patterns per lift (chronically high/low)
    - Day-of-week performance (which days tend to be missed)
    """
    from memory import upsert_coach_state, read_lift_history

    lift_history = memory_data.get("lift_history") or []
    if not lift_history:
        try:
            lift_history = read_lift_history(limit=60)
        except Exception:
            pass

    patterns = []

    # --- Skip patterns: exercises skipped ≥3 times in last 4 weeks ---
    skip_counts: dict[str, int] = {}
    done_counts: dict[str, int] = {}

    if program_data:
        recent_weeks = program_data.get("recent_weeks", [])
        current = program_data.get("current_week", {})
        all_weeks = recent_weeks[-3:] + ([current] if current else [])
        for week in all_weeks:
            for day in week.get("days", []):
                for ex in day.get("exercises", []):
                    name = (ex.get("name") or "").strip()
                    if not name:
                        continue
                    if ex.get("done") is False:
                        skip_counts[name] = skip_counts.get(name, 0) + 1
                    elif ex.get("done") is True:
                        done_counts[name] = done_counts.get(name, 0) + 1

        for ex_name, skips in skip_counts.items():
            total = skips + done_counts.get(ex_name, 0)
            if total >= 3 and skips / total >= 0.5:
                patterns.append(f"SKIP_PATTERN: {ex_name} skipped {skips}/{total} sessions")

    # --- Weight deviation tendency from lift history ---
    # Look for cases where actual weight consistently differs from last logged weight
    # We proxy this by checking Lift History entries for "(+X)" or "(-X)" notes
    over_count = 0
    under_count = 0
    import re as _re_inner
    for row in lift_history[-40:]:
        notes = (row.get("Notes") or "").lower()
        if _re_inner.search(r"\+\d+.*kg|went heavier|added weight|increased", notes):
            over_count += 1
        elif _re_inner.search(r"-\d+.*kg|went lighter|dropped weight|reduced", notes):
            under_count += 1

    if over_count >= 4 and over_count > under_count * 2:
        patterns.append(f"WEIGHT_TENDENCY: consistently trains above program ({over_count} instances) — consider proactive weight bumps")
    elif under_count >= 4 and under_count > over_count * 2:
        patterns.append(f"WEIGHT_TENDENCY: consistently trains below program ({under_count} instances) — may indicate recovery issues or low confidence")

    # --- RPE patterns per lift from lift history ---
    rpe_by_lift: dict[str, list[float]] = {}
    for row in lift_history[-40:]:
        ex = (row.get("Exercise") or "").strip()
        if not ex:
            continue
        notes = (row.get("Notes") or "").strip()
        m = _re_inner.search(r"@?RPE\s*(\d+(?:\.\d+)?)", notes, _re_inner.IGNORECASE)
        if m:
            try:
                rpe_by_lift.setdefault(ex, []).append(float(m.group(1)))
            except (ValueError, TypeError):
                pass

    for lift_name, rpe_vals in rpe_by_lift.items():
        if len(rpe_vals) < 3:
            continue
        avg = sum(rpe_vals[-5:]) / len(rpe_vals[-5:])
        if avg >= 8.5:
            patterns.append(f"RPE_PATTERN: {lift_name} avg RPE {avg:.1f} (last {min(5, len(rpe_vals))} sessions) — chronically heavy")
        elif avg <= 5.5:
            patterns.append(f"RPE_PATTERN: {lift_name} avg RPE {avg:.1f} — chronically light, may need progressive bump")

    # --- Day-of-week miss patterns from program data ---
    if program_data:
        day_misses: dict[str, int] = {}
        day_totals: dict[str, int] = {}
        all_weeks_for_dow = (program_data.get("recent_weeks", [])[-4:]
                             + ([program_data.get("current_week", {})] if program_data.get("current_week") else []))
        for week in all_weeks_for_dow:
            for day in week.get("days", []):
                label = day.get("label", "").split()[0].capitalize() if day.get("label") else ""
                if not label:
                    continue
                day_totals[label] = day_totals.get(label, 0) + 1
                any_done = any(ex.get("done") is True for ex in day.get("exercises", []))
                if not any_done:
                    day_misses[label] = day_misses.get(label, 0) + 1

        for day_name, misses in day_misses.items():
            total = day_totals.get(day_name, 1)
            if total >= 3 and misses / total >= 0.6:
                patterns.append(f"DAY_PATTERN: {day_name} sessions missed {misses}/{total} times — likely schedule conflict")

    if not patterns:
        patterns.append("No significant behavioral patterns detected yet (need 3+ weeks of data)")

    summary = " | ".join(patterns[:8])  # cap at 8 patterns to keep it concise

    if dry_run:
        print(f"  [DRY RUN] BEHAVIOR_PATTERNS: {summary[:120]}")
        return

    try:
        upsert_coach_state("BEHAVIOR_PATTERNS", summary, "MEDIUM")
        print(f"  BEHAVIOR_PATTERNS updated: {summary[:100]}")
    except Exception as e:
        print(f"  BEHAVIOR_PATTERNS update failed (non-fatal): {e}")


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


def _detect_session_delta(program_data: dict, coach_state: dict) -> dict:
    """
    Compare current program sheet state against LAST_PROGRAM_SNAPSHOT stored in Coach State.
    Returns a rich delta dict describing what changed since the last check.

    The snapshot now stores per-exercise: {done, actual, notes} so we can detect:
      - New sessions logged since last check
      - Exercises skipped within a partially-logged session
      - Actual weight vs prescribed weight (deviations — up or down)
      - New session notes added
      - Missing RPE in completed sessions
      - Footer health data changes (bodyweight, sleep, energy)

    Delta keys:
      new_sessions_done:   list of {label, exercises_done, exercises_total, skipped_names,
                                    has_rpe, weight_deviations, notable_notes, completion_pct}
      newly_skipped:       list of exercise names that went done→skipped
      new_health_data:     dict of {bodyweight, sleep, energy} if new footer data detected
      no_rpe_sessions:     list of session labels completed without any RPE data
      snapshot_updated:    bool (caller should write new snapshot)
    """
    import json as _json
    import re as _rej

    current_week = program_data.get("current_week", {})
    days = current_week.get("days", [])

    # Build current snapshot — richer: {day_label: {exercise_name: {done, actual, notes}}}
    current_snapshot: dict = {}
    for day in days:
        label = day.get("label", "")
        day_snap = {}
        for i, ex in enumerate(day.get("exercises", [])):
            name = ex.get("name") or f"ex{i}"
            day_snap[name] = {
                "done": ex.get("done", False),
                "actual": (ex.get("actual") or "").strip(),
                "notes": (ex.get("session_note") or ex.get("notes") or "").strip(),
                "weight": (ex.get("weight") or "").strip(),
            }
        # Footer: bodyweight, sleep, energy (if present)
        footer = day.get("footer", {})
        if footer:
            day_snap["__footer__"] = {
                "bodyweight": str(footer.get("bodyweight", "")),
                "sleep": str(footer.get("sleep", "")),
                "energy": str(footer.get("energy", "")),
            }
        current_snapshot[label] = day_snap

    # Also capture week-level footer if present
    week_footer = current_week.get("footer", {})
    if week_footer:
        current_snapshot["__week_footer__"] = {
            "bodyweight": str(week_footer.get("bodyweight", "")),
            "sleep": str(week_footer.get("sleep", "")),
            "energy": str(week_footer.get("energy", "")),
        }

    # Load previous snapshot
    prev_raw = coach_state.get("LAST_PROGRAM_SNAPSHOT", {}).get("summary", "")
    prev_snapshot: dict = {}
    if prev_raw:
        try:
            prev_snapshot = _json.loads(prev_raw)
        except (ValueError, TypeError):
            pass

    new_sessions_done: list[dict] = []
    newly_skipped: list[str] = []
    no_rpe_sessions: list[str] = []
    new_health_data: dict = {}

    # Check for new footer health data
    for footer_key in ("__week_footer__",):
        curr_f = current_snapshot.get(footer_key, {})
        prev_f = prev_snapshot.get(footer_key, {})
        if curr_f:
            for field in ("bodyweight", "sleep", "energy"):
                cv = curr_f.get(field, "")
                pv = prev_f.get(field, "")
                if cv and cv != pv:
                    new_health_data[field] = cv

    for day in days:
        label = day.get("label", "")
        exercises = day.get("exercises", [])
        curr_day = current_snapshot.get(label, {})
        prev_day = prev_snapshot.get(label, {})

        done_exs = [ex for ex in exercises if ex.get("done") is True]
        skip_exs = [ex for ex in exercises if ex.get("done") is False and ex.get("name")]
        total_exs = len([ex for ex in exercises if ex.get("name")])

        # Previous done count (from snapshot — may have nested dict or bool)
        prev_done_count = 0
        for k, v in prev_day.items():
            if k.startswith("__"):
                continue
            if isinstance(v, dict) and v.get("done") is True:
                prev_done_count += 1
            elif v is True:
                prev_done_count += 1

        # Session newly logged (had zero done before, now has some)
        if done_exs and prev_done_count == 0:
            has_rpe = any(
                _re_rpe.search(curr_day.get(ex.get("name", ""), {}).get("notes", "")
                               if isinstance(curr_day.get(ex.get("name", ""), {}), dict)
                               else "")
                for ex in exercises
            )

            # Weight deviations: actual vs prescribed
            weight_deviations: list[str] = []
            for ex in done_exs:
                name = ex.get("name", "")
                if not name:
                    continue
                ex_snap = curr_day.get(name, {})
                if not isinstance(ex_snap, dict):
                    continue
                actual_raw = ex_snap.get("actual", "")
                prescribed_raw = ex_snap.get("weight", "")
                if actual_raw and prescribed_raw:
                    # Extract numbers to compare
                    actual_nums = _rej.findall(r"\d+(?:\.\d+)?", actual_raw)
                    prescribed_nums = _rej.findall(r"\d+(?:\.\d+)?", prescribed_raw)
                    if actual_nums and prescribed_nums:
                        try:
                            a_val = float(actual_nums[0])
                            p_val = float(prescribed_nums[0])
                            if abs(a_val - p_val) >= 2.5:
                                direction = "above" if a_val > p_val else "below"
                                weight_deviations.append(
                                    f"{name}: {a_val}kg ({direction} programmed {p_val}kg)"
                                )
                        except (ValueError, TypeError):
                            pass

            # Notable notes (non-empty)
            notable_notes: list[str] = []
            for ex in done_exs:
                name = ex.get("name", "")
                if not name:
                    continue
                ex_snap = curr_day.get(name, {})
                note = ex_snap.get("notes", "") if isinstance(ex_snap, dict) else ""
                if note and len(note) > 5:
                    notable_notes.append(f"{name}: {note[:80]}")

            completion_pct = round(len(done_exs) / total_exs * 100) if total_exs else 0

            new_sessions_done.append({
                "label": label,
                "exercises_done": len(done_exs),
                "exercises_total": total_exs,
                "completion_pct": completion_pct,
                "skipped_names": [ex.get("name", "") for ex in skip_exs if ex.get("name")],
                "has_rpe": has_rpe,
                "weight_deviations": weight_deviations,
                "notable_notes": notable_notes,
            })
            if not has_rpe:
                no_rpe_sessions.append(label)

        # Exercises that went done → not-done (unusual but track it)
        for ex in exercises:
            name = ex.get("name", "")
            if not name:
                continue
            prev_ex = prev_day.get(name)
            was_done = prev_ex.get("done") if isinstance(prev_ex, dict) else prev_ex
            is_done_now = ex.get("done")
            if was_done is True and is_done_now is False:
                newly_skipped.append(name)

    # Retroactive change detection: exercises already marked done but with different actual weights
    retroactive_changes: list[dict] = []
    for day in days:
        label = day.get("label", "")
        exercises = day.get("exercises", [])
        curr_day = current_snapshot.get(label, {})
        prev_day = prev_snapshot.get(label, {})

        for ex in exercises:
            if ex.get("done") is not True:
                continue
            name = ex.get("name", "")
            if not name:
                continue
            curr_ex = curr_day.get(name, {})
            prev_ex = prev_day.get(name, {})
            if not isinstance(curr_ex, dict) or not isinstance(prev_ex, dict):
                continue
            # Both must have been done previously (prev had done=True)
            if prev_ex.get("done") is not True:
                continue
            curr_actual = (curr_ex.get("actual") or "").strip()
            prev_actual = (prev_ex.get("actual") or "").strip()
            if curr_actual and prev_actual and curr_actual != prev_actual:
                # Extract numeric weights for comparison
                curr_nums = _rej.findall(r"\d+(?:\.\d+)?", curr_actual)
                prev_nums = _rej.findall(r"\d+(?:\.\d+)?", prev_actual)
                if curr_nums and prev_nums:
                    try:
                        curr_val = float(curr_nums[0])
                        prev_val = float(prev_nums[0])
                        if abs(curr_val - prev_val) >= 1.0:  # 1kg threshold for retroactive
                            retroactive_changes.append({
                                "label": label,
                                "exercise": name,
                                "old_weight": prev_actual,
                                "new_weight": curr_actual,
                            })
                    except (ValueError, TypeError):
                        pass

    has_changes = bool(new_sessions_done or newly_skipped or new_health_data or retroactive_changes)
    return {
        "new_sessions_done": new_sessions_done,
        "newly_skipped": newly_skipped,
        "no_rpe_sessions": no_rpe_sessions,
        "new_health_data": new_health_data,
        "retroactive_changes": retroactive_changes,
        "current_snapshot_json": _json.dumps(current_snapshot),
        "snapshot_updated": has_changes,
    }


import re as _re_module
_re_rpe = _re_module.compile(r"@?RPE\s*\d", _re_module.IGNORECASE)


def _format_delta_for_athlete(session_delta: dict, session: dict) -> str:
    """
    Format a session delta dict into a transparent, readable "I see from the sheet:" block.
    Shown at the START of the endsession message — athlete can correct before we log anything.

    Example output:
        What I see in the sheet for today's session:
          ✓ Squat 4x5 @ 90kg
          ~ Bench 4x5 @ 75kg — 3 sets logged, 1 missing
          ✗ Barbell Row 3x8 — not logged

        Retroactive change detected:
          Tuesday Squat: was 87.5kg → now 85kg in the sheet

        Tell me if any of this is wrong before I close the session.

    Returns empty string if no useful delta to show.
    """
    exercises = session.get("exercises", []) if session else []
    new_done = session_delta.get("new_sessions_done", [])
    retro = session_delta.get("retroactive_changes", [])

    lines = []

    if new_done:
        session_info = new_done[0]  # Focus on today's session
        lines.append("Here's what I see in the sheet for this session:")

        done_names = set()
        skipped_names = set(session_info.get("skipped_names", []))

        for ex in exercises:
            name = ex.get("name", "")
            if not name:
                continue
            prescribed = ex.get("weight") or ex.get("prescribed") or ""
            actual = ex.get("actual") or ""
            is_done = ex.get("done") is True

            if is_done:
                done_names.add(name)
                if actual:
                    weight_str = f" @ {actual}"
                elif prescribed:
                    weight_str = f" @ {prescribed} (prescribed)"
                else:
                    weight_str = ""
                sets_reps = ex.get("sets_reps") or ex.get("scheme") or ""
                sr_str = f" {sets_reps}" if sets_reps else ""
                lines.append(f"  ✓ {name}{sr_str}{weight_str}")
            elif name in skipped_names:
                sets_reps = ex.get("sets_reps") or ex.get("scheme") or ""
                sr_str = f" {sets_reps}" if sets_reps else ""
                prescribed_str = f" @ {prescribed}" if prescribed else ""
                lines.append(f"  ✗ {name}{sr_str}{prescribed_str} — not logged")

        # Weight deviations
        for dev in session_info.get("weight_deviations", []):
            lines.append(f"  ⚠ Weight deviation: {dev}")

    if retro:
        if lines:
            lines.append("")
        lines.append("Retroactive change detected in the sheet:")
        for change in retro:
            lines.append(
                f"  {change['label']} — {change['exercise']}: "
                f"was {change['old_weight']} → now {change['new_weight']}"
            )
        lines.append("  Want me to update my records with the new value?")

    if not lines:
        return ""

    lines.append("")
    lines.append("Let me know if any of this is wrong before I close the session.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cascade context helpers — multi-layer coaching coherence system
# ---------------------------------------------------------------------------

def _get_authoritative_week_num() -> int:
    """
    Return the current training week using the sheet as ground truth.

    Calls infer_week_from_sheet() which scans actual Done entries — this handles
    the off-by-one that fires when calendar math says Week 10 but the athlete is
    still training Week 9 sessions. Falls back to calendar computation on error.
    Prints a warning when sheet-derived and calendar-derived weeks disagree.
    """
    try:
        from sheets import infer_week_from_sheet
        sheet_week = infer_week_from_sheet()
        calendar_week = compute_current_week(resolve_program_start_date())
        if sheet_week != calendar_week:
            print(
                f"  [WeekCheck] Sheet says Week {sheet_week}, "
                f"calendar says Week {calendar_week} — using sheet "
                f"({'athlete ahead' if sheet_week > calendar_week else 'athlete behind — deload swap or extended break'})."
            )
        return sheet_week
    except Exception:
        return compute_current_week(resolve_program_start_date())


_MUSCLE_HINTS: dict[str, str] = {
    "squat": "quad/posterior", "deadlift": "posterior/back", "rdl": "posterior",
    "sumo": "posterior/back", "trap bar": "posterior/back",
    "bench": "push/chest", "incline": "push/chest", "decline": "push/chest",
    "flye": "push/chest", "fly": "push/chest",
    "overhead press": "push/shoulder", "ohp": "push/shoulder", "military": "push/shoulder",
    "dip": "push/tricep", "pushdown": "push/tricep", "tricep": "push/tricep",
    "row": "pull/back", "pullup": "pull/back", "pull-up": "pull/back",
    "pulldown": "pull/back", "chin": "pull/back",
    "curl": "pull/bicep",
    "lunge": "quad/unilateral", "split squat": "quad/unilateral", "step up": "quad/unilateral",
    "nordic": "hamstring", "glute bridge": "posterior", "hip thrust": "posterior",
    "lateral raise": "shoulder/delt", "face pull": "shoulder/rear delt",
    "press": "push",  # generic push — matches overhead press, etc. (last resort)
    "calf": "calf", "ab": "core", "plank": "core",
}


def _infer_muscle_group(name: str) -> str:
    """Return a muscle group label for an exercise name (best-effort keyword match)."""
    name_lower = name.lower()
    for key, group in _MUSCLE_HINTS.items():
        if key in name_lower:
            return group
    return "general"


def _detect_program_phase(week_num: int, total_weeks: int) -> str:
    """Return training block phase name based on program position."""
    if total_weeks <= 0:
        return "unknown phase"
    pct = week_num / total_weeks
    if pct < 0.25:
        return "early accumulation"
    elif pct < 0.55:
        return "mid-block accumulation"
    elif pct < 0.75:
        return "intensification"
    elif pct < 0.90:
        return "peak / realization"
    else:
        return "taper / program end"


def _get_session_position(lift_name: str, lift_history: list, week_num: int, total_weeks: int) -> str:
    """
    Return a session position string like 'Squat session #22 (Week 9 of 30)'.
    Counts unique training dates where this lift was performed in lift_history.
    """
    if not lift_name or not lift_history:
        return ""
    lift_lower = lift_name.lower()
    session_dates: set = set()
    for entry in lift_history:
        ex = (entry.get("Exercise") or entry.get("exercise_name") or "").lower()
        if lift_lower in ex or (len(lift_lower) >= 4 and ex and lift_lower[:4] in ex):
            d = entry.get("Date") or entry.get("date") or ""
            if d:
                session_dates.add(str(d))
    count = len(session_dates)
    if count == 0:
        return ""
    pos = f"{lift_name} session #{count}"
    if total_weeks and week_num:
        pos += f" (Week {week_num} of {total_weeks})"
    return pos


def _get_phase_rpe_target(week_num: int, total_weeks: int) -> str:
    """Return RPE target range string based on program phase."""
    phase = _detect_program_phase(week_num, total_weeks)
    return {
        "early accumulation": "RPE 6-7",
        "mid-block accumulation": "RPE 7-8",
        "intensification": "RPE 8-8.5",
        "peak / realization": "RPE 9+",
        "taper / program end": "RPE 7-8",
    }.get(phase, "RPE 7-8")


def _detect_session_conflicts(coach_state: dict, commands: list) -> str:
    """
    Detect conflicts between TOMORROW_PLAN and unresolved pending catch-ups.
    Returns a conflict description string or 'clear'.
    """
    tomorrow_plan = coach_state.get("TOMORROW_PLAN", {}).get("summary", "")
    last_evening = coach_state.get("LAST_EVENING_PROTOCOL", {}).get("summary", "")
    yesterday = str(date.today() - timedelta(days=1))

    pending_catchups = [
        c for c in commands
        if c.get("Command", "").upper() == "PENDING_CATCHUP"
        and c.get("Applied", "").upper() not in ("Y", "DECLINED")
    ]

    if not pending_catchups:
        return "clear"

    catchup_str = " | ".join(c.get("Value", "") for c in pending_catchups[:3])
    if tomorrow_plan and last_evening == yesterday:
        return (
            f"⚠️ CONFLICT: TOMORROW_PLAN='{tomorrow_plan}' but "
            f"{len(pending_catchups)} catch-up(s) unresolved: {catchup_str}. "
            f"Must address explicitly — do not silently ignore."
        )
    return f"{len(pending_catchups)} catch-up(s) pending: {catchup_str}"


def _build_cascade_l1(coach_state: dict, projections: dict | None = None) -> str:
    """Layer 1 — Strategic: program position, goals, annual arc, long-term vision."""
    lines = ["LAYER 1 — STRATEGIC (where in the journey)"]

    program_summary = coach_state.get("PROGRAM", {}).get("summary", "")
    if program_summary:
        lines.append(f"  Program: {program_summary[:180]}")

    # Goal proximity from projections
    if projections:
        proximity = projections.get("goal_proximity", [])
        for item in proximity[:3]:
            lift = item.get("lift", "")
            current = item.get("current_1rm", "")
            target = item.get("target", "")
            if lift and current and target:
                try:
                    gap = float(target) - float(current)
                    if gap <= 0:
                        status = "🏆 GOAL REACHED"
                    elif gap <= 5:
                        status = f"🎯 {gap:.1f}kg from target — critical"
                    else:
                        status = f"{gap:.1f}kg from target"
                    lines.append(f"  Goal: {lift} {current}kg / {target}kg — {status}")
                except (ValueError, TypeError):
                    pass

    # Annual arc — first 2 sentences
    annual_arc = coach_state.get("ANNUAL_ARC", {}).get("summary", "")
    if annual_arc:
        sentences = annual_arc.replace("\n", " ").split(". ")
        arc_brief = ". ".join(sentences[:2]).strip()
        if arc_brief:
            lines.append(f"  Annual arc: {arc_brief[:200]}")

    # Long-term projections (1yr view per lift)
    if projections:
        long_term = projections.get("long_term", {})
        for lift_name, lt in list(long_term.items())[:2]:
            rate = lt.get("current_rate", 0)
            end_prog = lt.get("end_of_program", "")
            yr1 = lt.get("1yr", "")
            if end_prog or yr1:
                lines.append(
                    f"  Projection: {lift_name} → end-of-program {end_prog}kg | "
                    f"1yr {yr1}kg | trend {rate:+.1f}kg/wk"
                )

    # Long-term vision
    dreams = coach_state.get("ATHLETE_DREAMS", {}).get("summary", "")
    if dreams:
        lines.append(f"  Long-term vision: {dreams[:150]}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_cascade_l2(
    coach_state: dict,
    projections: dict | None,
    current_week_days: list,
) -> str:
    """Layer 2 — Mesocycle: weekly intent, periodization, recovery, compliance."""
    lines = ["LAYER 2 — MESOCYCLE (this week's purpose)"]

    weekly_intent = coach_state.get("WEEKLY_INTENT", {}).get("summary", "")
    if weekly_intent:
        lines.append(f"  Intent: {weekly_intent[:200]}")

    coaching_reason = coach_state.get("COACHING_REASON", {}).get("summary", "")
    if coaching_reason:
        lines.append(f"  Periodization: {coaching_reason[:160]}")

    # Session compliance
    done_count = sum(
        1 for d in current_week_days
        if any(ex.get("done") is True for ex in d.get("exercises", []))
    )
    total_days = len(current_week_days)
    schedule_ctx = coach_state.get("WEEKLY_SCHEDULE", {}).get("summary", "")
    sessions_line = f"  Sessions: {done_count}/{total_days} done this week"
    if schedule_ctx:
        sessions_line += f" | Schedule: {schedule_ctx[:80]}"
    lines.append(sessions_line)

    # TSB / recovery state
    if projections:
        fatigue = projections.get("fatigue", {})
        tsb = fatigue.get("TSB", None)
        readiness = fatigue.get("readiness", "")
        if tsb is not None:
            lines.append(f"  Recovery: TSB = {tsb:+.1f} ({readiness})")
        vol_spikes = projections.get("volume_spikes", [])
        if vol_spikes:
            spike = vol_spikes[0]
            lines.append(
                f"  ⚠️ Volume spike: {spike.get('lift', '?')} this week ({spike.get('alert', '?')})"
            )

    # Behavior patterns (brief)
    behavior = coach_state.get("BEHAVIOR_PATTERNS", {}).get("summary", "")
    if behavior:
        lines.append(f"  Patterns: {behavior[:130]}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_cascade_l3(
    coach_state: dict,
    commands: list,
    current_week_days: list,
    health_log: list | None,
    orientation: str,
    today: date,
) -> str:
    """
    Layer 3 — Session: committed plan, conflicts, resolved session, lift state, health.
    orientation: "today" | "tomorrow" | "session_end"
    """
    header = "LAYER 3 — TODAY'S SESSION" if orientation != "tomorrow" else "LAYER 3 — TOMORROW'S SESSION"
    lines = [header]

    tomorrow_plan = coach_state.get("TOMORROW_PLAN", {}).get("summary", "")
    last_evening = coach_state.get("LAST_EVENING_PROTOCOL", {}).get("summary", "")
    yesterday = str(today - timedelta(days=1))
    plan_is_fresh = bool(tomorrow_plan and last_evening == yesterday)

    if orientation == "tomorrow":
        lines.append("  Building tomorrow's plan — pending catch-ups must be resolved first")
    else:
        if plan_is_fresh:
            lines.append(f"  Commitment (last night): {tomorrow_plan}")
        else:
            lines.append("  Commitment (last night): none — derive from program sheet")

    # Pending catch-ups
    pending_catchups = [
        c for c in commands
        if c.get("Command", "").upper() == "PENDING_CATCHUP"
        and c.get("Applied", "").upper() not in ("Y", "DECLINED")
    ]
    if pending_catchups:
        catchup_str = " | ".join(c.get("Value", "") for c in pending_catchups[:3])
        lines.append(f"  Pending catch-ups: {catchup_str}")
    else:
        lines.append("  Pending catch-ups: none")

    # Conflict status
    conflict = _detect_session_conflicts(coach_state, commands)
    lines.append(f"  Conflicts: {conflict}")

    # Daily focus (what this morning's brief said) — relevant for session_end/tomorrow
    if orientation in ("session_end", "tomorrow"):
        daily_focus_raw = coach_state.get("DAILY_FOCUS", {}).get("summary", "")
        if daily_focus_raw and daily_focus_raw.startswith(str(today)):
            daily_focus_today = daily_focus_raw[len(str(today)) + 3:]
            lines.append(f"  Today's brief focused on: {daily_focus_today[:150]}")

    # Primary lift domain state
    today_str = today.strftime("%A")
    today_session = None
    for day in current_week_days:
        if today_str.lower() in day.get("label", "").lower():
            today_session = day
            break
    # If no day-name match, try TOMORROW_PLAN label
    if not today_session and plan_is_fresh and orientation != "tomorrow":
        plan_label = tomorrow_plan.split(" | ")[0].strip() if " | " in tomorrow_plan else ""
        if plan_label:
            for day in current_week_days:
                if plan_label.lower() in day.get("label", "").lower():
                    today_session = day
                    break

    if today_session:
        main_lifts = [ex for ex in today_session.get("exercises", []) if ex.get("weight")]
        if main_lifts:
            primary_name = main_lifts[0].get("name", "")
            name_lower = primary_name.lower()
            lift_domain = None
            if "squat" in name_lower:
                lift_domain = "SQUAT"
            elif "bench" in name_lower:
                lift_domain = "BENCH"
            elif "deadlift" in name_lower or "rdl" in name_lower:
                lift_domain = "DEADLIFT"
            elif "press" in name_lower or "ohp" in name_lower:
                lift_domain = "OHP"
            if lift_domain:
                state = coach_state.get(lift_domain, {}).get("summary", "")
                if state:
                    lines.append(f"  {primary_name} state: {state[:160]}")

    # Health inputs
    if health_log:
        try:
            latest = health_log[0]
            sleep = latest.get("Sleep (hrs)", "")
            energy = latest.get("Energy (1-10)", "")
            bw = latest.get("Bodyweight (kg)", "")
            health_parts = []
            if sleep:
                health_parts.append(f"Sleep {sleep}h")
            if energy:
                health_parts.append(f"Energy {energy}/10")
            if bw:
                health_parts.append(f"BW {bw}kg")
            if health_parts:
                lines.append(f"  Health inputs: {' | '.join(health_parts)}")
        except Exception:
            pass

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_cascade_l4(
    coach_state: dict,
    telegram_log: list,
    coach_focus: list,
    today: date,
) -> str:
    """Layer 4 — Immediate: last brief, today's Telegram, new concerns."""
    lines = ["LAYER 4 — LAST 24H (immediate context)"]
    today_str = str(today)

    # Last brief content
    last_brief_content = coach_state.get("LAST_BRIEF_CONTENT", {}).get("summary", "")
    if last_brief_content and last_brief_content.startswith(today_str):
        brief_text = last_brief_content[len(today_str) + 3:]  # strip "DATE | "
        lines.append(f"  Last brief sent: {brief_text[:200]}")
    else:
        lines.append("  Last brief: none yet today")

    # Today's Telegram (in/out)
    today_tg = [m for m in telegram_log if m.get("Date", "") == today_str]
    if today_tg:
        in_msgs = [
            m.get("Message", "")[:80]
            for m in today_tg
            if m.get("Direction", "").upper() == "IN"
        ][-3:]
        out_msgs = [
            m.get("Message", "")[:80]
            for m in today_tg
            if m.get("Direction", "").upper() == "OUT"
        ][-2:]
        if in_msgs:
            lines.append(f"  Athlete said today: {' / '.join(in_msgs)}")
        if out_msgs:
            lines.append(f"  Coach sent today: {' / '.join(out_msgs)}")
    else:
        lines.append("  Today's Telegram: none yet")

    # New concerns/followups from today
    new_concerns = [
        f.get("Item", "")[:100]
        for f in coach_focus
        if f.get("Status", "") == "OPEN"
        and f.get("Category", "") in ("CONCERN", "FOLLOWUP")
        and str(f.get("Date", "")).startswith(today_str)
    ][:3]
    if new_concerns:
        lines.append(f"  New concerns today: {' | '.join(new_concerns)}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_cascade_context(
    coach_state: dict,
    commands: list,
    projections: dict | None,
    current_week_days: list,
    health_log: list | None,
    telegram_log: list,
    coach_focus: list,
    today: date,
    orientation: str = "today",
) -> str:
    """
    Build a 4-layer coaching context cascade for structured reasoning.

    orientation: "today" (brief) | "tomorrow" (evening protocol) | "session_end" (endsession/post)

    Each layer feeds the next — Layer 3 is derived from Layers 1+2 commitments, Layer 4 validates.
    The LLM must reason through all 4 layers before generating output.
    """
    blocks = []
    for fn, args in [
        (_build_cascade_l1, (coach_state, projections)),
        (_build_cascade_l2, (coach_state, projections, current_week_days)),
        (_build_cascade_l3, (coach_state, commands, current_week_days, health_log, orientation, today)),
        (_build_cascade_l4, (coach_state, telegram_log, coach_focus, today)),
    ]:
        try:
            block = fn(*args)
            if block:
                blocks.append(block)
        except Exception as e:
            print(f"[Cascade] Layer build failed (non-fatal): {e}")

    return "\n\n".join(blocks) if blocks else "(cascade unavailable)"


def _build_ordered_exercise_context(exercises: list) -> tuple[str, str]:
    """
    Build an ordered exercise list with muscle group labels.

    Returns:
        (ordered_text, fatigue_notes)
        ordered_text — numbered list for prompt injection
        fatigue_notes — auto-generated notes about skip impacts on downstream exercises
    """
    ordered_lines = []
    for i, ex in enumerate(exercises, start=1):
        name = ex.get("name", "")
        if not name:
            continue
        weight = ex.get("weight", "")
        sets_reps = ex.get("sets_reps", "")
        done = ex.get("done")
        actual = ex.get("actual", "")
        note = ex.get("session_note", "")
        muscle = _infer_muscle_group(name)

        if done is True:
            actual_str = actual or f"{weight} {sets_reps}".strip()
            status = f"COMPLETED ({actual_str})"
        elif done is False:
            status = "SKIPPED"
        else:
            status = "NOT LOGGED"

        line = f"  {i}. {name} [{muscle}]: {weight} {sets_reps}".rstrip() + f" — {status}"
        if note:
            line += f" | note: {note[:60]}"
        ordered_lines.append(line)

    # Auto-generate fatigue chain notes for skipped exercises
    fatigue_notes = []
    for i, ex in enumerate(exercises):
        if ex.get("done") is not False:
            continue
        skipped_name = ex.get("name", "")
        if not skipped_name:
            continue
        skipped_muscle = _infer_muscle_group(skipped_name)
        # Find downstream exercises with same/overlapping muscle group
        downstream = []
        for j, later_ex in enumerate(exercises[i + 1:], start=i + 2):
            later_name = later_ex.get("name", "")
            if not later_name:
                continue
            later_muscle = _infer_muscle_group(later_name)
            # Check for muscle group overlap (shared first keyword)
            sk_root = skipped_muscle.split("/")[0]
            lt_root = later_muscle.split("/")[0]
            if sk_root == lt_root or skipped_muscle in later_muscle or later_muscle in skipped_muscle:
                status = "completed" if later_ex.get("done") is True else "not yet done"
                downstream.append(f"{later_name} (pos {j}, {status})")
        if downstream:
            fatigue_notes.append(
                f"NOTE: {skipped_name} [pos {i + 1}, {skipped_muscle}] was SKIPPED — "
                f"downstream {skipped_muscle} exercises had fresh muscles: {', '.join(downstream)}"
            )

    ordered_text = "\n".join(ordered_lines) if ordered_lines else "(no exercises)"
    fatigue_note_text = "\n".join(fatigue_notes) if fatigue_notes else ""
    return ordered_text, fatigue_note_text


def _today_telegram_covers_topic(topic_keywords: list[str],
                                  recent_tg: list[dict],
                                  today_str: str = None) -> bool:
    """
    Returns True if today's inbound Telegram messages already cover the given topic.
    Used to prevent the coach from asking about something the athlete already addressed.

    topic_keywords: list of lowercase keywords/phrases to match (any one match = covered)
    recent_tg: list of Telegram log dicts with 'Date', 'Direction', 'Message' keys
    today_str: ISO date string (defaults to today)

    Examples:
      _today_telegram_covers_topic(["rpe", "felt like", "left in tank"], tg_log)
      _today_telegram_covers_topic(["skipped", "didn't do", "couldn't", "missed"], tg_log)
      _today_telegram_covers_topic(["how it went", "session done", "finished", "completed"], tg_log)
    """
    if not recent_tg or not topic_keywords:
        return False

    if today_str is None:
        today_str = str(date.today())

    for msg in recent_tg:
        if msg.get("Date", "") != today_str:
            continue
        # Only check inbound messages (athlete → coach)
        direction = (msg.get("Direction") or msg.get("direction") or "").upper()
        if direction not in ("IN", "INBOUND", "ATHLETE", ""):
            continue
        text = (msg.get("Message") or msg.get("message") or "").lower()
        if any(kw.lower() in text for kw in topic_keywords):
            return True

    return False


# ---------------------------------------------------------------------------
# Contextual cue library — real coaching cues per lift, organized by type.
# The model selects from these rather than improvising, ensuring technical
# accuracy and variety. _select_lift_cue() picks the most contextually
# relevant cues and surfaces them in brief/evening prompts.
# ---------------------------------------------------------------------------

LIFT_CUE_BANK: dict[str, dict[str, list[str]]] = {
    "SQUAT": {
        "technique": [
            "Brace your core before unracking — create 360° pressure, not just forward",
            "Break at hips AND knees simultaneously — don't squat-morning it",
            "Drive knees out actively throughout the descent, not just at the bottom",
            "Cue 'chest up' in the hole — prevents the upper back rounding over",
            "Foot pressure: big toe, pinky toe, heel — full tripod, don't let the arch collapse",
        ],
        "tempo": [
            "Controlled descent (3 sec), brief pause at the bottom, explosive drive",
            "Don't bounce out of the hole — own the bottom for 0.5 seconds",
            "Slow eccentric builds motor control; make the descent your prep for the drive",
        ],
        "mental": [
            "Visualize driving the floor away from you, not just standing up",
            "Treat every warm-up set with the same intent as your top set",
            "If the bar feels heavy in the rack, it'll feel heavier in the hole — brace harder",
        ],
        "setup": [
            "Bar position: high bar sits on traps, low bar sits on rear delts — pick one and commit",
            "Walk-out: 2 steps back max, set your stance, don't fidget",
            "Take a big breath at the top, not halfway down",
        ],
    },
    "BENCH": {
        "technique": [
            "Retract and depress the scapulae — pull your shoulder blades into your back pockets",
            "Elbows at ~45°, not flared 90° — protects the shoulder and maintains power",
            "Drive your feet into the floor; leg drive transfers through a stable arch",
            "Touch the bar to your lower chest / sternum, not your clavicle",
            "Think 'pull the bar apart' as you press — activates lats, creates stability",
        ],
        "tempo": [
            "Controlled descent, 1-sec pause, explosive press — no bouncing",
            "The descent controls your groove; slow it down to find the ideal bar path",
            "Pause bench builds starting strength — treat the pause as the point, not an obstacle",
        ],
        "mental": [
            "Visualize the path of the bar before you unrack: diagonal line, not straight up",
            "Aggressive lockout — don't drift back over your face; drive the bar back slightly",
            "Think 'push yourself into the bench' not 'push the bar up'",
        ],
        "setup": [
            "Arch is not cheating — it's shortening the range while staying safe",
            "Grip width: pinkies on the ring marks is a good starting point",
            "Get tight before unracking — setup is 50% of the lift",
        ],
    },
    "DEADLIFT": {
        "technique": [
            "Push the floor away first — deadlift is a leg press that happens to hold a bar",
            "Bar stays over mid-foot at setup; if it drifts forward, you lose leverage",
            "Lat engagement ('protect your armpits') keeps the bar close all the way up",
            "Lock your hips and shoulders out simultaneously at the top — no hyperextending",
            "On the eccentric: hinge first (push hips back), then bend knees once bar passes them",
        ],
        "tempo": [
            "No bouncing between reps — reset position and brace each time",
            "Touch-and-go is a skill; if form breaks, switch to dead stop",
            "Slow the last 20% of the descent — teaches control and saves your lower back",
        ],
        "mental": [
            "Think 'bend the bar around your legs' before initiating the pull",
            "Big air, brace, pull — in that order, every single rep",
            "Before the pull: create tension in the whole system. Don't just yank it.",
        ],
        "setup": [
            "Hip height at setup: hips above knees, shoulders above the bar, mid-foot under bar",
            "Sumo: push knees out hard off the floor — don't let them cave inward",
            "Conventional: shoulder-width stance, double overhand grip until grip is the limit",
        ],
    },
    "OHP": {
        "technique": [
            "Bar starts at upper chest, elbows slightly in front — not fully flared",
            "Press in a slight backward arc, not straight up — the head moves back, then forward at lockout",
            "Lock out aggressively at the top — full elbow extension and shrug overhead",
            "Squeeze your glutes and quads — a rigid base transfers force from legs to shoulders",
            "Wrist position: straight, not cocked back — saves the wrist and improves power transfer",
        ],
        "tempo": [
            "No leg drive for strict press — if you dip, that's a push press. Pick one and commit.",
            "Control the descent — lowering slowly builds overhead stability and mass",
        ],
        "mental": [
            "Think 'push yourself under the bar' rather than 'push the bar up'",
            "The lockout is where the lift happens — don't relax at the top",
        ],
        "setup": [
            "Bar sits on the heel of the palm, not in the fingers",
            "Chin slightly back off the bar path — the bar goes over your face, not around it",
        ],
    },
    "GENERAL": {
        "breathing": [
            "Valsalva maneuver: big breath into your belly, brace, then lift — exhale at the top",
            "Don't exhale during the hardest part of the rep",
        ],
        "recovery": [
            "Full rest between top sets: 3-5 minutes. Strength is lost in incomplete rest.",
            "Between warm-ups: 90 seconds is enough. Use that time to rehearse the movement mentally.",
        ],
        "mental": [
            "Warm-up sets are rehearsal, not just temperature — treat them like the real thing",
            "If a set felt too easy, it probably was — don't chase false confidence",
        ],
    },
}


def _select_lift_cue(lift_name: str, coach_state: dict,
                     session_notes: str = "") -> list[str]:
    """
    Pick 2-3 contextually relevant cues for a given lift.
    Uses RPE history and session notes to select the cue type:
    - High RPE (>=8.5) → mental / breathing cues
    - Low RPE (<=5.5) → technique / tempo cues (go harder)
    - Notes mention 'form' / 'technique' → technique cues
    - Notes mention 'heavy' / 'hard' → mental cues
    - Default → mix of technique + setup
    """
    import random

    lift_key = "GENERAL"
    for key in LIFT_CUE_BANK:
        if key == "GENERAL":
            continue
        if key.lower() in lift_name.lower() or lift_name.lower() in key.lower():
            lift_key = key
            break

    bank = LIFT_CUE_BANK.get(lift_key, LIFT_CUE_BANK["GENERAL"])
    general = LIFT_CUE_BANK["GENERAL"]

    # Determine context from RPE history
    rpe_summary = coach_state.get(lift_key, {}).get("summary", "").lower()
    notes_lower = (session_notes or "").lower()

    avg_rpe = None
    m = _re_module.search(r"avg rpe (\d+(?:\.\d+)?)", rpe_summary)
    if m:
        try:
            avg_rpe = float(m.group(1))
        except (ValueError, TypeError):
            pass

    selected: list[str] = []

    if avg_rpe and avg_rpe >= 8.5:
        # Heavy — mental + breathing help most
        selected += random.sample(bank.get("mental", []), min(2, len(bank.get("mental", []))))
        selected += random.sample(general.get("breathing", []), 1)
    elif avg_rpe and avg_rpe <= 5.5:
        # Light — push harder, technique focus
        selected += random.sample(bank.get("technique", []), min(2, len(bank.get("technique", []))))
        selected += random.sample(bank.get("tempo", []) or bank.get("technique", []), 1)
    elif any(kw in notes_lower for kw in ("form", "technique", "bar path", "depth")):
        selected += random.sample(bank.get("technique", []), min(2, len(bank.get("technique", []))))
        selected += random.sample(bank.get("setup", bank.get("technique", [])), 1)
    elif any(kw in notes_lower for kw in ("heavy", "hard", "failed", "grind")):
        selected += random.sample(bank.get("mental", []), min(2, len(bank.get("mental", []))))
        selected += random.sample(general.get("breathing", []), 1)
    else:
        # Default: technique + setup, varied
        selected += random.sample(bank.get("technique", []), min(1, len(bank.get("technique", []))))
        selected += random.sample(bank.get("setup", bank.get("technique", [])), min(1, len(bank.get("setup", bank.get("technique", [])))))
        selected += random.sample(bank.get("mental", []) or general.get("mental", []), 1)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for c in selected:
        if c not in seen:
            seen.add(c)
            result.append(c)

    return result[:3]


def _build_garmin_summary(metrics_list: list) -> str:
    """
    Produce a compact Coach State GARMIN_SUMMARY from a list of daily metric dicts.
    Called after sync_garmin() to persist a human-readable summary for every prompt.
    """
    if not metrics_list:
        return ""

    hrv_vals = [m["hrv_ms"] for m in metrics_list if m.get("hrv_ms")]
    sleep_vals = [m["sleep_hrs"] for m in metrics_list if m.get("sleep_hrs")]
    rhr_vals = [m["resting_hr"] for m in metrics_list if m.get("resting_hr")]
    bb_vals = [m["body_battery_end"] for m in metrics_list if m.get("body_battery_end") is not None]

    parts = []

    if hrv_vals:
        avg = round(sum(hrv_vals) / len(hrv_vals))
        parts.append(f"HRV {len(hrv_vals)}d avg: {avg}ms (range {min(hrv_vals)}–{max(hrv_vals)})")
        # Trend: compare first half vs second half (newest first in list)
        if len(hrv_vals) >= 4:
            newer = hrv_vals[:len(hrv_vals) // 2]
            older = hrv_vals[len(hrv_vals) // 2:]
            diff = round(sum(newer) / len(newer) - sum(older) / len(older), 1)
            if diff > 3:
                parts.append("HRV trending up (recovering)")
            elif diff < -3:
                parts.append("HRV trending down (accumulating fatigue)")

    if sleep_vals:
        avg = round(sum(sleep_vals) / len(sleep_vals), 1)
        parts.append(f"Sleep {avg}h avg")

    if rhr_vals:
        avg = round(sum(rhr_vals) / len(rhr_vals))
        parts.append(f"RHR {avg}bpm")

    if bb_vals:
        # Show trend: first entry is most recent (yesterday), last is oldest
        recent = bb_vals[0] if bb_vals else None
        oldest = bb_vals[-1] if len(bb_vals) > 1 else None
        if recent is not None:
            trend_str = ""
            if oldest is not None and len(bb_vals) >= 3:
                if recent > oldest + 10:
                    trend_str = " (recovering)"
                elif recent < oldest - 10:
                    trend_str = " (declining)"
            parts.append(f"Body battery end-of-day: {recent}{trend_str}")

    return ". ".join(parts) + "." if parts else ""


def sync_garmin(days: int = 7, dry_run: bool = False) -> list:
    """
    Fetch Garmin data for last N days and upsert into Health Log.
    Generates + stores GARMIN_SUMMARY in Coach State.
    Called from run_proactive() and --sync-garmin flag.

    Returns list of synced metric dicts (empty on failure or missing credentials).
    Non-fatal: any error is caught and printed, coach continues normally.
    """
    if not os.environ.get("GARMIN_EMAIL") or not os.environ.get("GARMIN_PASSWORD"):
        print("  [Garmin] GARMIN_EMAIL or GARMIN_PASSWORD not set — skipping")
        return []

    try:
        from garmin import GarminClient
    except ImportError:
        print("  [Garmin] garminconnect not installed — skipping. Run: pip install garminconnect")
        return []

    client = GarminClient()
    if not client.is_available():
        print("  [Garmin] No credentials or login failed — skipping.")
        return []

    print(f"  [Garmin] Fetching last {days} days of recovery data...")
    metrics_list = client.fetch_range(days=days)

    if not metrics_list:
        print("  [Garmin] No data returned.")
        return []

    if dry_run:
        print(f"  [DRY RUN] Would sync {len(metrics_list)} Garmin day(s):")
        for m in metrics_list:
            print(f"    {m['date']}: HRV={m.get('hrv_ms')}ms sleep={m.get('sleep_hrs')}h "
                  f"RHR={m.get('resting_hr')}bpm BB_end={m.get('body_battery_end')}")
        summary = _build_garmin_summary(metrics_list)
        print(f"  [DRY RUN] GARMIN_SUMMARY would be: {summary}")
        return metrics_list

    from memory import upsert_health_log_row, upsert_coach_state

    synced = 0
    for m in metrics_list:
        updates = {}
        if m.get("steps") is not None:
            updates["Steps"] = str(m["steps"])
        if m.get("sleep_hrs") is not None:
            updates["Sleep (hrs)"] = str(m["sleep_hrs"])
        if m.get("hrv_ms") is not None:
            updates["HRV (ms)"] = str(m["hrv_ms"])
        if m.get("body_battery_end") is not None:
            updates["Body Battery"] = str(m["body_battery_end"])
        if updates:
            result = upsert_health_log_row(m["date"], updates)
            if result in ("inserted", "updated"):
                synced += 1

    summary = _build_garmin_summary(metrics_list)
    if summary:
        upsert_coach_state("GARMIN_SUMMARY", summary, "HIGH")

    print(f"  [Garmin] Synced {synced}/{len(metrics_list)} day(s). Summary: {summary[:100]}")
    return metrics_list


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

    # Sync Garmin recovery data before reading health_log so the pass sees fresh data
    try:
        sync_garmin(days=7, dry_run=dry_run)
    except Exception as _garmin_err:
        print(f"  Garmin sync failed (non-fatal): {_garmin_err}")

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

    # Load current week so proactive pass knows what's scheduled today + delta detection
    program_data_p = None
    session_delta: dict = {}
    try:
        from sheets import read_program_data
        program_data_p = read_program_data(week_num=compute_current_week(resolve_program_start_date()), lookback=0)
        session_delta = _detect_session_delta(program_data_p, coach_state)
        if session_delta.get("snapshot_updated") and not dry_run:
            from memory import upsert_coach_state
            upsert_coach_state("LAST_PROGRAM_SNAPSHOT",
                               session_delta["current_snapshot_json"], "HIGH")
            print(f"  Program snapshot updated ({len(session_delta['new_sessions_done'])} new session(s) detected)")
    except Exception as e:
        print(f"  Proactive: program load failed (non-fatal): {e}")

    # Sheet delta sync — update Coach State and resolve stale Commands
    try:
        from sheet_sync import SheetSyncEngine
        from memory import read_lift_history
        _proactive_days = (program_data_p or {}).get("current_week", {}).get("days", [])
        _proactive_week = compute_current_week(resolve_program_start_date())
        _sync_result = SheetSyncEngine().run_sync(
            week_num=_proactive_week,
            current_week_days=_proactive_days,
            health_log=health_log,
            lift_history=read_lift_history(limit=50),
            commands=memory_data.get("commands", []),
            dry_run=dry_run,
        )
        if _sync_result.get("resolved_catchups"):
            from memory import read_commands
            memory_data["commands"] = read_commands()
    except Exception as _e:
        print(f"  Sheet sync failed (non-fatal): {_e}")

    # Smart dedup: check which topics athlete already addressed in today's Telegram
    today_tg_for_dedup = [
        m for m in memory_data.get("telegram_log", [])
        if m.get("Date", "") == str(today)
    ]
    topic_coverage = {
        "RPE / effort feedback": _today_telegram_covers_topic(
            ["rpe", "felt like", "left in tank", "rir", "rate of perceived"],
            today_tg_for_dedup, str(today)
        ),
        "session recap / completion": _today_telegram_covers_topic(
            ["session done", "finished", "completed", "all done", "trained", "workout done",
             "did it", "went well", "went badly"],
            today_tg_for_dedup, str(today)
        ),
        "skipped exercises / why": _today_telegram_covers_topic(
            ["skipped", "didn't do", "couldn't", "missed", "left it out", "cut it short"],
            today_tg_for_dedup, str(today)
        ),
        "schedule / availability": _today_telegram_covers_topic(
            ["can't train", "won't train", "rest day", "no session", "rescheduling",
             "moving it", "pushing it"],
            today_tg_for_dedup, str(today)
        ),
    }

    system_prompt, user_message = build_proactive_prompt(
        memory_data, program_data=program_data_p, session_delta=session_delta,
        topic_coverage=topic_coverage
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=CLAUDE_HAIKU,
        max_tokens=500,
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

    # --- Fire any scheduled messages due today ---
    try:
        n_sched = run_scheduled_messages(dry_run=dry_run)
        if n_sched:
            print(f"  {n_sched} scheduled message(s) fired.")
    except Exception as e:
        print(f"  Scheduled messages failed (non-fatal): {e}")

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
        long_term_projections=projections.get("long_term", {}),
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

    # Extract [SCHEDULE: YYYY-MM-DD | message] markers — stored as SCHEDULED_MESSAGE commands
    email_text, schedule_items = extract_schedule_markers(email_text)
    if schedule_items:
        print(f"  [Scheduled messages]: {len(schedule_items)} future message(s) queued")
        if not dry_run and not no_sync:
            try:
                from memory import append_command
                for si in schedule_items:
                    append_command("SCHEDULED_MESSAGE", f"{si['target_date']} | {si['message']}")
                    print(f"    [Schedule] {si['target_date']}: {si['message'][:60]}")
            except Exception as e:
                print(f"    Schedule logging failed (non-fatal): {e}")
        elif dry_run:
            for si in schedule_items:
                print(f"    [DRY RUN] SCHEDULE {si['target_date']}: {si['message'][:60]}")

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
            from charts import (generate_1rm_chart, generate_volume_chart,
                                generate_bodyweight_chart, generate_strength_trajectory_chart)
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
            # e1RM trajectory chart with goal lines (requires weekly_e1rm)
            try:
                from strength_tracker import compute_weekly_e1rm
                from projections import _parse_lift_targets
                _traj_weekly = compute_weekly_e1rm(memory_data.get("lift_history", []))
                _traj_goals = {}
                _arc_raw = coach_state.get("ANNUAL_ARC", {})
                _arc_summary = _arc_raw.get("Summary", _arc_raw.get("summary", "")) if isinstance(_arc_raw, dict) else ""
                if _arc_summary and _arc_summary.strip().startswith("{"):
                    import json as _json_c
                    _arc = _json_c.loads(_arc_summary)
                    _medium = _arc.get("medium_goals", {})
                    _traj_goals = {k: float(v) for k, v in {
                        "Squat": _medium.get("squat_goal_kg"),
                        "Bench Press": _medium.get("bench_goal_kg"),
                        "Deadlift": _medium.get("deadlift_goal_kg"),
                    }.items() if v}
                if not _traj_goals:
                    _traj_goals = {k.title(): v for k, v in _parse_lift_targets(
                        memory_data.get("long_term_goals", "")).items()}
                c4 = generate_strength_trajectory_chart(_traj_weekly, goals=_traj_goals or None)
                if c4:
                    chart_list.append((c4, "chart-e1rm-trajectory"))
            except Exception as _ce:
                print(f"    → trajectory chart failed (non-fatal): {_ce}")
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

        # Sunday: open a collaborative weekly planning conversation (Sonnet)
        if is_weekly_summary:
            try:
                import json as _json_sun
                import anthropic as _ant_sun
                from sheets import read_program_data as _rpd_sun
                from memory import (
                    upsert_coach_state as _ucs_sun,
                    read_lift_history as _rlh_sun,
                    read_health_log as _rhl_sun,
                )
                from telegram_utils import send_telegram_message as _stm_sun
                from config import CLAUDE_MODEL as _model_sun

                # Context: last week sessions
                _lift_sun = _rlh_sun(limit=120)
                _last_wk_rows = [r for r in _lift_sun if str(r.get("Week", "")).strip() == str(week_num)]
                _days_trained = len(set(r.get("Day", "") for r in _last_wk_rows if r.get("Day")))
                _done_exs = sum(1 for r in _last_wk_rows if r.get("Completed", "").lower() in ("yes", "✓", "done"))
                _total_exs = len(_last_wk_rows)

                # Context: health readiness
                _cs_sun = read_coach_state() if read_coach_state else {}
                _health_sun = _cs_sun.get("HEALTH_READINESS", {}).get("summary", "") or \
                              _cs_sun.get("HEALTH_READINESS", {}).get("Summary", "")
                _session_q_sun = _cs_sun.get("SESSION_QUALITY", {}).get("summary", "") or \
                                 _cs_sun.get("SESSION_QUALITY", {}).get("Summary", "")
                _weekly_intent = _cs_sun.get("WEEKLY_INTENT", {}).get("summary", "") or \
                                 _cs_sun.get("WEEKLY_INTENT", {}).get("Summary", "")

                # Context: next week program
                _next_wk_data = _rpd_sun(week_num=week_num + 1, lookback=0)
                _next_days = _next_wk_data.get("current_week", {}).get("days", [])
                _sessions_data = []
                _sess_lines = []
                for _i, _d in enumerate(_next_days):
                    _slabel = _d.get("label") or f"Day {_d.get('day_num', _i+1)}"
                    _exnames = [ex.get("name") for ex in _d.get("exercises", []) if ex.get("name")]
                    _sessions_data.append({
                        "day_num": _d.get("day_num"),
                        "label": _slabel,
                        "exercises": [
                            {"name": ex.get("name"), "weight": ex.get("weight"), "sets_reps": ex.get("sets_reps")}
                            for ex in _d.get("exercises", []) if ex.get("name")
                        ],
                    })
                    _sess_lines.append(f"  {_slabel}: {', '.join(_exnames[:4])}")
                _next_wk_str = "\n".join(_sess_lines) if _sess_lines else "  (no sessions found in sheet)"

                # Build Sonnet prompt for planning opening
                # Read PENDING_FLAGS — deferred items from daily planning that need to surface
                _pending_flags_sun = []
                try:
                    import json as _json_pf
                    _pf_raw = _cs_sun.get("PENDING_FLAGS", {}).get("summary", "") or \
                              _cs_sun.get("PENDING_FLAGS", {}).get("Summary", "")
                    if _pf_raw and _pf_raw.strip().startswith("["):
                        _pending_flags_sun = _json_pf.loads(_pf_raw)
                except Exception:
                    pass
                _pending_block = ""
                if _pending_flags_sun:
                    _pf_lines = []
                    for _pf in _pending_flags_sun[-5:]:
                        _pf_type = _pf.get("type", "?")
                        _pf_content = _pf.get("content", "")
                        if isinstance(_pf_content, dict):
                            _pf_content = _json_pf.dumps(_pf_content, ensure_ascii=False)
                        _pf_lines.append(f"  [{_pf.get('date','?')} / {_pf_type}] {_pf_content}"[:120])
                    _pending_block = f"\nPENDING FLAGS (from daily planning — must address this week):\n" + "\n".join(_pf_lines) + "\n"

                _open_prompt = (
                    f"You are a strength coach opening the weekly planning conversation with {ATHLETE_NAME}.\n\n"
                    f"LAST WEEK (Week {week_num}):\n"
                    f"  Days trained: {_days_trained}, exercises completed: {_done_exs}/{_total_exs}\n"
                    f"  Session quality: {_session_q_sun or 'no data'}\n"
                    f"  Health readiness: {_health_sun or 'no data'}\n"
                    f"  Weekly intent was: {_weekly_intent or 'not set'}\n"
                    f"{_pending_block}\n"
                    f"NEXT WEEK PROGRAM (Week {week_num + 1}):\n{_next_wk_str}\n\n"
                    f"Write the opening message for this week's planning conversation.\n"
                    f"In 100-130 words:\n"
                    f"  1. One honest sentence on last week (no fluff).\n"
                    f"  2. Your recommendation for next week — which sessions, any adjustments, reasoning.\n"
                    f"     Include deload if warranted; challenge if athlete is thriving.\n"
                    f"  3. If there are PENDING FLAGS above, surface the most important one explicitly.\n"
                    f"  4. Ask: what does the week look like schedule-wise? Any constraints?\n"
                    f"Do NOT use emojis. Do NOT add motivational filler."
                )
                _open_resp = _ant_sun.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
                    model=_model_sun, max_tokens=250,
                    messages=[{"role": "user", "content": _open_prompt}],
                )
                _opening_msg = _open_resp.content[0].text.strip()

                # Store thread + session data; set planning flow
                _plan_thread_data = {
                    "week": week_num + 1,
                    "thread": [{"role": "assistant", "content": _opening_msg}],
                    "next_week_sessions": _sessions_data,
                }
                if dry_run:
                    print(f"\n[DRY RUN — Sunday planning opening]:\n{_opening_msg}")
                else:
                    _ucs_sun("WEEKLY_PLAN_THREAD", _json_sun.dumps(_plan_thread_data), "HIGH")
                    _stm_sun(_opening_msg)
                    _ucs_sun("CURRENT_FLOW", f"weekly_planning | {today} | week:{week_num + 1}", "MEDIUM")
                    print(f"  Sunday weekly planning conversation opened for Week {week_num + 1}.")
            except Exception as e:
                print(f"  Sunday weekly planning failed (non-fatal): {e}")

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
    week_num = _get_authoritative_week_num()
    print(f"[{today}] Running pre-session brief (Week {week_num})...")

    # Dedup: only send once per day
    coach_state = read_coach_state()
    last_brief = coach_state.get("LAST_BRIEF", {}).get("summary", "")
    if last_brief == str(today):
        print("  Brief: already sent today — skipping.")
        return

    # Check if there's an agreed daily plan from last night's conversation
    _daily_focus_raw = coach_state.get("DAILY_FOCUS", {}).get("summary", "") or \
                       coach_state.get("DAILY_FOCUS", {}).get("Summary", "")
    _daily_focus = None
    _checkin_flags: list = []
    if _daily_focus_raw:
        try:
            import json as _json_df
            _df = _json_df.loads(_daily_focus_raw)
            if _df.get("date") == str(today):
                _daily_focus = _df
                print(f"  Brief: DAILY_FOCUS found for today — {_df.get('session', '?')}")
            # Consume checkin_flags regardless of date match (yesterday's flags carry over)
            _checkin_flags = _df.get("checkin_flags", [])
            if _checkin_flags:
                print(f"  Brief: {len(_checkin_flags)} checkin flag(s) loaded from DAILY_FOCUS")
        except Exception:
            pass

    try:
        program_data = read_program_data(week_num=week_num, lookback=0)
    except Exception as e:
        print(f"  Brief: program load failed: {e}")
        return

    current_week = program_data.get("current_week", {})
    today_str = today.strftime("%A")

    # Sheet delta sync — runs before cascade so Coach State is fresh
    try:
        from sheet_sync import SheetSyncEngine
        from memory import read_lift_history, read_health_log as _rhl_brief, read_commands as _rc_brief
        _brief_days = current_week.get("days", [])
        _brief_cmds = _rc_brief()
        SheetSyncEngine().run_sync(
            week_num=week_num,
            current_week_days=_brief_days,
            health_log=_rhl_brief(limit=50),
            lift_history=read_lift_history(limit=50),
            commands=_brief_cmds,
            dry_run=dry_run,
        )
    except Exception as _e:
        print(f"  Sheet sync failed (non-fatal): {_e}")

    # Day-name matching: labels can be "DAY 1: Squat + Bench" (won't match "Monday")
    # or "MONDAY - Squat" (will match). Try day-name first; fall back to next undone session.
    today_sessions = [
        day for day in current_week.get("days", [])
        if today_str.lower() in day.get("label", "").lower()
    ]
    if not today_sessions:
        # Fallback: check WEEKLY_SCHEDULE to confirm today is a training day,
        # then use the next undone session of the week.
        weekly_schedule = coach_state.get("WEEKLY_SCHEDULE", {}).get("summary", "")
        today_abbrev = today.strftime("%a").lower()  # "mon", "tue", etc.
        is_training_today = (
            any(kw in weekly_schedule.lower() for kw in (today_abbrev, today_str.lower()))
            or not weekly_schedule  # no schedule known → try anyway
        )
        if is_training_today:
            all_undone = [
                day for day in current_week.get("days", [])
                if not any(ex.get("done") is True for ex in day.get("exercises", []))
            ]
            if all_undone:
                today_sessions = [all_undone[0]]
                print(f"  Brief: day-name fallback — using next undone session: {all_undone[0].get('label', '?')}")

    if not today_sessions:
        print("  Brief: no session found for today — skipping.")
        return

    # Check if session already done
    already_done = any(
        any(ex.get("done") for ex in day.get("exercises", []))
        for day in today_sessions
    )
    if already_done:
        print("  Brief: today's session already logged — skipping.")
        return

    # Build a concise session overview (all lifts with weights)
    session_lines = []
    for day in today_sessions:
        exercises = day.get("exercises", [])
        main_lifts = [ex for ex in exercises if ex.get("weight")]
        for ex in main_lifts[:5]:
            session_lines.append(
                f"  {ex.get('name', '?')}: {ex.get('weight', '?')} × {ex.get('sets_reps', '?')}"
            )

    # Open commitments
    open_commitments = read_commitments("OPEN")
    commitment_note = ""
    if open_commitments:
        commitment_note = " | ".join(c["Commitment"][:80] for c in open_commitments[:2])

    # Select contextually relevant cues for today's primary lift.
    # Also pull recent Telegram technique mentions to make cues more targeted.
    primary_lift = ""
    primary_lift_notes = ""
    for day in today_sessions:
        for ex in day.get("exercises", []):
            if ex.get("weight"):
                primary_lift = ex.get("name", "")
                primary_lift_notes = ex.get("notes", "") or ex.get("session_note", "") or ""
                break
        if primary_lift:
            break

    # Recent Telegram technique/form mentions — informs cue selection
    recent_tech_notes = ""
    try:
        from memory import read_telegram_log
        all_tg = read_telegram_log(limit=15)
        tech_keywords = ["form", "technique", "bar path", "depth", "shoulder", "elbow",
                         "knee", "back", "wrist", "grip", "arch", "setup", "felt", "moved"]
        tech_msgs = [
            m.get("Message", "")[:100]
            for m in all_tg[-10:]
            if m.get("Direction", "").upper() in ("IN", "ATHLETE")
            and any(kw in m.get("Message", "").lower() for kw in tech_keywords)
        ]
        if tech_msgs:
            recent_tech_notes = " / ".join(tech_msgs[-3:])
            # Pass to cue selector for context-aware selection
            primary_lift_notes = (primary_lift_notes + " " + recent_tech_notes).strip()
    except Exception:
        pass

    cues = _select_lift_cue(primary_lift, coach_state, primary_lift_notes) if primary_lift else []
    cue_text = "\n".join(f"  - {c}" for c in cues) if cues else "(use general principles)"

    # Session position (e.g. "Squat session #22 (Week 9 of 30)") and phase RPE target
    _brief_session_position = ""
    _brief_rpe_target = _get_phase_rpe_target(week_num, program_data.get("total_weeks", 30))
    try:
        from memory import read_lift_history as _rlh_brief_pos
        _brief_lh = _rlh_brief_pos(limit=200)
        if primary_lift:
            _brief_session_position = _get_session_position(
                primary_lift, _brief_lh, week_num, program_data.get("total_weeks", 30)
            )
    except Exception:
        pass
    _brief_session_pos_line = f"This is {_brief_session_position}." if _brief_session_position else ""

    # HEALTH_READINESS — load daily signal and apply RPE constraints
    _brief_readiness_text = ""
    try:
        import json as _json_br
        _raw_readiness_brief = coach_state.get("HEALTH_READINESS", {}).get("summary", "")
        if _raw_readiness_brief:
            _brief_readiness = _json_br.loads(_raw_readiness_brief)
            # Override phase RPE target if readiness constraint is stricter
            for _constraint in _brief_readiness.get("constraints", []):
                if _constraint.startswith("max_rpe:"):
                    try:
                        _constrained_rpe = _constraint.split(":")[1].strip()
                        _brief_rpe_target = f"RPE {_constrained_rpe} (readiness constraint)"
                    except (IndexError, ValueError):
                        pass
            # Build brief readiness line
            _br_score = _brief_readiness.get("readiness_score")
            _br_flags = _brief_readiness.get("flags", [])
            _br_insights = _brief_readiness.get("insights", [])
            _br_parts = []
            if _br_score is not None:
                _br_parts.append(f"Readiness {_br_score}/100")
            if _br_flags:
                _br_parts.append(f"flags: {', '.join(_br_flags[:2])}")
            if _br_insights:
                _br_parts.append(_br_insights[0])
            _brief_readiness_text = " | ".join(_br_parts) if _br_parts else ""
    except Exception:
        pass

    # Load extra context for cascade
    commands_for_cascade = []
    coach_focus_for_cascade = []
    try:
        from memory import read_commands, read_coach_focus
        commands_for_cascade = read_commands()
        coach_focus_for_cascade = read_coach_focus()
    except Exception:
        pass

    # Build cascade context (no projections — too expensive for brief)
    _brief_tg_log: list = []
    try:
        _brief_tg_log = all_tg  # already loaded above for tech notes
    except NameError:
        pass

    cascade = _build_cascade_context(
        coach_state=coach_state,
        commands=commands_for_cascade,
        projections=None,
        current_week_days=current_week.get("days", []),
        health_log=None,
        telegram_log=_brief_tg_log,
        coach_focus=coach_focus_for_cascade,
        today=today,
        orientation="today",
    )

    session_text = "\n".join(session_lines) or "session details unavailable"

    _agreed_plan_block = ""
    if _daily_focus:
        _agreed_plan_block = (
            f"\n=== AGREED PLAN FROM LAST NIGHT (athlete confirmed this) ===\n"
            f"Session: {_daily_focus.get('session', '?')}\n"
            f"Focus: {_daily_focus.get('focus', '')}\n"
            f"Adjustments: {_daily_focus.get('adjustments', 'none')}\n"
            f"Build on this — do NOT reopen scheduling or propose alternatives.\n"
        )

    _high_priority_flags = [f for f in _checkin_flags if f.get("priority") == "HIGH"]
    _checkin_block = ""
    if _high_priority_flags:
        _flag_lines = "\n".join(f"  - {f['message']}" for f in _high_priority_flags)
        _checkin_block = f"\n=== FOLLOW-UP REQUIRED FROM YESTERDAY ===\n{_flag_lines}\nAddress these briefly before the session focus.\n"

    prompt = f"""You are {ATHLETE_NAME}'s strength coach. Write a pre-session brief for today's training.

=== COACHING CONTEXT — WORK THROUGH ALL 4 LAYERS BEFORE WRITING ===
{cascade}
{_agreed_plan_block}{_checkin_block}
=== TODAY'S SESSION ===
Exercises:
{session_text}

Session position: {_brief_session_pos_line or '(unknown)'}
Phase RPE target: {_brief_rpe_target}
{f"Readiness signal: {_brief_readiness_text}" if _brief_readiness_text else ""}

TECHNIQUE CUE OPTIONS FOR {primary_lift.upper() or "TODAY'S MAIN LIFT"} (choose ONE or skip):
{cue_text}
{f"RECENT ATHLETE TECHNIQUE MENTIONS: {recent_tech_notes}" if recent_tech_notes else ""}
{f"OPEN COMMITMENT: {commitment_note}" if commitment_note else ""}

=== COACHING TONE DIRECTIVE ===
Write this as a pre-session coaching brief — warm, direct, personal. English. No greeting, no sign-off.

The message MUST include ALL of the following in flowing prose (no bullet points, no headers):

1. WHY TODAY: One sentence connecting today's session to the program arc. Draw from Layer 2
   (WEEKLY_INTENT, COACHING_REASON). Example: "This is the intensity peak of your accumulation block —
   today's sets build the foundation for the heavier loads coming in weeks 12-14."

2. SESSION POSITION + TARGET: State {_brief_session_pos_line or "the session position"}.
   Name the key lift, weight, and sets. State RPE target: {_brief_rpe_target}.
   If it might hit RPE 9+ on set 3, name the drop weight explicitly.

3. ONE CUE: The single most important cue from the options above. One thing only.

4. SPECIFIC FEEDBACK ASK: A precise question that gives coaching data — not "how did it go?"
   Example: "Tell me how the last two sets felt — that's my calibration signal."

REASONING RULE: At least one sentence must follow: "I see [data] — this means [X] — [action]."
If Layer 3 shows a CONFLICT, address it directly. If last night's commitment matches today, build on it.
English. 3–5 sentences MAX. Every brief must feel different from the last.

HARD RULE: NEVER ask scheduling questions. Never ask "when can you train?" or "what day works?".
You have the schedule — make a decision and tell the athlete what to do. Propose, don't ask."""

    # Enforce athlete output preferences as hard constraints
    try:
        from memory import read_athlete_preferences
        from prompt import apply_output_preferences
        prompt = apply_output_preferences(prompt, read_athlete_preferences())
    except Exception:
        pass

    if dry_run:
        print(f"  [DRY RUN] Brief would cover: {session_text[:80]}")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=320,
            messages=[{"role": "user", "content": prompt}],
        )
        brief_msg = result.content[0].text.strip()

        from telegram_utils import send_telegram_message
        sent = send_telegram_message(brief_msg)
        if sent:
            print(f"  Brief sent: {brief_msg[:80]}")
            upsert_coach_state("LAST_BRIEF", str(today), "HIGH")
            # Clear any stale CURRENT_FLOW from a previous session — new day, fresh start
            upsert_coach_state("CURRENT_FLOW", "", "LOW")
            # Store full brief content so subsequent messages can verify consistency (Layer 4)
            upsert_coach_state(
                "LAST_BRIEF_CONTENT",
                f"{today} | {brief_msg[:400].replace(chr(10), ' ')}",
                "HIGH",
            )
            # Write DAILY_FOCUS so post-session and evening-protocol can reference today's emphasis
            daily_focus = (
                f"{today} | lift:{primary_lift} | "
                f"intent:{brief_msg[:150].replace(chr(10), ' ')}"
            )
            upsert_coach_state("DAILY_FOCUS", daily_focus, "HIGH")
            # Clear consumed checkin_flags so they don't repeat tomorrow
            if _checkin_flags:
                try:
                    import json as _json_cf_clear
                    _df_cf_clear_raw = coach_state.get("DAILY_FOCUS", {}).get("summary", "") or \
                                      coach_state.get("DAILY_FOCUS", {}).get("Summary", "")
                    if _df_cf_clear_raw and _df_cf_clear_raw.strip().startswith("{"):
                        _df_cf_clear = _json_cf_clear.loads(_df_cf_clear_raw)
                        _df_cf_clear.pop("checkin_flags", None)
                        from memory import write_single_summary as _wss_cf
                        _wss_cf("DAILY_FOCUS", _df_cf_clear)
                        print("  Brief: checkin_flags cleared from DAILY_FOCUS.")
                except Exception:
                    pass
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
    from memory import read_coach_state, upsert_coach_state, read_commitments

    today = date.today()
    week_num = _get_authoritative_week_num()
    print(f"[{today}] Running post-session check-in (Week {week_num})...")

    from memory import read_telegram_log

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

    # Sheet delta sync — key here: marks session Done, resolves PENDING_CATCHUPs
    try:
        from sheet_sync import SheetSyncEngine
        from memory import read_lift_history, read_health_log as _rhl_ps, read_commands as _rc_ps
        SheetSyncEngine().run_sync(
            week_num=week_num,
            current_week_days=current_week.get("days", []),
            health_log=_rhl_ps(limit=50),
            lift_history=read_lift_history(limit=50),
            commands=_rc_ps(),
            dry_run=dry_run,
        )
    except Exception as _e:
        print(f"  Sheet sync failed (non-fatal): {_e}")

    today_sessions = [
        day for day in current_week.get("days", [])
        if today_str.lower() in day.get("label", "").lower()
    ]

    if not today_sessions:
        print("  Post-session: no session scheduled today — skipping.")
        return

    session_label = today_sessions[0].get("label", "today's session")
    exercises = today_sessions[0].get("exercises", [])
    done_exs = [ex for ex in exercises if ex.get("done") is True]
    skip_exs = [ex for ex in exercises if ex.get("done") is False and ex.get("name")]
    total_count = len([ex for ex in exercises if ex.get("name")])
    done_count = len(done_exs)

    # Check if session has RPE logged already (so we don't ask redundantly)
    has_rpe = any(
        _re_rpe.search((ex.get("session_note") or ex.get("notes") or ""))
        for ex in done_exs
    )

    # Today's Telegram — avoid repeating what was already discussed
    today_tg_raw: list[dict] = []
    try:
        all_tg = read_telegram_log(limit=10)
        today_tg_raw = [m for m in all_tg if m.get("Date", "") == str(today)]
        today_tg_text = "\n".join(
            f"  [{m.get('Direction','')}] {m.get('Message','')[:100]}"
            for m in today_tg_raw[-6:]
        ) or "(none)"
    except Exception:
        today_tg_text = "(unavailable)"

    # Smart dedup: check if athlete already covered RPE or skip reasons in today's Telegram
    tg_covers_rpe = _today_telegram_covers_topic(
        ["rpe", "felt like", "left in tank", "rir", "rate of perceived", "exertion"],
        today_tg_raw, str(today)
    ) or has_rpe
    tg_covers_skips = _today_telegram_covers_topic(
        ["skipped", "didn't do", "couldn't", "missed", "no rows", "no accessory",
         "left it out", "cut it short", "didn't finish"],
        today_tg_raw, str(today)
    )
    tg_covers_session = _today_telegram_covers_topic(
        ["session done", "finished", "completed", "all done", "wrapped up",
         "trained", "hit the gym", "workout done"],
        today_tg_raw, str(today)
    )

    weekly_intent = coach_state.get("WEEKLY_INTENT", {}).get("summary", "")

    # DAILY_FOCUS: what today's brief emphasized — avoid contradicting it
    daily_focus_raw = coach_state.get("DAILY_FOCUS", {}).get("summary", "")
    daily_focus_today = ""
    if daily_focus_raw and daily_focus_raw.startswith(str(today)):
        daily_focus_today = daily_focus_raw[len(str(today)) + 3:]  # strip "DATE | "

    # Open commitments due today or soon — surface in post-session if relevant
    try:
        open_commits = read_commitments("OPEN")
        post_commitment_note = " | ".join(c["Commitment"][:80] for c in open_commits[:2]) if open_commits else ""
    except Exception:
        post_commitment_note = ""

    # Build ordered exercise context with muscle group labels and fatigue notes
    ordered_ex_text, fatigue_note_text = _build_ordered_exercise_context(exercises)

    # Build context for Haiku
    skipped_names = [ex.get("name", "") for ex in skip_exs if ex.get("name")][:4]

    any_complete = done_count > 0

    if any_complete:
        # Adjust what the coach asks based on what's already been discussed today
        rpe_instruction = (
            "3. RPE already discussed today — don't ask again. Instead, give one forward-looking note."
            if tg_covers_rpe else
            "3. If no RPE logged, ask for it: \"RPE on the main sets? Helps me calibrate next week.\""
        )
        skip_instruction = (
            "2. Skip reason already discussed today — acknowledge it briefly instead of asking again."
            if (tg_covers_skips and skipped_names) else
            ("2. If exercises were skipped, ask specifically WHY — not accusatorially, with curiosity.\n"
             '   "You skipped the rows — was it time, the elbow, or just done?"')
        )
        session_ack = (
            "The athlete has already reported the session in Telegram — acknowledge briefly, don't recap what they said."
            if tg_covers_session else
            f"The athlete completed {session_label}: {done_count}/{total_count} exercises done."
        )

        prompt = f"""You are {ATHLETE_NAME}'s strength coach. Write a post-session Telegram message (3-5 sentences).

{session_ack}

SESSION ORDER (fatigue accumulates top to bottom — reason about this when interpreting performance):
{ordered_ex_text}
{fatigue_note_text}

WHAT THE MESSAGE MUST DO:
1. Acknowledge the session briefly and specifically (name the key lift done, not just "good session").
{skip_instruction}
{rpe_instruction}
4. One forward-looking note: how today connects to the weekly goal or next session.

CONTEXT:
Weekly intent: {weekly_intent or '(not set)'}
{f"Today's pre-session brief focused on: {daily_focus_today}" if daily_focus_today else ""}
Today's Telegram conversation: {today_tg_text}
{f"OPEN COMMITMENT (follow up if relevant): {post_commitment_note}" if post_commitment_note else ""}

Rules: No greeting, no sign-off. Reference actual data (weights, exercises). Don't repeat today's conversation."""
    else:
        prompt = f"""You are {ATHLETE_NAME}'s strength coach. Write a warm post-session check-in (2-3 sentences).

The athlete had {session_label} scheduled today but nothing is logged yet.
This is NOT a nudge or guilt trip — it's genuine curiosity.

WHAT THE MESSAGE MUST DO:
1. Ask specifically about today's session — "Did {session_label} happen today?"
2. Mention one specific thing from today's program (a key lift) to show you're paying attention.
3. Make it easy to respond: "Even a quick yes/no helps — I track everything."

CONTEXT:
Today's Telegram conversation (do not repeat): {today_tg_text}
Weekly intent: {weekly_intent or '(not set)'}

Rules: No greeting, no sign-off. Human and specific."""

    # Enforce athlete output preferences as hard constraints
    try:
        from memory import read_athlete_preferences
        from prompt import apply_output_preferences
        prompt = apply_output_preferences(prompt, read_athlete_preferences())
    except Exception:
        pass

    if dry_run:
        print(f"  [DRY RUN] Post-session: {done_count}/{total_count} done, "
              f"skipped: {skipped_names}, rpe: {has_rpe}")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = result.content[0].text.strip()

        from telegram_utils import send_telegram_message
        sent = send_telegram_message(msg)
        if sent:
            print(f"  Post-session check-in sent: {msg[:80]}")
            upsert_coach_state("LAST_POST_SESSION", str(today), "HIGH")
    except Exception as e:
        print(f"  Post-session check-in failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# End-session protocol: athlete-triggered structured check-in
# ---------------------------------------------------------------------------

def run_endsession_protocol(user_message: str = "", dry_run: bool = False) -> str:
    """
    Called when athlete signals session end — via /endsession command or NL detection.

    Flow:
    1. Dedup check (LAST_POST_SESSION == today) → short ack if already ran
    2. Load today's program session + read done/actual state from sheet
    3. Build 4-layer cascade context (orientation="session_end")
    4. Build ordered exercise list with muscle group labels
    5. Parse user_message for already-reported info (don't re-ask)
    6. Detect multi-session (AM/PM keywords) → keep day open if partial
    7. Ask targeted questions: RPE for completed lifts, skip reasoning with fatigue chain
    8. Write LAST_POST_SESSION to prevent cron duplicate

    Returns the response string (also sends via Telegram if not dry_run).
    """
    from sheets import read_program_data
    from memory import (
        read_coach_state, upsert_coach_state, read_commands,
        read_coach_focus, read_telegram_log, read_health_log,
    )

    today = date.today()
    week_num = _get_authoritative_week_num()
    print(f"[{today}] Running endsession protocol (Week {week_num})...")

    coach_state = read_coach_state()

    # Dedup: if post-session already ran today, return short ack
    last_post = coach_state.get("LAST_POST_SESSION", {}).get("summary", "")
    if last_post == str(today):
        return "Already logged today's session check-in — anything to add or correct?"

    # Load program data
    try:
        program_data = read_program_data(week_num=week_num, lookback=0)
    except Exception as e:
        print(f"  EndSession: program load failed: {e}")
        return "Couldn't load the program right now — send me the details directly."

    current_week = program_data.get("current_week", {})
    today_str = today.strftime("%A")
    today_sessions = [
        day for day in current_week.get("days", [])
        if today_str.lower() in day.get("label", "").lower()
    ]

    # Fallback: use TOMORROW_PLAN label if no day-name match
    if not today_sessions:
        tomorrow_plan = coach_state.get("TOMORROW_PLAN", {}).get("summary", "")
        last_evening = coach_state.get("LAST_EVENING_PROTOCOL", {}).get("summary", "")
        yesterday = str(today - timedelta(days=1))
        if tomorrow_plan and last_evening == yesterday:
            plan_label = tomorrow_plan.split(" | ")[0].strip()
            today_sessions = [
                day for day in current_week.get("days", [])
                if plan_label.lower() in day.get("label", "").lower()
            ]

    # Last resort: most recently logged session (has done exercises)
    if not today_sessions:
        logged = [
            day for day in current_week.get("days", [])
            if any(ex.get("done") is True for ex in day.get("exercises", []))
        ]
        if logged:
            today_sessions = [logged[-1]]  # most recently logged

    # Final fallback: next undone session
    if not today_sessions:
        undone = [
            day for day in current_week.get("days", [])
            if not any(ex.get("done") is True for ex in day.get("exercises", []))
        ]
        if undone:
            today_sessions = [undone[0]]

    if not today_sessions:
        return "Couldn't find today's session — what did you do? Send me the details."

    session = today_sessions[0]
    session_label = session.get("label", "today's session")
    exercises = session.get("exercises", [])

    # --- Layer 1: Detect session delta and show it upfront ---
    session_delta = _detect_session_delta(program_data, coach_state)
    delta_display = _format_delta_for_athlete(session_delta, session)
    print(f"  EndSession delta: {len(session_delta.get('new_sessions_done', []))} new, "
          f"{len(session_delta.get('retroactive_changes', []))} retro changes")

    # Build ordered exercise context with fatigue chain notes
    ordered_ex_text, fatigue_note_text = _build_ordered_exercise_context(exercises)

    # Detect multi-session (AM / PM keywords)
    msg_lower = user_message.lower()
    is_partial_day = any(
        kw in msg_lower
        for kw in ("am ", "morning", "mañana", "primera sesión", "primera sesion",
                   "first session", "am session", "entreno de mañana")
    )

    # Parse user_message for already-reported info (to avoid re-asking)
    already_reported_parts = []
    if any(kw in msg_lower for kw in ("rpe", "rir", "felt like", "@")):
        already_reported_parts.append("RPE/exertion level")
    if any(kw in msg_lower for kw in ("skipped", "didn't do", "no hice", "dejé", "sin bench",
                                       "sin row", "no rows")):
        already_reported_parts.append("skip explanations")
    # Extract lift names mentioned
    known_lifts = ["squat", "bench", "deadlift", "press", "row", "lunge", "dip", "nordic",
                   "pullup", "curl", "sentadilla", "press banca", "peso muerto"]
    mentioned_lifts = [lift for lift in known_lifts if lift in msg_lower]
    if mentioned_lifts:
        already_reported_parts.append(f"mentioned: {', '.join(mentioned_lifts)}")
    already_reported_text = (
        " | ".join(already_reported_parts) if already_reported_parts else "nothing yet"
    )

    # Load context for cascade
    commands_list = []
    coach_focus_list = []
    health_log_list = []
    tg_log_list = []
    try:
        commands_list = read_commands()
        coach_focus_list = read_coach_focus()
        health_log_list = read_health_log(limit=1)
        tg_log_list = read_telegram_log(limit=12)
    except Exception:
        pass

    cascade = _build_cascade_context(
        coach_state=coach_state,
        commands=commands_list,
        projections=None,
        current_week_days=current_week.get("days", []),
        health_log=health_log_list,
        telegram_log=tg_log_list,
        coach_focus=coach_focus_list,
        today=today,
        orientation="session_end",
    )

    # Determine done/skipped counts for strategic framing
    done_exs = [ex for ex in exercises if ex.get("done") is True]
    total_named = len([ex for ex in exercises if ex.get("name")])

    prompt = f"""You are {ATHLETE_NAME}'s strength coach. The athlete just finished a session. Write a check-in message.

=== COACHING CONTEXT ===

{cascade}

=== SESSION JUST COMPLETED: {session_label} ===
({len(done_exs)}/{total_named} exercises completed)

{"=== WHAT I SEE IN THE SHEET (show this to athlete FIRST, before any questions) ===" if delta_display else ""}
{delta_display if delta_display else ""}

SESSION ORDER (fatigue accumulates top to bottom — use this to interpret performance):
{ordered_ex_text}

{fatigue_note_text}

=== WHAT THE ATHLETE ALREADY REPORTED ===
{already_reported_text}

{"=== PARTIAL DAY NOTE ===" if is_partial_day else ""}
{"This appears to be an AM/first session — do NOT assume the day is over. Ask if there's a PM session." if is_partial_day else ""}

=== WHAT TO ASK (priority order) ===
{"0. START by showing the delta block above verbatim — let the athlete confirm or correct before anything else." if delta_display else ""}
1. RPE for each COMPLETED main lift (one question per lift) — SKIP if already reported above.
2. For each SKIPPED exercise: ask why specifically — reference the fatigue chain notes above.
   E.g.: "You skipped bench [push/chest, pos 2] — your chest was fresh when you got to dips [pos 4];
   dips being easy makes sense. What happened with bench?"
3. If retroactive changes detected: ask athlete to confirm the new value is correct.
4. If Layer 3 shows a pending catch-up, ask if it needs rescheduling.
5. If partial day: ask if there's a PM session planned.

=== RULES ===
- Max 3–4 questions total, grouped naturally.
- Reference actual exercise names, positions, muscle groups from the ordered list above.
- Direct coach voice. No sign-off.
- Do NOT re-ask anything already in WHAT THE ATHLETE ALREADY REPORTED.
- Write in Spanish (the athlete's primary language).
- NARRACIÓN OBLIGATORIA: Al menos UNA frase debe mostrar tu razonamiento.
  Plantilla: Veo que [dato] — esto [qué significa] — [pregunta o recomendación concreta]."""

    # Enforce athlete output preferences
    try:
        from memory import read_athlete_preferences
        from prompt import apply_output_preferences
        prompt = apply_output_preferences(prompt, read_athlete_preferences())
    except Exception:
        pass

    if dry_run:
        print(f"  [DRY RUN] EndSession: {session_label} | done={len(done_exs)}/{total_named}")
        return f"[DRY RUN] EndSession check-in for {session_label}"

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        response = result.content[0].text.strip()

        from telegram_utils import send_telegram_message
        sent = send_telegram_message(response)
        if sent:
            print(f"  EndSession check-in sent: {response[:80]}")
            upsert_coach_state("LAST_POST_SESSION", str(today), "HIGH")
            # Track what was asked so the bot can continue this conversation if athlete replies
            _skipped = [ex['name'] for ex in exercises if ex.get('done') is False and ex.get('name')]
            _retro = session_delta.get("retroactive_changes", [])
            _done_names = [ex['name'] for ex in done_exs if ex.get('name')]
            _questions_summary = (
                f"RPE for {', '.join(_done_names[:8])}" if _done_names
                else f"RPE for completed lifts in {session_label}"
            )
            _questions_summary += (
                (f"; skip reasons for {', '.join(_skipped)[:60]}" if _skipped else "")
                + (f"; retroactive changes: {len(_retro)}" if _retro else "")
                + ("; delta shown to athlete" if delta_display else "")
            )
            upsert_coach_state(
                "CURRENT_FLOW",
                f"endsession | {today} | {session_label} | asked: {_questions_summary[:200]}",
                "HIGH",
            )
            # Write new snapshot after showing the delta
            if session_delta.get("snapshot_updated"):
                upsert_coach_state(
                    "LAST_PROGRAM_SNAPSHOT",
                    session_delta.get("current_snapshot_json", ""),
                    "HIGH",
                )

            # --- Part 3 Fix A: Immediate escalation check ---
            # Check user message for injury/goal-change keywords — fire cascade if triggered
            _esc = _check_escalation_from_message(user_message)
            if _esc:
                try:
                    _fire_immediate_escalation(_esc, session_label, dry_run=dry_run)
                except Exception as _esc_err:
                    print(f"  [Escalation] Failed (non-fatal): {_esc_err}")

        return response

    except Exception as e:
        print(f"  EndSession failed (non-fatal): {e}")
        return "Something went wrong generating the check-in — send me the details directly."


def _check_escalation_from_message(user_message: str, _coach_state: dict = None) -> dict | None:
    """
    Check if a user message contains SEVERE escalation-triggering keywords.
    Casual mentions of pain/elbow/soreness do NOT trigger escalation — only
    explicit, structural-damage signals do (torn, can't train, doctor, surgery, etc.).

    Casual mentions (INJURY_WATCH_KEYWORDS) are handled by the coach's Q&A
    in endsession — not by triggering a cascade replanning.

    Returns escalation context dict or None.
    """
    from cascade_levels import INJURY_ESCALATION_KEYWORDS, GOAL_CHANGE_KEYWORDS
    text = user_message.lower()
    # Only escalate on severe/explicit injury language
    for kw in INJURY_ESCALATION_KEYWORDS:
        if kw in text:
            return {"type": "injury", "disruption": "injury", "context": {"keyword": kw}}
    for kw in GOAL_CHANGE_KEYWORDS:
        if kw in text:
            return {"type": "goal_change", "disruption": "goal_change", "context": {"keyword": kw}}
    return None


def _fire_immediate_escalation(
    escalation_ctx: dict,
    session_label: str,
    dry_run: bool = False,
) -> None:
    """
    Fix A: After endsession, immediately fire escalation:
    1. Send bridge message to athlete ("I'm flagging a concern...")
    2. Call appropriate cascade level with escalation_context
    Called only when escalation detected in endsession message.
    """
    from cascade_state import initiate_escalation, classify_disruption
    from telegram_utils import send_telegram_message

    disruption_type = escalation_ctx.get("disruption", "injury")
    esc_type = escalation_ctx.get("type", "unknown")
    keyword = escalation_ctx.get("context", {}).get("keyword", "")

    # Decide which cascade level to call
    target_level = classify_disruption(disruption_type)

    # Bridge message — let athlete know reasoning is happening
    type_display = {
        "injury": "a potential injury concern",
        "goal_change": "a goal change",
        "sessions_skipped": "multiple missed sessions",
    }.get(esc_type, "a concern")
    bridge = (
        f"I'm flagging {type_display} ('{keyword}' in your message). "
        f"Before I close today, I want to reason through the impact on your program. "
        f"Give me a moment."
    )
    print(f"  [Escalation] Firing immediate escalation: {esc_type} → {target_level}")
    if not dry_run:
        try:
            send_telegram_message(bridge)
        except Exception as e:
            print(f"  [Escalation] Bridge message failed: {e}")

    # Escalation context for cascade level
    full_ctx = {
        **escalation_ctx,
        "session_label": session_label,
    }

    # Lock levels + create snapshot
    if not dry_run:
        try:
            initiate_escalation(disruption_type, full_ctx)
        except Exception as e:
            print(f"  [Escalation] initiate_escalation failed: {e}")

    # Call cascade level with escalation_context
    if not dry_run:
        try:
            if target_level in ("ANNUAL", "LONGTERM"):
                from cascade_levels import annual_eval
                annual_eval(dry_run=False, escalation_context=full_ctx)
            elif target_level == "MONTHLY":
                from cascade_levels import monthly_eval
                monthly_eval(dry_run=False, escalation_context=full_ctx)
            # WEEKLY is handled at close_day() — no immediate action needed
        except Exception as e:
            print(f"  [Escalation] Cascade level call failed: {e}")
    else:
        print(f"  [DRY RUN] Would call {target_level} with escalation_context={full_ctx}")


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
    week_num = _get_authoritative_week_num()
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
    week_num = _get_authoritative_week_num()
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
            model=CLAUDE_HAIKU,
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

    # --- Generate WEEKLY_INTENT for the week ---
    # A short coaching summary of what this week is trying to achieve.
    # Written to Coach State so every daily message can reference it.
    try:
        coach_state = read_coach_state()
        program_summary = coach_state.get("PROGRAM", {}).get("summary", "")
        lift_states_text = "\n".join(
            f"  {dom}: {coach_state.get(dom, {}).get('summary', '')[:100]}"
            for dom in ("SQUAT", "BENCH", "DEADLIFT", "OHP")
            if coach_state.get(dom, {}).get("summary", "")
        )
        annual_arc = coach_state.get("ANNUAL_ARC", {}).get("summary", "")[:200]

        intent_prompt = (
            f"You are a strength coach writing a WEEKLY INTENT summary for Week {week_num}.\n\n"
            f"This is 2-3 sentences that will be referenced in every daily Telegram message this week. "
            f"It should explain: what the week is trying to achieve, where we are in the training block, "
            f"and one specific thing to watch or prioritize (an injury, a key lift, a recovery focus).\n\n"
            f"PROGRAM: {program_summary}\n"
            f"LIFT STATE:\n{lift_states_text}\n"
            f"ANNUAL ARC: {annual_arc or '(not set)'}\n"
            f"SESSIONS THIS WEEK: {sessions_text}\n\n"
            f"Output just the 2-3 sentence intent. Direct, specific, coaching language. "
            f"Example: 'Week 10 is a volume accumulation week — the goal is to hit 97.5kg squat "
            f"and push bench above 87.5kg across all working sets. These next 4 weeks build the "
            f"base for the Week 14 intensity peak. Keep rows at 3x8 maximum — elbow is a concern.'\n\n"
            f"Write it now:"
        )
        intent_result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=150,
            messages=[{"role": "user", "content": intent_prompt}],
        )
        intent_text = intent_result.content[0].text.strip()
        upsert_coach_state("WEEKLY_INTENT", intent_text, "HIGH")
        print(f"  WEEKLY_INTENT written: {intent_text[:80]}")

        # Generate COACHING_REASON — one sentence explaining the training science behind this week.
        # This is injected into every brief and email so the athlete understands WHY, not just WHAT.
        reason_prompt = (
            f"You are a strength coach. In ONE sentence, explain the training science principle "
            f"behind this week's programming. Focus on WHY the volume/intensity/structure matters "
            f"right now — what adaptation are we targeting, and why this timing?\n\n"
            f"PROGRAM: {program_summary}\n"
            f"LIFT STATE:\n{lift_states_text}\n"
            f"ANNUAL ARC: {annual_arc or '(not set)'}\n"
            f"WEEKLY INTENT: {intent_text}\n\n"
            f"Output just one sentence. Examples:\n"
            f"'We're using higher reps this week to accumulate volume that will translate to "
            f"heavier singles in the peak block four weeks from now.'\n"
            f"'This deload is timed specifically — the body supercompensates after reduced load, "
            f"so next week's PRs are built this week, not next.'\n"
            f"Write it now:"
        )
        reason_result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=80,
            messages=[{"role": "user", "content": reason_prompt}],
        )
        reason_text = reason_result.content[0].text.strip()
        upsert_coach_state("COACHING_REASON", reason_text, "HIGH")
        print(f"  COACHING_REASON written: {reason_text[:80]}")
    except Exception as e:
        print(f"  WEEKLY_INTENT generation failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Evening protocol: plan check + TOMORROW_PLAN for bot to use
# ---------------------------------------------------------------------------

def _is_on_vacation(life_context: list[dict],
                    recent_telegram: list[dict] = None) -> bool:
    """
    Return True if the most recent vacation-related Life Context entry indicates
    an ACTIVE vacation (not a return announcement or a stale mention).

    Logic (newest-first):
    1. If recent Telegram messages (last 2 days) contain training signals → False
       (athlete described a workout → clearly not on vacation, regardless of Life Context)
    2. If the entry has a return signal ("back from", "returned", etc.) → False
    3. If the entry is > 14 days old → treat as stale/expired → False
    4. Otherwise → True (active vacation)
    """
    vacation_keywords = ("vacation", "holiday", "vacaciones", "holidays", "de vacaciones")
    return_keywords = (
        "back from", "returned from", "back home", "de vuelta",
        "regresé", "already back", "got back", "came back", "just back",
        "back to training", "resuming", "volviendo", "retomando", "vuelta al gym",
        "vuelta al entreno", "I'm back", "estoy de vuelta",
    )
    # Signals that clearly indicate the athlete is actively training
    training_signals = (
        "squat", "bench", "deadlift", "press", "workout", "session", "trained",
        "gym", "lift", "set", "rep", " kg", " lbs", "entrenamiento", "entrené",
        "sesión", "pesas", "cardio", "corr", "ran ", "rows", "pull",
    )

    today = date.today()

    # 1. Override: if athlete mentioned training in last 2 days via Telegram → not on vacation
    if recent_telegram:
        two_days_ago = str(today - timedelta(days=2))
        for msg in recent_telegram:
            if msg.get("Direction", "").upper() != "IN":
                continue  # only check inbound (athlete) messages
            if msg.get("Date", "") < two_days_ago:
                continue
            text = msg.get("Message", "").lower()
            if any(k in text for k in training_signals):
                return False  # athlete described training → definitely not on vacation

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


def _should_suggest_challenge(coach_state: dict, projections: dict,
                               program_data: dict) -> dict | None:
    """
    Evaluate whether the coach should suggest a fun variation or challenge.
    Returns a dict {type, description, reason} if a challenge is appropriate, else None.

    ONLY suggests when:
    - TSB is positive (athlete is NOT fatigued)
    - Not in peak/deload week
    - Last challenge was 14+ days ago (stored in LAST_CHALLENGE Coach State)
    - A specific motivational trigger is present (see below)

    This is NOT a scheduled feature. It fires rarely, only when it makes sense.
    """
    import json as _json

    # 1. Check fatigue — never suggest if TSB is negative
    fatigue = projections.get("fatigue", {}) if projections else {}
    tsb = fatigue.get("TSB", 0)
    if tsb < 0:
        return None

    # 2. Check phase — no challenges during peak or deload weeks
    current_week_data = program_data.get("current_week", {})
    week_label = current_week_data.get("label", "").lower()
    if any(k in week_label for k in ("peak", "deload", "taper", "test")):
        return None

    # 3. Check cooldown — at least 14 days since last challenge
    last_challenge_str = coach_state.get("LAST_CHALLENGE", {}).get("summary", "")
    if last_challenge_str:
        try:
            last_ch = date.fromisoformat(last_challenge_str[:10])
            if (date.today() - last_ch).days < 14:
                return None
        except (ValueError, TypeError):
            pass

    # 4. Look for motivational triggers
    session_quality = coach_state.get("SESSION_QUALITY", {}).get("summary", "").lower()

    # Trigger A: Multiple consecutive good sessions → athlete is hot, suggest a test
    if "100%" in session_quality or ("strong" in session_quality and "consecutive" in session_quality):
        # 1RM test — only for a main lift that hasn't been tested in a while
        for domain in ("SQUAT", "BENCH", "DEADLIFT", "OHP"):
            state = coach_state.get(domain, {}).get("summary", "")
            if state and "on track" in state.lower():
                lift_name = domain.replace("OHP", "Overhead Press")
                return {
                    "type": "1rm_test",
                    "lift": lift_name,
                    "description": (
                        f"After your work sets on {lift_name.lower()}, try one all-out single "
                        f"attempt. Warm up properly, then go for a heavy single — see where you actually are."
                    ),
                    "reason": "Consecutive solid sessions + positive TSB. Good time to test actual strength.",
                    "cost": "~5 min. No recovery cost if you leave 1 RIR on the single.",
                }

    # Trigger B: Energy seems high but week has been light (many sessions done early)
    done_count = sum(
        1 for day in current_week_data.get("days", [])
        if any(ex.get("done") for ex in day.get("exercises", []))
    )
    total_count = len(current_week_data.get("days", []))
    if total_count > 0 and done_count >= total_count and tsb > 5:
        return {
            "type": "movement_variety",
            "description": (
                "You've hit all your sessions this week and TSB is positive. "
                "Consider adding 2-3 sets of a movement you haven't done in a while — "
                "good mornings, pause squats, tempo bench. Expose a weakness, stay fresh."
            ),
            "reason": "Week fully complete, body fresh. Good time to explore variation.",
            "cost": "Optional add-on. 10-15 min. No impact on next week.",
        }

    return None


def run_evening_protocol(dry_run: bool = False):
    """
    Evening protocol (19:00 UTC = 20:00 Spain).

    Sends a Telegram message that:
    1. References the weekly intent and why tomorrow's session matters
    2. Names tomorrow's session and key lifts with targets
    3. Includes a proactive health/recovery insight (sleep, nutrition, injury management)
    4. Asks one specific question about readiness or today's session
    5. Optionally surfaces a challenge suggestion if conditions are right

    The full training protocol (Message 2) is generated by the Railway bot when athlete replies.
    Deduped via LAST_EVENING_PROTOCOL Coach State domain.
    """
    from sheets import read_program_data
    from memory import read_coach_state, upsert_coach_state, read_life_context, read_telegram_log, read_health_log

    today = date.today()
    tomorrow = today + timedelta(days=1)
    week_num = _get_authoritative_week_num()
    print(f"[{today}] Running evening protocol (Week {week_num})...")

    # Dedup: only send once per day
    coach_state = read_coach_state()
    last_evening = coach_state.get("LAST_EVENING_PROTOCOL", {}).get("summary", "")
    if last_evening == str(today):
        print("  Evening protocol: already sent today — skipping.")
        return

    # Vacation check — pass recent Telegram log so training signals override stale Life Context
    try:
        life_context = read_life_context(limit=10)
        recent_tg = read_telegram_log(limit=20)
        if _is_on_vacation(life_context, recent_telegram=recent_tg):
            vacation_msg = (
                "You're on vacation — enjoy it. Try to walk 30-45 min if you feel like it. "
                "Eat well tonight: protein + vegetables, easy on the carbs late. "
                "Check in when you're back and we'll plan the first session."
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

    # Sheet delta sync — detect Done changes, resolve PENDING_CATCHUPs, refresh Coach State
    _ep_commands: list = []
    try:
        from sheet_sync import SheetSyncEngine
        from memory import read_lift_history, read_health_log as _rhl_ep, read_commands as _rc_ep
        _ep_commands = _rc_ep()
        _ep_sync = SheetSyncEngine().run_sync(
            week_num=week_num,
            current_week_days=days,
            health_log=_rhl_ep(limit=50),
            lift_history=read_lift_history(limit=50),
            commands=_ep_commands,
            dry_run=dry_run,
        )
        if _ep_sync.get("resolved_catchups"):
            _ep_commands = _rc_ep()  # reload so cascade sees resolved state
    except Exception as _e:
        print(f"  Sheet sync failed (non-fatal): {_e}")

    # Find next incomplete session
    next_session = None
    for day in days:
        exercises = day.get("exercises", [])
        done_count = sum(1 for ex in exercises if ex.get("done") is True)
        if done_count == 0 and exercises:
            next_session = day
            break

    # If no incomplete session in current week, try next week
    # On Sundays: always look at next week Day 1 so Monday brief is always sent
    if not next_session and (tomorrow.weekday() == 0 or today.weekday() == 6):
        try:
            next_week_data = read_program_data(week_num=week_num + 1, lookback=0)
            next_week_days = next_week_data.get("current_week", {}).get("days", [])
            if next_week_days:
                next_session = next_week_days[0]
                print(f"  Evening protocol: Sunday mode — targeting Week {week_num + 1} Day 1.")
        except Exception:
            pass

    # Check WEEKLY_SCHEDULE: is tomorrow explicitly a rest day?
    tomorrow_name = tomorrow.strftime("%a")  # e.g. "Mon", "Tue"
    tomorrow_full = tomorrow.strftime("%A")  # e.g. "Monday"
    weekly_schedule_raw = coach_state.get("WEEKLY_SCHEDULE", {}).get("summary", "")
    _tomorrow_is_rest = False
    _tomorrow_session_time = ""
    if weekly_schedule_raw:
        try:
            import json as _json_ep
            _ws = _json_ep.loads(weekly_schedule_raw) if weekly_schedule_raw.strip().startswith("{") else {}
            _tm_entry = _ws.get(tomorrow_name) or _ws.get(tomorrow_full)
            if _tm_entry == "rest" or (isinstance(_tm_entry, dict) and _tm_entry.get("rest")):
                _tomorrow_is_rest = True
            elif isinstance(_tm_entry, dict):
                _tomorrow_session_time = _tm_entry.get("time", "")
            elif isinstance(_tm_entry, str) and _tm_entry not in ("rest", ""):
                _tomorrow_session_time = _tm_entry
        except Exception:
            pass

    if not next_session:
        if _tomorrow_is_rest:
            # Explicit rest day — send a recovery-focused message
            _week_intent = coach_state.get("WEEKLY_INTENT", {}).get("summary", "")
            _rest_prompt = (
                f"You are {ATHLETE_NAME}'s strength coach. Tomorrow ({tomorrow_full}) is a rest day.\n\n"
                f"Write a short Telegram message (2-3 sentences) that:\n"
                f"1. Confirms tomorrow is a rest day\n"
                f"2. Gives 1-2 specific recovery actions for tonight/tomorrow relevant to this week's training\n"
                f"3. Optionally references what's coming next\n\n"
                f"Weekly intent: {_week_intent or '(not set)'}\n\n"
                f"Style: direct, specific. No greeting, no sign-off. Not generic 'rest and recover' — "
                f"say WHY (what muscle groups need it, what the training load has been).\n"
                f"Write it now:"
            )
            try:
                from anthropic import Anthropic as _Anthropic
                from config import ANTHROPIC_API_KEY as _APIKEY, CLAUDE_HAIKU as _HAIKU
                _rest_resp = _Anthropic(api_key=_APIKEY).messages.create(
                    model=_HAIKU, max_tokens=100,
                    messages=[{"role": "user", "content": _rest_prompt}],
                )
                _rest_msg = _rest_resp.content[0].text.strip()
                if dry_run:
                    print(f"\n[DRY RUN — rest day message]:\n{_rest_msg}")
                    return
                from telegram_utils import send_telegram_message as _stm_ep
                if _stm_ep(_rest_msg):
                    upsert_coach_state("LAST_EVENING_PROTOCOL", str(today), "HIGH")
                    print(f"  Rest day message sent for {tomorrow_full}.")
            except Exception as _re:
                print(f"  Rest day message failed (non-fatal): {_re}")
            return
        print("  Evening protocol: all sessions complete or no upcoming session — skipping.")
        return

    # Build session summary
    _raw_label = next_session.get("label", "next session")
    label = f"{tomorrow_full} {_tomorrow_session_time} — {_raw_label}" if _tomorrow_session_time else _raw_label
    exercises = next_session.get("exercises", [])
    main_lifts = [ex for ex in exercises if ex.get("weight")][:4]
    lift_summary = ", ".join(
        f"{ex.get('name', '?')} {ex.get('weight', '')} {ex.get('sets_reps', '')}".strip()
        for ex in main_lifts
    )
    # Count of exercises in session for context
    total_ex_count = len([ex for ex in exercises if ex.get("name")])

    # Read context still needed directly in prompt
    weekly_intent = coach_state.get("WEEKLY_INTENT", {}).get("summary", "")

    # Recent health summary — only entries from last 3 days to avoid presenting stale data as current
    try:
        _cutoff = today - timedelta(days=3)
        _all_health = read_health_log(limit=10)
        _fresh = [e for e in _all_health if e.get("Date", "") >= str(_cutoff)]
        health_lines = []
        for e in _fresh[:3]:
            d = e.get("Date", "?")
            sleep = e.get("Sleep (hrs)", "")
            bw = e.get("Bodyweight (kg)", "")
            food = e.get("Food Quality (1-10)", "")
            notes = e.get("Notes", "")
            parts = [f"[{d}]"]
            if sleep: parts.append(f"sleep:{sleep}h")
            if bw: parts.append(f"BW:{bw}kg")
            if food: parts.append(f"food:{food}/10")
            if notes: parts.append(f"note:{notes[:50]}")
            health_lines.append(" ".join(parts))
        health_summary = "\n".join(health_lines) or "(no health data logged in last 3 days)"
    except Exception:
        health_summary = "(health data unavailable)"

    # Today's Telegram summary (last 5 inbound messages) — to avoid contradicting what was discussed
    try:
        today_str_iso = str(today)
        today_tg = [
            m for m in (read_telegram_log(limit=10) if "recent_tg" not in dir() else recent_tg)
            if m.get("Date", "") == today_str_iso
        ]
        today_tg_text = "\n".join(
            f"  [{m.get('Direction','')}] {m.get('Message','')[:100]}"
            for m in today_tg[-5:]
        ) or "(no messages today)"
    except Exception:
        today_tg_text = "(unavailable)"

    # Open concerns from Coach Focus
    try:
        from memory import read_coach_focus
        focus_items = read_coach_focus()
        concerns = [
            f.get("Item", "")[:100] for f in focus_items
            if f.get("Status", "") == "OPEN"
            and f.get("Category", "") in ("CONCERN", "FOLLOWUP")
            and "elbow" in f.get("Item", "").lower() or
            f.get("Category", "") == "CONCERN"
        ][:3]
        concerns_text = "; ".join(concerns) if concerns else "(none)"
    except Exception:
        concerns_text = "(unavailable)"

    # Projections for goal proximity
    projections: dict = {}
    try:
        from projections import run_all_projections
        projections = run_all_projections({"coach_state": coach_state, "health_log": [],
                                           "lift_history": [], "tracked_lifts": []}, program_data=program_data)
    except Exception:
        pass

    # Challenge suggestion — only when conditions are right
    challenge_suggestion = ""
    try:
        challenge = _should_suggest_challenge(coach_state, projections, program_data)
        if challenge:
            challenge_suggestion = (
                f"\nOPTIONAL CHALLENGE TO MENTION: {challenge['description']} "
                f"Coach reasoning: {challenge['reason']} Cost: {challenge['cost']}"
            )
    except Exception:
        pass

    # Select contextually relevant cues for tomorrow's primary lift
    primary_tomorrow_lift = main_lifts[0].get("name", "") if main_lifts else ""
    primary_tomorrow_notes = main_lifts[0].get("notes", "") or main_lifts[0].get("session_note", "") if main_lifts else ""
    tomorrow_cues = _select_lift_cue(primary_tomorrow_lift, coach_state, primary_tomorrow_notes) if primary_tomorrow_lift else []
    tomorrow_cue_text = "\n".join(f"  - {c}" for c in tomorrow_cues) if tomorrow_cues else ""

    tomorrow_plan_summary = f"{label} | {lift_summary}" if lift_summary else label

    # Session position (e.g. "Squat session #22 (Week 9 of 30)") and phase RPE target
    _ep_session_position = ""
    _ep_rpe_target = _get_phase_rpe_target(week_num, program_data.get("total_weeks", 30))
    try:
        _ep_lh = read_lift_history(limit=200)
        if primary_tomorrow_lift:
            _ep_session_position = _get_session_position(
                primary_tomorrow_lift, _ep_lh, week_num, program_data.get("total_weeks", 30)
            )
    except Exception:
        pass

    # HEALTH_READINESS — load daily signal and apply constraints
    _ep_readiness: dict = {}
    _ep_readiness_text = ""
    try:
        import json as _json
        _raw_readiness = coach_state.get("HEALTH_READINESS", {}).get("summary", "")
        if _raw_readiness:
            _ep_readiness = _json.loads(_raw_readiness)
            # Override RPE target if readiness constraint is stricter
            for constraint in _ep_readiness.get("constraints", []):
                if constraint.startswith("max_rpe:"):
                    try:
                        constrained_rpe = constraint.split(":")[1].strip()
                        _ep_rpe_target = f"RPE {constrained_rpe} (readiness constraint — do not exceed)"
                    except (IndexError, ValueError):
                        pass
            # Build readiness context block for prompt
            score = _ep_readiness.get("readiness_score", None)
            flags = _ep_readiness.get("flags", [])
            insights = _ep_readiness.get("insights", [])
            recs = _ep_readiness.get("recommendations", [])
            parts = []
            if score is not None:
                parts.append(f"Readiness score: {score}/100")
            if flags:
                parts.append(f"Flags: {', '.join(flags[:3])}")
            if recs:
                parts.append(f"Recommendations: {', '.join(recs[:2])}")
            if insights:
                parts.append(f"Data insight: {insights[0]}")
            _ep_readiness_text = " | ".join(parts) if parts else ""
    except Exception:
        pass

    # STRENGTH_PROJECTIONS — stall detection, goal proximity, push/pull balance
    _ep_strength_text = ""
    try:
        import json as _json_sp
        _raw_sp = coach_state.get("STRENGTH_PROJECTIONS", {}).get("summary", "")
        if _raw_sp:
            _sp = _json_sp.loads(_raw_sp)
            from strength_tracker import format_strength_report_for_prompt
            _ep_strength_text = format_strength_report_for_prompt(_sp)
    except Exception:
        pass

    # CARDIO_ZONES — weekly zone summary for prompt context
    _ep_cardio_text = ""
    try:
        import json as _json_cz
        _raw_cz = coach_state.get("CARDIO_ZONES", {}).get("summary", "")
        if _raw_cz:
            _cz = _json_cz.loads(_raw_cz)
            _ep_cardio_text = _cz.get("summary_text", "")
    except Exception:
        pass

    # Load commands for cascade conflict detection
    _ep_commands: list = []
    try:
        from memory import read_commands
        _ep_commands = read_commands()
    except Exception:
        pass

    # Build cascade context (with projections already computed above)
    _ep_health_log: list = []
    try:
        _ep_health_log = read_health_log(limit=3)
    except Exception:
        pass

    _ep_tg_log: list = []
    try:
        _ep_tg_log = recent_tg if "recent_tg" in dir() else read_telegram_log(limit=15)
    except Exception:
        pass

    cascade = _build_cascade_context(
        coach_state=coach_state,
        commands=_ep_commands,
        projections=projections,
        current_week_days=days,
        health_log=_ep_health_log,
        telegram_log=_ep_tg_log,
        coach_focus=focus_items if "focus_items" in dir() else [],
        today=today,
        orientation="tomorrow",
    )

    _ep_session_pos_line = f"This is {_ep_session_position}." if _ep_session_position else ""

    prompt = f"""You are {ATHLETE_NAME}'s strength coach. Write him a coaching message for tomorrow's session.

=== WHAT WAS ALREADY DISCUSSED TODAY (read FIRST — do NOT repeat or ask about these) ===
{today_tg_text}

=== COACHING CONTEXT — WORK THROUGH ALL 4 LAYERS BEFORE WRITING ===
{cascade}

=== TOMORROW'S SESSION ===
Session: {label} ({total_ex_count} exercises total)
Key lifts: {lift_summary or 'see program'}
Session position: {_ep_session_pos_line or '(unknown)'}
Phase RPE target: {_ep_rpe_target}
CUE OPTIONS for {primary_tomorrow_lift or 'primary lift'} (choose ONE, or skip if none apply):
{tomorrow_cue_text or "  (no specific cues — use your judgment)"}

=== HEALTH & CONCERNS ===
Recent health (last 3 days only — if empty, there is NO recent data, do not mention health):
{health_summary}
{f"Readiness signal: {_ep_readiness_text}" if _ep_readiness_text else ""}
Active concerns (elbow, injury, follow-ups): {concerns_text}
{challenge_suggestion}
{f"Strength analytics:{chr(10)}{_ep_strength_text}" if _ep_strength_text else ""}
{f"Cardio:{chr(10)}{_ep_cardio_text}" if _ep_cardio_text else ""}

=== COACHING TONE DIRECTIVE ===
Every message must do ALL of the following — in flowing prose, never as bullet points or headers:

1. WHY THIS SESSION: In 1-2 sentences, connect today's session to the program arc.
   Draw from Layer 2 (WEEKLY_INTENT, COACHING_REASON) in the cascade above.
   Example: "This week is the volume peak of your accumulation block — the sets today trigger
   the hypertrophic stimulus that converts to heavier loads in weeks 12-14."

2. SESSION POSITION + TARGET: State the session position (use "{_ep_session_pos_line}") and the
   target weight/sets from the program. Then state the RPE target: {_ep_rpe_target}.
   If RPE would be exceeded on set 3, drop 2.5kg — state this explicitly.

3. ONE CUE: Pick exactly ONE cue from the options above if relevant. One thing to focus on. Not a list.

4. SPECIFIC FEEDBACK ASK: End with a precise question that gives you calibration data.
   NOT "how did it go?" — instead: "Tell me how the last two sets felt — that's my calibration signal."
   or: "After the session, tell me if the third squat set hit RPE {_ep_rpe_target.split('-')[1] if '-' in _ep_rpe_target else '8'} or above."

5. HEALTH P.S. (only if health data or readiness signal above is non-empty):
   One line postscript. Prioritize readiness data insights over raw health log.
   Example: "P.S. — Readiness score is 52/100 (sleep debt flagged). Drop 2.5kg without hesitation."
   Or: "P.S. — Based on your data: 7.5h+ sleep → avg +2.8% strength. Tonight matters."

6. If Layer 3 shows a CONFLICT (pending catch-up vs. tomorrow's plan), address it conversationally:
   "Still catching up on [X] — want to swap tomorrow, or stick with the plan?"

LANGUAGE: English. Warm, direct, personal. Text message tone — not a report.
No bullet lists. No numbered points. No headers. Max 250 words.
REASONING RULE: At least one sentence must show your reasoning: "I see [data] — this means [X]."

HARD RULE: NEVER ask scheduling questions. Never "when can you train?" or "what day works?".
You have the athlete's schedule — make a decision and state it. Always propose, never ask openly.
If a session needs to be rescheduled, pick the day yourself and say "I'm moving this to [day]."

Write the message now:"""

    # Enforce athlete output preferences as hard constraints
    try:
        from memory import read_athlete_preferences
        from prompt import apply_output_preferences
        prompt = apply_output_preferences(prompt, read_athlete_preferences())
    except Exception:
        pass

    if dry_run:
        print(f"  [DRY RUN] Evening protocol for: {tomorrow_plan_summary}")
        print(f"  [DRY RUN] Weekly intent: {weekly_intent[:80] if weekly_intent else 'none'}")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        msg = result.content[0].text.strip()

        from telegram_utils import send_telegram_message
        sent = send_telegram_message(msg)
        if sent:
            print(f"  Evening protocol sent: {msg[:100]}")
            upsert_coach_state("LAST_EVENING_PROTOCOL", str(today), "HIGH")
            upsert_coach_state("TOMORROW_PLAN", tomorrow_plan_summary, "HIGH")
            # Write challenge cooldown if one was suggested
            if challenge_suggestion and not dry_run:
                upsert_coach_state("LAST_CHALLENGE", str(today), "HIGH")
            # Open daily planning conversation — athlete can respond to adjust tomorrow's plan
            try:
                import json as _json_dp
                _dp_thread = {
                    "date": str(tomorrow),
                    "session": tomorrow_plan_summary,
                    "weekly_intent": weekly_intent,
                    "thread": [{"role": "assistant", "content": msg}],
                }
                upsert_coach_state("DAILY_PLAN_THREAD", _json_dp.dumps(_dp_thread), "HIGH")
                upsert_coach_state("CURRENT_FLOW", f"daily_planning | {today} | tomorrow:{tomorrow}", "MEDIUM")
                print(f"  Daily planning conversation opened for {tomorrow}.")
            except Exception as _dp_err:
                print(f"  Daily planning CURRENT_FLOW failed (non-fatal): {_dp_err}")
        else:
            print("  Evening protocol: Telegram send failed.")
    except Exception as e:
        print(f"  Evening protocol failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------

def run_export(output_file: str = None, dry_run: bool = False):
    """
    Export all Coach Memory tabs to JSON and upload to Google Drive as a backup.

    Two destinations:
      1. Local file (output_file arg or auto-named coach_export_YYYYMMDD.json)
      2. Google Drive folder 'coach_backups/' — uploaded via Drive API

    Safe read-only on sheets. Drive upload is non-fatal if credentials lack Drive scope.
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
        "version": "V12",
        "data": {k: v for k, v in data.items()},
    }

    json_str = json.dumps(export, indent=2, default=str)
    byte_count = len(json_str.encode("utf-8"))
    record_count = sum(len(v) if isinstance(v, list) else 1 for v in data.values())
    print(f"  Export: {byte_count:,} bytes | {record_count} records")

    # --- Write local file ---
    if not output_file:
        from datetime import datetime as _dt
        output_file = f"coach_export_{_dt.today().strftime('%Y%m%d')}.json"

    if dry_run:
        print(f"  [DRY RUN] Would write to {output_file} and upload to Google Drive")
        print(json_str[:1000] + ("..." if len(json_str) > 1000 else ""))
        return

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(json_str)
    print(f"  Written to: {output_file}")

    # --- Upload to Google Drive ---
    try:
        import io
        from googleapiclient.discovery import build as _gapi_build
        from googleapiclient.http import MediaIoBaseUpload
        from sheets import get_credentials  # reuse existing OAuth token

        creds = get_credentials()
        drive_service = _gapi_build("drive", "v3", credentials=creds)

        # Find or create 'coach_backups' folder in Drive root
        folder_id = None
        results = drive_service.files().list(
            q="name='coach_backups' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
            spaces="drive",
        ).execute()
        folders = results.get("files", [])
        if folders:
            folder_id = folders[0]["id"]
        else:
            folder_meta = {
                "name": "coach_backups",
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = drive_service.files().create(body=folder_meta, fields="id").execute()
            folder_id = folder.get("id")
            print(f"  Created Drive folder 'coach_backups' (id={folder_id})")

        # Upload JSON file to the folder
        file_meta = {
            "name": output_file,
            "parents": [folder_id],
        }
        media = MediaIoBaseUpload(
            io.BytesIO(json_str.encode("utf-8")),
            mimetype="application/json",
            resumable=False,
        )
        uploaded = drive_service.files().create(
            body=file_meta, media_body=media, fields="id, name"
        ).execute()
        print(f"  Uploaded to Google Drive: {uploaded.get('name')} (id={uploaded.get('id')})")

    except ImportError:
        print("  Drive upload skipped: googleapiclient not available (pip install google-api-python-client)")
    except Exception as e:
        print(f"  Drive upload failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Meta-improvement: coach critiques itself and surfaces concrete suggestions
# ---------------------------------------------------------------------------

def run_meta_improvement(dry_run: bool = False) -> str:
    """
    The coach reads its own memory — Coach State, Coach Log, recent Telegram,
    athlete profile, open questions — and critiques its own coaching quality.

    Produces 3-5 concrete improvement suggestions surfaced to the athlete via
    Telegram. Each suggestion can be a behaviour change, a missing data request,
    a code feature idea, or a coaching adjustment.

    Called from: Telegram "meta" / "suggest improvements" keyword routing,
    or manually with --meta flag.
    """
    from memory import read_all, read_telegram_log, read_coach_log
    from telegram_utils import send_message as send_telegram_message

    today = date.today()
    print(f"[{today}] Running meta-improvement pass...")

    memory_data = read_all()
    coach_state = memory_data.get("coach_state", {})
    athlete_profile = memory_data.get("athlete_profile", "")
    long_term_goals = memory_data.get("long_term_goals", "")
    open_questions = [
        c for c in memory_data.get("commands", [])
        if c.get("Command", "").upper() == "OPEN_QUESTION"
        and c.get("Applied", "").upper() not in ("Y", "DECLINED")
    ]
    telegram_log = read_telegram_log(limit=30)
    coach_log = read_coach_log(limit=5)

    # Format recent Telegram exchange
    recent_tg = "\n".join(
        f"  [{r.get('Direction','?')}] {str(r.get('Message',''))[:200]}"
        for r in telegram_log[-20:]
    ) or "(none)"

    # Format open questions awaiting answers
    open_q_text = "\n".join(
        f"  - {c.get('Value','')}"
        for c in open_questions[:10]
    ) or "(none)"

    # Key Coach State domains most relevant to coaching quality
    key_domains = ["ATHLETE_MODEL", "ATHLETE_DREAMS", "BEHAVIOR_PATTERNS",
                   "SESSION_QUALITY", "HEALTH", "SCHEDULE", "WEEKLY_SCHEDULE"]
    state_snapshot = "\n".join(
        f"  {domain}: {coach_state.get(domain, {}).get('summary', '(empty)')[:300]}"
        for domain in key_domains
    )

    # Last few coach log entries
    recent_log = "\n".join(
        f"  [{e.get('Date','')}] {str(e.get('Entry',''))[:200]}"
        for e in (coach_log or [])[-5:]
    ) or "(none)"

    system_msg = (
        "You are an elite strength & conditioning coach doing a rigorous self-critique. "
        "You have full access to your memory and recent interactions with your athlete. "
        "Your job is to identify gaps, blind spots, and improvements in your own coaching — "
        "not platitudes, but real specific problems and concrete fixes. "
        "Think like a sports scientist reviewing their own practice."
    )

    user_msg = f"""SELF-CRITIQUE: Analyse the quality of my coaching over the past weeks.

ATHLETE PROFILE:
{athlete_profile[:500] if athlete_profile else "(not set)"}

LONG-TERM GOALS:
{long_term_goals[:300] if long_term_goals else "(not set)"}

KEY COACH STATE DOMAINS:
{state_snapshot}

OPEN QUESTIONS (unanswered follow-ups I asked the athlete):
{open_q_text}

RECENT TELEGRAM (last 20 messages):
{recent_tg}

RECENT COACH LOG (last 5 entries):
{recent_log}

---

Identify the 4-5 most impactful improvements I could make to my coaching of this athlete RIGHT NOW.

For each improvement:
- What is the concrete gap or missed opportunity?
- What specific change would fix it (behaviour, question, data, code feature)?
- Why does it matter for THIS athlete's trajectory?

Be brutally honest. Skip generic advice. Reference actual data from above.
End with one thing the athlete should do TODAY that I haven't asked them yet.

Format: numbered list, concise. Max 400 words total."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            system=system_msg,
            messages=[{"role": "user", "content": user_msg}],
        )
        analysis = resp.content[0].text.strip()
    except Exception as e:
        print(f"  [meta] Claude call failed: {e}")
        return ""

    message = f"🔍 *Coach self-critique* (meta-improvement pass):\n\n{analysis}"

    if dry_run:
        print(f"[DRY RUN] Meta-improvement message:\n{message}")
        return analysis

    try:
        send_telegram_message(message)
        print(f"  [meta] Sent meta-improvement analysis to Telegram.")
    except Exception as e:
        print(f"  [meta] Telegram send failed: {e}")
        print(f"  [meta] Analysis:\n{analysis}")

    return analysis


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Bootstrap Cascade — synthesize historical summaries from raw lift/health data
# ---------------------------------------------------------------------------

def run_bootstrap_cascade(dry_run: bool = False) -> None:
    """
    One-time bootstrap: synthesize WEEKLY_SUMMARIES and MONTHLY_SUMMARIES from
    all available lift history + health log, then run annual_eval + longterm_eval.

    Robustness constraints:
    - Sheet tabs use "Week N" names only — no dates in rows. Dates are inferred
      from actual lift history (Date column), NOT from a formula. The athlete
      progressed slower than 1 week per calendar week, so the formula is wrong.
      Approximate dates (prefixed with ~) are used only when lift history has no
      entries for a given week.
    - Session notes may be absent for some weeks — handled gracefully.
    - No RPE data exists yet — rpe_avg set to null in all synthesized summaries.
    - Includes current week (e.g. week 9) as a partial/in-progress summary
      alongside the closed weeks. Does NOT call weekly_eval for the current week.

    Flow:
      1. Read weeks 1-8 from program sheet
      2. Read full lift_history + health_log + GOLDEN_RULES + ANNUAL_ARC
      3. Sonnet → 4 WEEKLY_SUMMARYs (W1-2, W3-4, W5-6, W7-8)
      4. Sonnet → 2 MONTHLY_SUMMARYs (M1=W1-4, M2=W5-8)
      5. annual_eval() → ANNUAL_SUMMARY
      6. longterm_eval() → LONGTERM_PLAN
      7. run_weekly_health_science() → HEALTH_INSIGHTS + LIFT_INSIGHTS
    """
    import json as _json
    from datetime import date as _date, timedelta as _td
    from memory import (
        read_lift_history, read_health_log, read_coach_state,
        append_summary, write_single_summary,
    )
    from sheets import read_program_data
    from health_science import run_weekly_health_science

    print(f"[BOOTSTRAP] Starting cascade bootstrap at {_date.today()}...")

    # Resolve program start date
    program_start = resolve_program_start_date()
    print(f"[BOOTSTRAP] Program start: {program_start}")

    # Read all available data
    print("[BOOTSTRAP] Reading lift history and health log...")
    lift_history = read_lift_history(limit=500)
    health_log = read_health_log(limit=500)
    coach_state = read_coach_state()

    golden_rules_raw = coach_state.get("GOLDEN_RULES", {}).get("summary", "")
    annual_arc_raw = coach_state.get("ANNUAL_ARC", {}).get("summary", "")
    athlete_model_raw = coach_state.get("ATHLETE_MODEL", {}).get("summary", "")

    print(f"[BOOTSTRAP] Loaded {len(lift_history)} lift entries, {len(health_log)} health entries.")

    # Read program data for all weeks up to and including current week.
    # Closed weeks (1 to current_week-1) get full WEEKLY_SUMMARYs.
    # Current week (e.g. week 9) gets included as a PARTIAL summary
    # — whatever is logged so far, marked as in-progress.
    current_week = _get_authoritative_week_num()
    weeks_to_read = current_week  # include current week
    print(f"[BOOTSTRAP] Current week: {current_week}. Reading weeks 1-{weeks_to_read}.")

    week_data_by_num: dict = {}
    for wn in range(1, weeks_to_read + 1):
        try:
            pd = read_program_data(week_num=wn, lookback=0)
            week_data_by_num[wn] = pd
            days = pd.get("current_week", {}).get("days", [])
            sessions_done = sum(
                1 for d in days if any(e.get("done") for e in d.get("exercises", []))
            )
            status = "(partial/in-progress)" if wn == current_week else ""
            print(f"[BOOTSTRAP]   Week {wn}: {sessions_done}/{len(days)} sessions logged {status}")
        except Exception as e:
            print(f"[BOOTSTRAP]   Week {wn}: failed to read ({e})")

    # Build actual date ranges from lift_history "Date" + "Week" columns
    # Nacho's program weeks ≠ calendar weeks (he went slower), so we can't
    # use start_date + (N-1)*7. Instead, read actual dates logged per week.
    _week_dates_cache: dict = {}
    import re as _re_bs
    for _entry in lift_history:
        _raw_week = str(_entry.get("Week") or _entry.get("week") or "")
        _wn_match = _re_bs.search(r"\d+", _raw_week)
        if not _wn_match:
            continue
        try:
            _wn = int(_wn_match.group())
        except ValueError:
            continue
        _raw_date = _entry.get("Date") or _entry.get("date") or ""
        try:
            from datetime import datetime as _dtt
            _d = _dtt.fromisoformat(str(_raw_date)[:10]).date()
            if _wn not in _week_dates_cache:
                _week_dates_cache[_wn] = (_d, _d)
            else:
                _min_d, _max_d = _week_dates_cache[_wn]
                _week_dates_cache[_wn] = (min(_min_d, _d), max(_max_d, _d))
        except (ValueError, TypeError):
            continue

    def _infer_week_dates(week_num: int) -> tuple:
        """
        Return (week_start_str, week_end_str) from actual lift history dates.
        Falls back to approximate calendar math only if lift history has no data
        for this week. Marks approximate dates with a ~ prefix so LLM knows.
        """
        if week_num in _week_dates_cache:
            start_d, end_d = _week_dates_cache[week_num]
            return str(start_d), str(end_d)
        # No actual data — try to interpolate from surrounding weeks
        prev_end = _week_dates_cache.get(week_num - 1, (None, None))[1]
        if prev_end:
            approx_start = prev_end + _td(days=1)
            approx_end = approx_start + _td(days=6)
            return f"~{approx_start}", f"~{approx_end}"
        # Last resort: formula (warn that it may be off)
        from datetime import datetime as _dtt2
        _ps = _dtt2.fromisoformat(str(program_start)[:10]).date() if isinstance(program_start, str) else program_start
        start = _ps + _td(days=(week_num - 1) * 7)
        end = start + _td(days=6)
        return f"~{start}(approx)", f"~{end}(approx)"

    def _summarize_week_data(week_num: int) -> str:
        """Build a text summary of a single week's training data for the LLM prompt."""
        pd = week_data_by_num.get(week_num, {})
        start_str, end_str = _infer_week_dates(week_num)
        lines = [f"WEEK {week_num} ({start_str} to {end_str}):"]

        days = pd.get("current_week", {}).get("days", [])
        if not days:
            lines.append("  No data available for this week.")
            return "\n".join(lines)

        for day in days:
            label = day.get("label", f"Day")
            exercises = day.get("exercises", [])
            done_exs = [e for e in exercises if e.get("done") is True]
            skip_exs = [e for e in exercises if e.get("done") is False and e.get("name")]

            if not exercises:
                continue

            status = f"COMPLETED ({len(done_exs)}/{len(exercises)})" if done_exs else "NOT LOGGED"
            lines.append(f"  {label}: {status}")

            for ex in done_exs:
                name = ex.get("name", "")
                actual = ex.get("actual") or ex.get("weight") or "?"
                note = ex.get("session_note") or ex.get("notes") or ""
                note_str = f" | Note: {note[:60]}" if note else ""
                lines.append(f"    - {name}: {actual}{note_str}")

            if skip_exs:
                skip_names = [e.get("name", "") for e in skip_exs]
                lines.append(f"    SKIPPED: {', '.join(skip_names)}")

        return "\n".join(lines)

    def _get_health_for_week(week_num: int) -> str:
        """Build a text summary of health log entries for a given week."""
        start_d, end_d = _infer_week_dates(week_num)
        try:
            from datetime import datetime as _dt
            # Strip ~ or (approx) prefix from approximate dates
            clean_start = start_d.lstrip("~").split("(")[0].strip()
            clean_end = end_d.lstrip("~").split("(")[0].strip()
            start = _dt.fromisoformat(clean_start).date()
            end = _dt.fromisoformat(clean_end).date()
        except ValueError:
            return "No health data."

        entries = []
        for entry in health_log:
            raw_d = entry.get("Date") or entry.get("date") or ""
            try:
                entry_d = _dt.fromisoformat(str(raw_d)[:10]).date()
                if start <= entry_d <= end:
                    sleep = entry.get("Sleep (hrs)") or entry.get("sleep_hrs") or "?"
                    bw = entry.get("Body Weight") or entry.get("bw") or "?"
                    food = entry.get("Food Quality") or entry.get("food_quality") or "?"
                    entries.append(f"  {entry_d}: sleep={sleep}h, BW={bw}kg, food_quality={food}")
            except (ValueError, TypeError):
                continue

        return "\n".join(entries) if entries else "No health log data for this week."

    # --- Step 3: Synthesize WEEKLY_SUMMARYs ---
    # Group closed weeks into pairs: (1-2), (3-4), (5-6), (7-8)
    # Current week (9) gets its own entry, marked as partial/in-progress.
    closed_weeks = current_week - 1  # weeks fully completed
    weekly_summary_pairs = []
    for pair_start in range(1, closed_weeks + 1, 2):
        pair_end = min(pair_start + 1, closed_weeks)
        weekly_summary_pairs.append((pair_start, pair_end))
    # Add current (partial) week solo
    if current_week <= weeks_to_read:
        weekly_summary_pairs.append((current_week, current_week))

    synthesized_weekly: list = []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    for w_start, w_end in weekly_summary_pairs:
        if w_start > weeks_to_read:
            break

        is_partial_week = (w_start == current_week)
        label = f"weeks {w_start}-{w_end}" if w_start != w_end else f"week {w_start}"
        status_note = " (IN-PROGRESS — not yet complete)" if is_partial_week else ""
        print(f"[BOOTSTRAP] Synthesizing WEEKLY_SUMMARY for {label}{status_note}...")

        # Build training summary
        training_text = ""
        for wn in range(w_start, w_end + 1):
            training_text += _summarize_week_data(wn) + "\n\n"

        health_text = ""
        for wn in range(w_start, w_end + 1):
            health_text += f"Week {wn} health:\n" + _get_health_for_week(wn) + "\n"

        w_start_str, _ = _infer_week_dates(w_start)
        _, w_end_str = _infer_week_dates(w_end)

        partial_note = """
IMPORTANT: This week is CURRENTLY IN PROGRESS — not all sessions are complete yet.
Set "status": "in_progress" in the output. Do not treat missing sessions as skips.
""" if is_partial_week else ""

        prompt = f"""You are synthesizing historical training data into a structured WEEKLY_SUMMARY JSON.
This is a bootstrap operation — no real-time data, only what was logged in the program sheet.

IMPORTANT DATA CONSTRAINTS:
- No RPE data exists for any of these weeks (RPE column was not yet in the program). Set rpe_avg to null.
- Session notes may be absent — use exercise completion + actual weights as the primary signal.
- Infer effort quality from: completion rate, weight progression, and any available notes.
{partial_note}

=== ATHLETE PROFILE ===
{athlete_model_raw[:600] if athlete_model_raw else "Finance professional, 4x/week strength training, goals: 120kg squat, 105kg bench by Week 30."}

=== TRAINING DATA ===
{training_text}

=== HEALTH DATA ===
{health_text}

=== LIFT HISTORY EXCERPT (most relevant) ===
{_json.dumps([e for e in lift_history if e.get("Week") in [str(w) for w in range(w_start, w_end + 1)]][:30], ensure_ascii=False, indent=2)[:2000] if lift_history else "Not available."}

Synthesize a WEEKLY_SUMMARY JSON covering weeks {w_start}-{w_end} (period: {w_start_str} to {w_end_str}).

Return ONLY this JSON (no markdown fences, no explanation):
{{
  "week": {w_end},
  "week_range": "{w_start}-{w_end}",
  "status": "{"in_progress" if is_partial_week else "closed"}",
  "period_start": "{w_start_str}",
  "period_end": "{w_end_str}",
  "training": {{
    "sessions_done": <integer>,
    "sessions_possible": <integer>,
    "avg_effort_quality": "<poor|moderate|strong|excellent>",
    "volume_achieved": "<below_plan|partial|full|above_plan>",
    "primary_lift_progress": {{
      "squat": "<e.g. +0, +2.5kg, -2.5kg, or 'no data'>",
      "bench": "<...>",
      "deadlift": "<...>",
      "overhead_press": "<...>"
    }},
    "notable": "<1-2 sentence summary of what stood out>"
  }},
  "health": {{
    "avg_sleep": <float or null>,
    "avg_hrv": <float or null>,
    "avg_readiness": <float or null>,
    "bw_trend": "<e.g. 'stable at 82.3kg', 'dropped 0.5kg', or 'no data'>"
  }},
  "rpe_note": "RPE data not available — RPE column added from Week 10 onwards",
  "escalations": [],
  "patterns": {{
    "recurring_concern": "<e.g. right_elbow_pulls or null>",
    "behavioral_notes": "<any patterns observed or null>"
  }},
  "markov_note_for_next_week": "<1 sentence key carryover insight>",
  "to_monthly": "<1 sentence key contribution to monthly picture>"
}}"""

        if dry_run:
            print(f"[BOOTSTRAP][DRY RUN] Would synthesize WEEKLY_SUMMARY for weeks {w_start}-{w_end}")
            synthesized_weekly.append({
                "week": w_end, "week_range": f"{w_start}-{w_end}",
                "period_start": w_start_str, "period_end": w_end_str,
                "training": {"sessions_done": 0, "sessions_possible": 0, "avg_effort_quality": "dry_run"},
                "health": {"avg_sleep": None}, "rpe_note": "dry_run",
                "escalations": [], "patterns": {}, "markov_note_for_next_week": "", "to_monthly": "",
            })
            continue

        try:
            result = client.messages.create(
                model=CLAUDE_SONNET,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = result.content[0].text.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            summary = _json.loads(raw.strip())
            synthesized_weekly.append(summary)
            print(f"[BOOTSTRAP]   OK — weeks {w_start}-{w_end}: effort={summary.get('training', {}).get('avg_effort_quality', '?')}")
        except Exception as e:
            print(f"[BOOTSTRAP]   FAILED synthesizing weeks {w_start}-{w_end}: {e}")
            # Add minimal fallback so cascade doesn't skip
            w_s, _ = _infer_week_dates(w_start)
            _, w_e = _infer_week_dates(w_end)
            synthesized_weekly.append({
                "week": w_end, "week_range": f"{w_start}-{w_end}",
                "period_start": w_s, "period_end": w_e,
                "training": {"sessions_done": 0, "sessions_possible": 0,
                             "avg_effort_quality": "unknown", "volume_achieved": "unknown",
                             "primary_lift_progress": {}, "notable": "Synthesis failed — raw data unavailable."},
                "health": {"avg_sleep": None, "avg_hrv": None, "avg_readiness": None, "bw_trend": "no data"},
                "rpe_note": "RPE data not available",
                "escalations": [], "patterns": {},
                "markov_note_for_next_week": "Insufficient data.", "to_monthly": "Insufficient data.",
            })

    # Write WEEKLY_SUMMARIES
    if not dry_run and synthesized_weekly:
        from memory import upsert_coach_state
        upsert_coach_state(
            "WEEKLY_SUMMARIES",
            _json.dumps(synthesized_weekly, ensure_ascii=False),
            "HIGH",
        )
        print(f"[BOOTSTRAP] Wrote {len(synthesized_weekly)} WEEKLY_SUMMARIES to Coach State.")

    # --- Step 4: Synthesize MONTHLY_SUMMARYs ---
    # M1 = weeks 1-4 (first month of program)
    # M2 = weeks 5-current (second month, includes partial week 9 if present)
    # Note: month boundaries don't correspond to calendar months — they follow
    # the program's own pace, which was slower than 4 weeks per month.
    monthly_pairs = [
        ("M1", [s for s in synthesized_weekly if int(s.get("week", 0)) <= 4]),
        ("M2", [s for s in synthesized_weekly if int(s.get("week", 0)) > 4]),
    ]
    synthesized_monthly: list = []

    for month_label, month_weeks in monthly_pairs:
        if not month_weeks:
            continue

        period_start = month_weeks[0].get("period_start", "?")
        period_end = month_weeks[-1].get("period_end", "?")
        print(f"[BOOTSTRAP] Synthesizing MONTHLY_SUMMARY {month_label} ({period_start} to {period_end})...")

        weekly_summaries_text = _json.dumps(month_weeks, ensure_ascii=False, indent=2)

        prompt = f"""You are synthesizing weekly summaries into a MONTHLY_SUMMARY JSON.

=== ATHLETE PROFILE ===
{athlete_model_raw[:400] if athlete_model_raw else "Finance professional. Goals: 120kg squat, 105kg bench by Week 30."}

=== GOLDEN RULES ===
{golden_rules_raw[:400] if golden_rules_raw else "Strength + aesthetics + health + longevity + sleep > volume."}

=== ANNUAL ARC (program goals and trajectory) ===
{annual_arc_raw[:400] if annual_arc_raw else "30-week strength program. Targets: 120kg squat, 105kg bench by Week 30."}

=== WEEKLY SUMMARIES ===
{weekly_summaries_text[:3000]}

Return ONLY this JSON (no markdown, no explanation):
{{
  "month": "{month_label}",
  "period_start": "{period_start}",
  "period_end": "{period_end}",
  "training": {{
    "avg_sessions_per_week": <float>,
    "avg_effort_quality": "<poor|moderate|strong|excellent>",
    "volume_trend": "<decreasing|stable|increasing>",
    "primary_lift_progress": {{
      "squat": "<overall trend for this month>",
      "bench": "<...>",
      "deadlift": "<...>",
      "overhead_press": "<...>"
    }},
    "notable": "<2-3 sentences: what defined this month of training>"
  }},
  "health": {{
    "avg_sleep": <float or null>,
    "avg_readiness": <float or null>,
    "bw_trend": "<trend summary or 'no data'>"
  }},
  "escalations": [],
  "recurring_patterns": "<key behavioral or performance patterns>",
  "markov_note": "<key carryover insight for next month>",
  "to_annual": "<1-2 sentence contribution to annual picture>"
}}"""

        if dry_run:
            print(f"[BOOTSTRAP][DRY RUN] Would synthesize MONTHLY_SUMMARY {month_label}")
            synthesized_monthly.append({"month": month_label, "period_start": period_start, "period_end": period_end})
            continue

        try:
            result = client.messages.create(
                model=CLAUDE_SONNET,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = result.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            summary = _json.loads(raw.strip())
            synthesized_monthly.append(summary)
            print(f"[BOOTSTRAP]   OK — {month_label}: effort={summary.get('training', {}).get('avg_effort_quality', '?')}")
        except Exception as e:
            print(f"[BOOTSTRAP]   FAILED synthesizing {month_label}: {e}")
            synthesized_monthly.append({
                "month": month_label, "period_start": period_start, "period_end": period_end,
                "training": {"avg_sessions_per_week": 0, "notable": "Synthesis failed."},
                "health": {}, "escalations": [], "recurring_patterns": "",
                "markov_note": "Insufficient data.", "to_annual": "Insufficient data.",
            })

    # Write MONTHLY_SUMMARIES
    if not dry_run and synthesized_monthly:
        from memory import upsert_coach_state
        upsert_coach_state(
            "MONTHLY_SUMMARIES",
            _json.dumps(synthesized_monthly, ensure_ascii=False),
            "HIGH",
        )
        print(f"[BOOTSTRAP] Wrote {len(synthesized_monthly)} MONTHLY_SUMMARIES to Coach State.")

    # --- Step 5: annual_eval ---
    print("[BOOTSTRAP] Running annual_eval()...")
    try:
        from cascade_levels import annual_eval
        annual_eval(dry_run=dry_run)
        print("[BOOTSTRAP] annual_eval() complete.")
    except Exception as e:
        print(f"[BOOTSTRAP] annual_eval() failed (non-fatal): {e}")

    # --- Step 6: longterm_eval ---
    print("[BOOTSTRAP] Running longterm_eval()...")
    try:
        from cascade_levels import longterm_eval
        longterm_eval(dry_run=dry_run)
        print("[BOOTSTRAP] longterm_eval() complete.")
    except Exception as e:
        print(f"[BOOTSTRAP] longterm_eval() failed (non-fatal): {e}")

    # --- Step 7: lift + health science ---
    print("[BOOTSTRAP] Running lift + health science pass...")
    try:
        results = run_weekly_health_science(health_log, lift_history, dry_run=dry_run)
        h_count = len(results.get("health", {}))
        l_trends = len(results.get("lift", {}).get("trends", {}))
        print(f"[BOOTSTRAP] Science pass done: {h_count} health correlations, {l_trends} lift trends.")
    except Exception as e:
        print(f"[BOOTSTRAP] Science pass failed (non-fatal): {e}")

    print("[BOOTSTRAP] Bootstrap complete.")
    if dry_run:
        print("[BOOTSTRAP][DRY RUN] No data was written to Coach State.")


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
        elif getattr(args, "meta", False):
            run_meta_improvement(dry_run=args.dry_run)
        elif getattr(args, "init", False):
            from iteration_zero import run_iteration_zero
            run_iteration_zero(dry_run=args.dry_run)
        elif getattr(args, "close_day", False):
            from cascade_levels import close_day
            close_day(dry_run=args.dry_run)
        elif getattr(args, "weekly_eval", False):
            from cascade_levels import weekly_eval
            weekly_eval(dry_run=args.dry_run)
        elif getattr(args, "monthly_eval", False):
            from cascade_levels import monthly_eval
            monthly_eval(dry_run=args.dry_run)
        elif getattr(args, "annual_eval", False):
            from cascade_levels import annual_eval
            annual_eval(dry_run=args.dry_run)
        elif getattr(args, "longterm_eval", False):
            from cascade_levels import longterm_eval
            longterm_eval(dry_run=args.dry_run)
        elif getattr(args, "bootstrap", False):
            run_bootstrap_cascade(dry_run=args.dry_run)
        elif getattr(args, "sync_garmin", False):
            sync_garmin(days=14, dry_run=args.dry_run)
        elif getattr(args, "sync_sheet", False):
            from sheet_sync import SheetSyncEngine
            from memory import read_lift_history, read_health_log, read_commands
            from sheets import read_program_data
            _wn = _get_authoritative_week_num()
            _pd = read_program_data(week_num=_wn, lookback=0)
            _days = _pd.get("current_week", {}).get("days", [])
            _result = SheetSyncEngine().run_sync(
                week_num=_wn,
                current_week_days=_days,
                health_log=read_health_log(limit=50),
                lift_history=read_lift_history(limit=50),
                commands=read_commands(),
                dry_run=args.dry_run,
            )
            print(f"  Sync complete: {_result['events']} event(s), "
                  f"{_result['resolved_catchups']} catchup(s) resolved, "
                  f"domains updated: {_result['updated_domains']}")
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
