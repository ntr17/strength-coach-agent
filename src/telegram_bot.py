"""
Telegram bot — persistent bidirectional channel with the athlete.

Runs 24/7 on Railway. Handles incoming messages from the athlete and responds
as the coach. All conversations are logged to Coach Memory (Telegram Log tab).

Commands:
  /start   — greeting from the coach
  /summary — quick weekly progress snapshot

Any other text is treated as a question to the coach.
Routing: short/simple questions → Haiku (fast), longer/complex → Sonnet.
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import anthropic

# Import project modules (bot runs from repo root or src/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from config import (
    ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL, CLAUDE_HAIKU,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    bootstrap_google_credentials,
)

# Write Google credential files from env vars if running on Railway/CI
bootstrap_google_credentials()

from prompt import SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

HAIKU_MODEL = CLAUDE_HAIKU
SONNET_MODEL = CLAUDE_MODEL

# Use Haiku for short conversational messages, Sonnet for complex queries
HAIKU_THRESHOLD_WORDS = 20  # if message < 20 words, use Haiku


def _choose_model(message_text: str) -> str:
    word_count = len(message_text.split())
    return HAIKU_MODEL if word_count < HAIKU_THRESHOLD_WORDS else SONNET_MODEL


# ---------------------------------------------------------------------------
# In-memory tool cache (60s TTL — avoids redundant sheet reads within one session)
# ---------------------------------------------------------------------------

import time as _time

_TOOL_CACHE: dict = {}
_TOOL_CACHE_TTL = 60  # seconds


def _cache_get(key: str):
    entry = _TOOL_CACHE.get(key)
    if entry and (_time.time() - entry["ts"]) < _TOOL_CACHE_TTL:
        return entry["value"]
    return None


def _cache_set(key: str, value: str) -> None:
    _TOOL_CACHE[key] = {"value": value, "ts": _time.time()}


# ---------------------------------------------------------------------------
# Build context for the bot response
# ---------------------------------------------------------------------------

def _build_bot_context() -> str:
    """
    Build a rich context string for the bot — same Tier-1 brain as the daily email coach.
    Reads Coach State, Coach Focus, Athlete Preferences, Lift History, and recent Telegram log.
    """
    try:
        from memory import (
            read_telegram_log, read_athlete_profile, read_long_term_goals,
            read_lift_history, read_coach_state, read_coach_focus,
            read_athlete_preferences, read_commands, read_tracked_lifts,
        )

        sections = []

        # --- System capabilities (so bot knows what it is and what the pipeline does) ---
        sections.append(
            "SYSTEM CAPABILITIES (what this coaching system does and when):\n"
            "  - Morning brief (--brief): 07:00 UTC Mon-Sat (08:00/09:00 Spain) — automated cron\n"
            "  - Proactive check-in (--proactive): 08:00 + 14:00 UTC (09:00 + 15:00 Spain) — automated cron\n"
            "  - Post-session check-in (--post-session): 13:00 UTC (14:00 Spain) — automated cron\n"
            "  - Evening protocol (--evening-protocol): 19:00 UTC Mon-Sat (20:00/21:00 Spain) — automated cron\n"
            "  - Weekly email + strategic pass (--weekly, --think): 19:00 + 21:00 UTC Sunday — automated cron\n"
            "  - Telegram bot (this process): online 24/7 on Railway — responds to direct messages only\n"
            "  NOTE: You are the Telegram bot. You do NOT control or trigger the crons above. "
            "If an automated message didn't arrive, tell the athlete honestly — you cannot investigate or retry it."
        )

        # --- Init Zero completion note (suppress stale golden-rules thread) ---
        try:
            from iteration_zero import read_iteration_zero_state
            _iz = read_iteration_zero_state()
            if _iz.get("status") == "COMPLETE":
                sections.append(
                    "SYSTEM NOTE: Initialization interview (Iteration Zero) is COMPLETE. "
                    "Do NOT ask any more golden rules, long-term vision, or onboarding questions. "
                    "All profile data has been collected and committed. Operate in normal coaching mode."
                )
        except Exception:
            pass

        # --- Tier 1: Coach State (compressed domain summaries — the coach's brain) ---
        coach_state = read_coach_state()
        commands = read_commands()

        # --- Cascade summary: 4-layer context for coherent reasoning ---
        try:
            from datetime import date as _date, timedelta as _timedelta
            from run_coach import (
                _build_cascade_l1, _build_cascade_l2,
                _detect_session_conflicts,
            )

            _today = _date.today()
            _today_str = str(_today)
            _yesterday_str = str(_today - _timedelta(days=1))

            # Layer 1 — strategic (no projections in bot context for speed)
            _l1 = _build_cascade_l1(coach_state, None)

            # Layer 2 — mesocycle (no projections)
            _l2 = _build_cascade_l2(coach_state, None, [])

            # Layer 3 — active commitments (the most critical for coherence)
            _tomorrow_plan = coach_state.get("TOMORROW_PLAN", {}).get("summary", "")
            _last_evening = coach_state.get("LAST_EVENING_PROTOCOL", {}).get("summary", "")
            _last_brief_content = coach_state.get("LAST_BRIEF_CONTENT", {}).get("summary", "")
            _last_brief_date = coach_state.get("LAST_BRIEF", {}).get("summary", "")
            _conflict = _detect_session_conflicts(coach_state, commands)

            _commitment_lines = []
            if _tomorrow_plan and _last_evening == _yesterday_str:
                _commitment_lines.append(f"  Committed (last night): {_tomorrow_plan}")
            if _last_brief_content and _last_brief_date == _today_str:
                _brief_text = _last_brief_content[len(_today_str) + 3:]
                _commitment_lines.append(f"  Briefed (this morning): {_brief_text[:200]}")
            if _conflict != "clear":
                _commitment_lines.append(f"  {_conflict}")

            _cascade_lines = []
            if _l1:
                _cascade_lines.append(_l1)
            if _l2:
                _cascade_lines.append(_l2)
            if _commitment_lines:
                _l3_block = (
                    "LAYER 3 — ACTIVE COMMITMENTS\n"
                    + "\n".join(_commitment_lines)
                    + "\n  RULE: Before claiming anything about today's session, verify it matches these commitments.\n"
                    "  If no commitment is set, derive from Layer 1 + Layer 2 — not from 'next undone session' alone.\n"
                    "  If a CONFLICT exists, surface it explicitly rather than silently picking one side."
                )
                _cascade_lines.append(_l3_block)

            # Layer 4 — last 24h active conversation
            _current_flow = coach_state.get("CURRENT_FLOW", {}).get("summary", "")
            if _current_flow and _current_flow.startswith(f"endsession | {_today_str}"):
                _l4_block = (
                    "LAYER 4 — ACTIVE CONVERSATION\n"
                    + f"  {_current_flow}\n"
                    + "  RULE: The athlete's reply is likely answering these questions — continue the thread, "
                    "don't start fresh. Log any RPE or session data they provide."
                )
                _cascade_lines.append(_l4_block)

            if _cascade_lines:
                sections.append(
                    "=== COACHING CONTEXT (reason through this before responding) ===\n\n"
                    + "\n\n".join(_cascade_lines)
                )
        except Exception as _e:
            print(f"[BotContext] Cascade build failed (non-fatal): {_e}")
        if coach_state:
            lines = []
            for domain, data in coach_state.items():
                summary = data.get("summary", "").strip()
                if summary:
                    lines.append(f"  [{domain}] {summary}")
            if lines:
                sections.append("COACH STATE (current understanding per domain)\n" + "\n".join(lines))

        # --- Tier 1: Coach Focus (open watch items) ---
        focus_items = read_coach_focus(status_filter="OPEN")
        if focus_items:
            lines = []
            for item in focus_items[:12]:
                tag = item.get("Category", "")
                note = item.get("Item", "").strip()
                priority = item.get("Priority", "")
                badge = f"[{priority}] " if priority in ("HIGH", "PINNED") else ""
                lines.append(f"  {badge}[{tag}] {note}")
            sections.append("COACH FOCUS (open watch items)\n" + "\n".join(lines))

        # --- Tier 1: Athlete Preferences ---
        prefs = read_athlete_preferences()
        if prefs:
            lines = []
            for p in prefs:
                cat = p.get("Category", "")
                pref = p.get("Preference", "").strip()
                lines.append(f"  [{cat}] {pref}")
            sections.append("ATHLETE PREFERENCES\n" + "\n".join(lines))

        # --- Pending proposals (so bot knows what's awaiting confirmation) ---
        commands = read_commands()
        pending = [c for c in commands
                   if c.get("Command", "").upper() == "PENDING_PROPOSAL"
                   and c.get("Applied", "").upper() not in ("Y", "DECLINED")]
        if pending:
            lines = [f"  {p.get('Value', '')[:120]}" for p in pending]
            sections.append("AWAITING ATHLETE CONFIRMATION\n" + "\n".join(lines))

        # --- Athlete profile + goals ---
        profile = read_athlete_profile()
        if profile:
            sections.append(f"ATHLETE PROFILE\n{profile.strip()}")

        goals = read_long_term_goals()
        if goals:
            sections.append(f"LONG-TERM GOALS\n{goals.strip()}")

        # --- Lift levels (best 1RM per tracked lift, word-boundary match) ---
        import re as _re
        tracked_lifts = read_tracked_lifts()
        lift_history = read_lift_history(limit=60)
        if lift_history:
            lift_lines = []
            for tl in tracked_lifts:
                lift = tl["match_pattern"]
                pattern = _re.compile(r"(?i)^" + _re.escape(lift) + r"(\s|$|\()")
                best_est = None
                for row in lift_history:
                    if not pattern.match(row.get("Exercise", "").strip()):
                        continue
                    est = row.get("Est 1RM", "")
                    if est:
                        try:
                            v = float(est)
                            if best_est is None or v > best_est[0]:
                                best_est = (v, row.get("Date", "?"))
                        except ValueError:
                            pass
                if best_est:
                    lift_lines.append(f"  {lift}: {best_est[0]}kg est. 1RM [{best_est[1]}]")
            if lift_lines:
                sections.append("CURRENT LIFT LEVELS\n" + "\n".join(lift_lines))

        # --- Recent Telegram conversation (last 12 messages) ---
        tg_log = read_telegram_log(limit=12)
        if tg_log:
            lines = []
            for entry in tg_log:
                direction = entry.get("Direction", "")
                msg = entry.get("Message", "").strip()
                d = entry.get("Date", "")
                label = ATHLETE_NAME if direction == "IN" else "Coach"
                lines.append(f"  [{d}] {label}: {msg}")
            sections.append("RECENT TELEGRAM CONVERSATION\n" + "\n".join(lines))

        return "\n\n---\n\n".join(sections)

    except Exception as e:
        return f"[Context unavailable: {e}]"


# ---------------------------------------------------------------------------
# Claude response generation (context-injection path — used by /summary)
# ---------------------------------------------------------------------------

def _generate_response(user_message: str, context: str, model: str) -> str:
    """Generate a coaching response via Claude (pre-loaded context path)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    bot_system = SYSTEM_PROMPT + (
        "\n\nYou are responding via Telegram — keep replies concise and conversational. "
        "This is a quick check-in, not a full coaching email. 1-4 sentences unless the question genuinely needs more. "
        "No section headers. Natural tone.\n\n"
        "CRITICAL: Only state facts that are explicitly present in the context above. "
        "If specific data is missing (exercise names, weights, dates, numbers), say so directly: "
        "'I don't have that detail in front of me right now.' "
        "Never invent training data, exercise names, weights, or results. "
        "Never claim to have access to data that isn't shown in the context.\n\n"
        "NOTE: Estimated 1RM values are computed from prescribed weights unless the athlete "
        "explicitly logged actual weights in session notes."
    )

    full_message = f"{context}\n\n---\n\nATHLETE MESSAGE (via Telegram): {user_message}\n\nReply as the coach."

    message = client.messages.create(
        model=model,
        max_tokens=400,
        system=bot_system,
        messages=[{"role": "user", "content": full_message}]
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Tool use — Claude fetches exactly the data it needs
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS = [
    {
        "name": "get_coach_brain",
        "description": (
            "Fetch the coach's current knowledge: Coach State (per-domain summaries), "
            "Coach Focus (open watch items), Athlete Preferences, Profile, Long-Term Goals, "
            "and any pending proposals awaiting athlete confirmation. "
            "Call this first for most questions."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_lift_history",
        "description": (
            "Fetch recent lift history for a specific exercise. Returns dates, weights, "
            "sets/reps, and estimated 1RM values. "
            "Note: Est 1RM is computed from prescribed weights unless athlete logged actual weights."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": "Exercise name (e.g. 'Squat', 'Bench Press'). Matched at start of name.",
                },
                "weeks": {
                    "type": "integer",
                    "description": "How many weeks back to look (default 8, max 24).",
                },
            },
            "required": ["exercise"],
        },
    },
    {
        "name": "get_program_week",
        "description": (
            "Fetch prescribed workout data for a specific week — exercises, weights, sets/reps, done status. "
            "Omit week_num to get the current week. Pass week_num to fetch any historical week (1-30). "
            "Optionally pass sheet_id to read from a specific program sheet (use list_programs to find IDs)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "week_num": {
                    "type": "integer",
                    "description": "Week number to fetch (1-based). Omit for current week.",
                },
                "sheet_id": {
                    "type": "string",
                    "description": "Google Sheet ID of a specific program (from list_programs). Omit for active program.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_programs",
        "description": (
            "List all training programs — active and completed — with their Sheet IDs, "
            "date ranges, and total weeks. Use this to discover what program history is available, "
            "then fetch specific weeks using get_program_week with the sheet_id."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_projections",
        "description": (
            "Fetch computed 1RM projections, bodyweight trend, and program completion status. "
            "These are Python math results — not LLM guesses. "
            "Use for progress and goal-tracking questions."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_data_summary",
        "description": (
            "Get a quick overview of what training data is available — session counts per lift, "
            "date ranges, and how much Est 1RM data exists. "
            "Call this when you suspect data might be sparse or want to know what history is available "
            "before deciding what to fetch in detail."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "log_lift",
        "description": (
            "Record a completed lift to the training archive (Lift History). "
            "Use immediately when the athlete reports what they just lifted or mentions past performance. "
            "Est 1RM is auto-computed from weight and reps using the Epley formula."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {"type": "string", "description": "Exercise name (e.g. 'Squat', 'Bench Press')."},
                "weight": {"type": "string", "description": "Weight lifted, e.g. '100kg' or '100'."},
                "sets_reps": {"type": "string", "description": "Sets x reps, e.g. '3x5' or '4x4'."},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD. Omit for today."},
                "notes": {"type": "string", "description": "Optional notes about the set/session."},
                "completed": {"type": "boolean", "description": "Whether completed successfully (default true)."},
            },
            "required": ["exercise", "weight", "sets_reps"],
        },
    },
    {
        "name": "log_bodyweight",
        "description": (
            "Record the athlete's bodyweight to the Health Log. "
            "Use when the athlete mentions their weight, even in passing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "weight_kg": {"type": "number", "description": "Bodyweight in kg."},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD. Omit for today."},
                "notes": {"type": "string", "description": "Optional notes (sleep quality, energy, etc.)."},
            },
            "required": ["weight_kg"],
        },
    },
    {
        "name": "get_health_log",
        "description": (
            "Fetch the athlete's recent health log: bodyweight, sleep, energy, food quality, steps, notes. "
            "Use for any health, recovery, nutrition, or body composition question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to fetch (default 14, max 90).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "send_chart",
        "description": (
            "Generate and send a progress chart to the athlete via Telegram. "
            "Use when the athlete asks for a visual chart, graph, or progress picture."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["1rm", "bodyweight", "volume"],
                    "description": (
                        "'1rm' = estimated 1RM over time for key lifts, "
                        "'bodyweight' = bodyweight trend, "
                        "'volume' = weekly training volume"
                    ),
                }
            },
            "required": ["chart_type"],
        },
    },
    {
        "name": "get_program_comparison",
        "description": (
            "Compare 1RM gains across all historical training programs. "
            "Shows start vs end 1RM and total gain per lift for each completed program, "
            "plus the current program's progress so far. "
            "Use for long-term progress review, motivation, or when athlete asks "
            "'how do I compare to previous programs?' or 'what's my best ever?'"
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_program",
        "description": (
            "Write a change to the active program sheet. Use for any request to modify, "
            "annotate, or update something in the program — notes, weights, reps, exercise swaps. "
            "Examples: 'leave a note on squat week 8', 'add a comment to bench press', "
            "'change OHP weight to 60kg', 'add a hi to the notes'.\n\n"
            "Operations:\n"
            "  note_add — add/append a note to an exercise row (low-stakes, applied immediately)\n"
            "  weight_change — change the prescribed weight for an exercise (creates confirmation proposal)\n"
            "  sets_reps_change — change sets/reps prescription (creates confirmation proposal)\n"
            "  exercise_swap — replace one exercise with another (creates confirmation proposal)\n\n"
            "For note_add: applied immediately, no confirmation needed.\n"
            "For all other ops: a proposal is created and shown to the athlete for confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["note_add", "weight_change", "sets_reps_change", "exercise_swap"],
                    "description": "The type of change to make.",
                },
                "exercise": {
                    "type": "string",
                    "description": "Exercise name to target (e.g. 'Squat', 'Bench Press'). Required for most ops.",
                },
                "week": {
                    "type": "integer",
                    "description": "Week number to apply the change to. Omit to use current week.",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "The new value: for note_add = note text, for weight_change = new weight (e.g. '82.5'), "
                        "for sets_reps_change = new prescription (e.g. '4x5'), "
                        "for exercise_swap = new exercise name."
                    ),
                },
            },
            "required": ["operation", "value"],
        },
    },
]


def _tool_get_coach_brain() -> str:
    """Return the full Tier-1 context (reuses _build_bot_context)."""
    return _build_bot_context()


def _tool_get_lift_history(exercise: str, weeks: int = 8) -> str:
    """Return recent lift history rows for a given exercise."""
    import re as _re
    from datetime import date as _date, timedelta
    try:
        from memory import read_lift_history
        cutoff = str(_date.today() - timedelta(weeks=min(weeks, 24)))
        all_rows = read_lift_history(limit=300)
        pattern = _re.compile(r"(?i)^" + _re.escape(exercise) + r"(\s|$|\()")
        rows = [
            r for r in all_rows
            if pattern.match(r.get("Exercise", "").strip())
            and r.get("Date", "") >= cutoff
        ]
        if not rows:
            return f"No lift history for '{exercise}' in the last {weeks} weeks."
        lines = []
        for r in rows[-25:]:  # cap at 25 rows to keep context tight
            lines.append(
                f"  {r.get('Date','')} | {r.get('Exercise','')} | "
                f"{r.get('Weight','')} | {r.get('Sets x Reps','')} | "
                f"Est 1RM: {r.get('Est 1RM','?')}kg"
                + (f" | actual: {r.get('Actual','')}" if r.get("Actual") else "")
            )
        return f"Lift history for '{exercise}' (last {weeks} weeks, {len(rows)} sessions):\n" + "\n".join(lines)
    except Exception as e:
        return f"Could not load lift history: {e}"


def _tool_get_program_week(week_num: int = None, sheet_id: str = None) -> str:
    """Return workout data for a specific week from any program sheet."""
    try:
        from sheets import read_program_data
        from config import compute_current_week, resolve_program_start_date
        from workout_agent import _format_program_for_context

        if week_num is None:
            week_num = compute_current_week(resolve_program_start_date())

        program_data = read_program_data(week_num=week_num, sheet_id=sheet_id or None)
        result = _format_program_for_context(program_data)
        label = f"Week {week_num}" + (f" (sheet: {sheet_id[:12]}...)" if sheet_id else " (active program)")
        return f"{label}\n{result}" if result else f"{label}: no data found"
    except Exception as e:
        return f"Could not load program week {week_num}: {e}"


def _tool_list_programs() -> str:
    """Return all programs from the Active Sheets registry and Program History."""
    try:
        from memory import read_sheet_registry, read_program_history

        lines = []

        # Active Sheets registry (current + recently created programs)
        registry = read_sheet_registry()
        if registry:
            lines.append("PROGRAM REGISTRY (Active Sheets tab):")
            for entry in registry:
                name = entry.get("Name", "?")
                status = entry.get("Status", "?")
                sheet_id = entry.get("Sheet ID", "")
                start = entry.get("Start Date", "")
                weeks = entry.get("Total Weeks", "")
                notes = entry.get("Notes", "")
                line = f"  [{status}] {name} | start: {start} | {weeks}wk"
                if sheet_id:
                    line += f" | sheet_id: {sheet_id}"
                if notes:
                    line += f" | {notes}"
                lines.append(line)

        # Program History tab (older programs)
        history = read_program_history()
        if history:
            lines.append("\nPROGRAM HISTORY tab:")
            for entry in history:
                lines.append(
                    f"  {entry.get('Program', entry.get('Name', '?'))} | "
                    f"{entry.get('Start', entry.get('Start Date', '?'))} → "
                    f"{entry.get('End', entry.get('End Date', '?'))} | "
                    f"{entry.get('Notes', entry.get('Outcome', ''))}"
                )

        if not lines:
            return "No program records found."

        lines.append(
            "\nTo fetch a specific week from a program, use get_program_week(week_num=N, sheet_id='...')."
        )
        return "\n".join(lines)
    except Exception as e:
        return f"Could not load program list: {e}"


def _tool_get_projections() -> str:
    """Return computed projections (includes fatigue model ATL/CTL/TSB + tonnage)."""
    try:
        from memory import read_all
        from projections import run_all_projections
        from sheets import read_program_data
        from config import compute_current_week, resolve_program_start_date
        memory_data = read_all()
        week_num = compute_current_week(resolve_program_start_date())
        program_data = read_program_data(week_num=week_num, lookback=4)
        result = run_all_projections(memory_data, program_data=program_data)
        return result.get("formatted") or "Insufficient data for projections."
    except Exception as e:
        return f"Could not compute projections: {e}"


def _tool_get_data_summary() -> str:
    """Return a bird's-eye view of data availability per tracked lift."""
    import re as _re
    try:
        from memory import read_lift_history, read_tracked_lifts
        all_rows = read_lift_history(limit=500)
        tracked = read_tracked_lifts()

        if not all_rows:
            return "No lift history data found in memory."

        all_dates = sorted(r.get("Date", "") for r in all_rows if r.get("Date"))
        total = len(all_rows)
        span = f"{all_dates[0]} to {all_dates[-1]}" if all_dates else "no dates"

        lines = [f"Lift History: {total} total sessions | {span}", ""]
        for tl in tracked:
            lift = tl["match_pattern"]
            pattern = _re.compile(r"(?i)^" + _re.escape(lift) + r"(\s|$|\()")
            rows = [r for r in all_rows if pattern.match(r.get("Exercise", "").strip())]
            if rows:
                dates = sorted(r.get("Date", "") for r in rows if r.get("Date"))
                has_1rm = sum(1 for r in rows if r.get("Est 1RM"))
                lines.append(
                    f"  {lift}: {len(rows)} sessions | {dates[0]} to {dates[-1]} "
                    f"| {has_1rm} with Est 1RM"
                )
            else:
                lines.append(f"  {lift}: no sessions found")

        lines.append(
            "\nTo fetch detailed history for a lift, use get_lift_history(exercise, weeks=N). "
            "For full history set weeks=24."
        )
        return "\n".join(lines)
    except Exception as e:
        return f"Could not load data summary: {e}"


def _tool_get_health_log(days: int = 14) -> str:
    """Return recent health log entries."""
    from datetime import date as _date, timedelta
    try:
        from memory import read_health_log
        days = min(days, 90)
        cutoff = str(_date.today() - timedelta(days=days))
        all_entries = read_health_log(limit=days + 10)
        entries = [e for e in all_entries if e.get("Date", "") >= cutoff]
        if not entries:
            return f"No health log entries in the last {days} days."
        lines = []
        for e in entries:
            parts = [f"[{e.get('Date','?')}]"]
            if e.get("Bodyweight (kg)"): parts.append(f"BW:{e['Bodyweight (kg)']}kg")
            if e.get("Sleep (hrs)"): parts.append(f"sleep:{e['Sleep (hrs)']}h")
            if e.get("Food Quality (1-10)"): parts.append(f"food:{e['Food Quality (1-10)']}/10")
            if e.get("Steps"): parts.append(f"steps:{e['Steps']}")
            if e.get("Notes"): parts.append(f"note:{e['Notes'][:60]}")
            lines.append("  " + " | ".join(parts))
        return f"Health log (last {days} days, {len(entries)} entries):\n" + "\n".join(lines)
    except Exception as e:
        return f"Could not load health log: {e}"


def _tool_log_lift(exercise: str, weight: str, sets_reps: str,
                   date_str: str = None, notes: str = "", completed: bool = True) -> str:
    """Append a lift session to Lift History with auto-computed Est 1RM."""
    from datetime import date as _date
    try:
        from memory import append_lift_history, compute_epley
        from config import compute_current_week, resolve_program_start_date

        log_date = date_str or str(_date.today())
        week_num = compute_current_week(resolve_program_start_date())
        est_1rm = compute_epley(weight, sets_reps)

        session = {
            "date": log_date,
            "week": str(week_num),
            "day_label": f"Telegram_{log_date}",
            "exercise_name": exercise,
            "prescribed_weight": weight,
            "actual": f"{weight} {sets_reps}".strip(),
            "completed": completed,
            "notes": notes or "[logged via Telegram]",
            "sets_reps": sets_reps,
        }
        if est_1rm is not None:
            session["est_1rm"] = est_1rm

        append_lift_history([session])
        est_str = f" | Est 1RM: {est_1rm}kg" if est_1rm else ""
        return f"Logged: {exercise} {weight} {sets_reps} on {log_date}{est_str}."
    except Exception as e:
        return f"Could not log lift: {e}"


def _tool_log_bodyweight(weight_kg: float, date_str: str = None, notes: str = "") -> str:
    """Append a bodyweight entry to the Health Log."""
    from datetime import date as _date
    try:
        from memory import append_health_log

        log_date = date_str or str(_date.today())
        append_health_log([{
            "date": log_date,
            "bodyweight": str(weight_kg),
            "notes": notes or "[logged via Telegram]",
        }])
        return f"Logged bodyweight: {weight_kg}kg on {log_date}."
    except Exception as e:
        return f"Could not log bodyweight: {e}"


def _tool_update_program(operation: str, value: str, exercise: str = None, week: int = None) -> str:
    """
    Write a change to the active program sheet.
    note_add: applied immediately via writeback.
    All other ops: logged as PENDING_PROPOSAL for athlete confirmation.
    """
    from config import compute_current_week, resolve_program_start_date, PROGRAM_SHEET_ID
    try:
        current_week = week or compute_current_week(resolve_program_start_date())

        if operation == "note_add":
            # Low-stakes — apply immediately
            from writeback import apply_writeback
            proposal = (
                f"NOTE_ADD | week={current_week} | exercise={exercise or 'general'} | note={value}"
            )
            success, msg = apply_writeback(proposal, current_week=current_week, program_sheet_id=PROGRAM_SHEET_ID)
            if success:
                return f"Note added to {exercise or 'program'} (Week {current_week}): \"{value}\""
            return f"Could not add note: {msg}"

        # High-stakes ops → PENDING_PROPOSAL (requires athlete confirmation)
        op_map = {
            "weight_change": "WEIGHT_CHANGE",
            "sets_reps_change": "SETS_REPS_CHANGE",
            "exercise_swap": "EXERCISE_SWAP",
        }
        op_tag = op_map.get(operation, "UNKNOWN")
        proposal_text = (
            f"{op_tag} | week={current_week} | exercise={exercise or '?'} | new_value={value}"
        )
        from memory import append_command, read_commands
        from run_coach import log_pending_proposal
        existing = read_commands()
        log_pending_proposal(proposal_text, existing)
        return (
            f"Proposal created: {op_tag} on {exercise or '?'} → {value} (Week {current_week}). "
            f"Reply 'yes' to confirm or 'no' to cancel."
        )
    except Exception as e:
        return f"update_program failed: {e}"


def _tool_get_program_comparison() -> str:
    """Return cross-program 1RM comparison via the projections engine."""
    try:
        from memory import read_all, read_sheet_registry
        from projections import compare_program_progress
        memory_data = read_all()
        lift_history = memory_data.get("lift_history", [])
        program_registry = read_sheet_registry()
        # Build minimal current_program_info from registry
        active = next((p for p in program_registry if p.get("Status", "").upper() == "ACTIVE"), None)
        current_program_info = {}
        if active:
            current_program_info = {
                "name": active.get("Name", "Current Program"),
                "start_date": active.get("Start Date", ""),
                "total_weeks": active.get("Total Weeks", ""),
            }
        return compare_program_progress(lift_history, program_registry, current_program_info) or "No program comparison data available yet."
    except Exception as e:
        return f"Could not compare programs: {e}"


_CACHEABLE_TOOLS = {"get_coach_brain", "get_lift_history", "get_projections",
                    "get_data_summary", "get_program_comparison"}


def _execute_data_tool(name: str, inp: dict) -> str:
    """Dispatch a data-fetching tool call synchronously."""
    # Check cache for read tools
    cache_key = f"{name}:{sorted(inp.items())}" if name in _CACHEABLE_TOOLS else None
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            print(f"[Cache] HIT {name}")
            return cached

    try:
        if name == "get_coach_brain":
            result = _tool_get_coach_brain()
        elif name == "get_lift_history":
            result = _tool_get_lift_history(inp.get("exercise", ""), inp.get("weeks", 8))
        elif name == "get_program_week":
            result = _tool_get_program_week(inp.get("week_num"), inp.get("sheet_id"))
        elif name == "list_programs":
            result = _tool_list_programs()
        elif name == "get_projections":
            result = _tool_get_projections()
        elif name == "get_data_summary":
            result = _tool_get_data_summary()
        elif name == "get_program_comparison":
            result = _tool_get_program_comparison()
        elif name == "log_lift":
            result = _tool_log_lift(
                inp.get("exercise", ""), inp.get("weight", ""), inp.get("sets_reps", ""),
                inp.get("date"), inp.get("notes", ""), inp.get("completed", True)
            )
        elif name == "get_health_log":
            result = _tool_get_health_log(inp.get("days", 14))
        elif name == "log_bodyweight":
            result = _tool_log_bodyweight(inp.get("weight_kg", 0), inp.get("date"), inp.get("notes", ""))
        elif name == "update_program":
            result = _tool_update_program(
                inp.get("operation", "note_add"), inp.get("value", ""),
                inp.get("exercise"), inp.get("week")
            )
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error ({name}): {e}"

    if cache_key:
        _cache_set(cache_key, result)
    return result


async def _send_chart_tool(chart_type: str, chat_id: int, bot) -> str:
    """Generate a chart and send it to Telegram. Returns status string."""
    try:
        from memory import read_lift_history, read_tracked_lifts, read_health_log
        from charts import generate_1rm_chart, generate_bodyweight_chart, generate_volume_chart

        if chart_type == "1rm":
            history = read_lift_history(limit=200)
            tracked = read_tracked_lifts()
            buf = generate_1rm_chart(history, tracked)
        elif chart_type == "bodyweight":
            health = read_health_log(limit=180)
            buf = generate_bodyweight_chart(health)
        elif chart_type == "volume":
            from sheets import read_program_data
            from config import compute_current_week, resolve_program_start_date
            week_num = compute_current_week(resolve_program_start_date())
            program_data = read_program_data(week_num=week_num, lookback=6)
            buf = generate_volume_chart(
                program_data.get("recent_weeks", []),
                program_data.get("current_week"),
            )
        else:
            return f"Unknown chart type: {chart_type}"

        if buf:
            await bot.send_photo(chat_id=chat_id, photo=buf)
            return f"{chart_type.upper()} chart sent."
        else:
            return "Not enough data to generate a chart yet (need at least a few sessions)."
    except Exception as e:
        return f"Chart generation failed: {e}"


def _get_recent_conversation_turns(limit_pairs: int = 3) -> list[dict]:
    """
    Return the last N complete conversation pairs from Telegram Log as Claude message turns.
    Always returns a valid alternating sequence ending with an assistant turn,
    so the current user message can be appended without format errors.
    """
    try:
        from memory import read_telegram_log
        log = read_telegram_log(limit=limit_pairs * 2 + 4)

        turns = []
        for entry in log:
            direction = entry.get("Direction", "")
            msg = entry.get("Message", "").strip()
            if not msg:
                continue
            role = "user" if direction == "IN" else "assistant"
            # Skip system/command-only entries like "[chart: 1rm]"
            if msg.startswith("[") and msg.endswith("]"):
                continue
            turns.append({"role": role, "content": msg})

        # Merge consecutive same-role turns
        merged: list[dict] = []
        for turn in turns:
            if merged and merged[-1]["role"] == turn["role"]:
                merged[-1]["content"] += "\n" + turn["content"]
            else:
                merged.append({"role": turn["role"], "content": turn["content"]})

        # Must start with user
        while merged and merged[0]["role"] == "assistant":
            merged.pop(0)

        # Must end with assistant so we can add the current user turn cleanly
        while merged and merged[-1]["role"] == "user":
            merged.pop()

        # Cap at limit_pairs exchanges
        if len(merged) > limit_pairs * 2:
            merged = merged[-(limit_pairs * 2):]

        return merged
    except Exception:
        return []


async def _generate_response_with_tools(user_message: str, chat_id: int, bot,
                                         intent: str = "GENERAL") -> str:
    """
    Generate a coaching response using Claude tool use.

    Claude decides which data to fetch rather than receiving everything pre-loaded.
    Pre-seeds recent conversation turns for continuity.
    Loops until Claude produces a final text response or tool limit is reached.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if intent == "WORKOUT":
        focus_note = (
            "The athlete is asking about today's session or workout adaptation. "
            "Call get_program_week first to see what's prescribed, then get_lift_history if needed. "
            "Give specific advice: exact weights, sets/reps, substitutions if needed. "
            "Log what they actually did using log_lift if they report performance.\n\n"
        )
    elif intent == "HEALTH":
        focus_note = (
            "The athlete is asking about health, recovery, nutrition, or body composition. "
            "Call get_health_log first to see recent data (sleep, BW, energy, food quality). "
            "Be specific — reference actual numbers if available. "
            "Connect health signals to training impact. One insight + one action beats a list.\n\n"
        )
    else:
        focus_note = ""

    system = (
        f"You are {ATHLETE_NAME}'s strength coach — the same coach who writes the daily email. "
        "One coach, two channels. Telegram is the primary coaching channel: real-time, direct, conversational. "
        "Email is the daily structured check-in. Everything here feeds back into the email and vice versa.\n\n"
        "Call get_coach_brain first — it includes a COACHING CONTEXT cascade (4 layers: strategic, mesocycle, "
        "active commitments, last 24h) AND the full Coach State.\n\n"
        "COHERENCE RULE: Before making any claim about today's training session, read the COACHING CONTEXT "
        "cascade in get_coach_brain output. Derive today's session from Layer 1 (strategic) + Layer 2 "
        "(weekly intent) — do NOT re-derive independently from scratch. Validate against Layer 3 "
        "(ACTIVE COMMITMENTS). If a CONFLICT exists in Layer 3, surface it to the athlete — never "
        "silently pick one side.\n\n"
        "NARRATION RULE: Every coaching response must include at least ONE sentence that shows reasoning. "
        "Pattern: 'Veo que [dato] — esto [qué significa] — [recomendación concreta].' "
        "Do not just state what to do — explain why based on actual data from the context.\n\n"
        "HONESTY RULE: Do NOT promise future automated actions (protocols, check-ins, emails) that depend "
        "on cron jobs you do not control. If the athlete says an automated message didn't arrive, "
        "say honestly: 'El pipeline automático puede haber fallado — no puedo verificarlo desde aquí.' "
        "Never invent a future delivery or promise a check-in to satisfy the question.\n\n"
        + focus_note +
        "Read tools: get_coach_brain, get_lift_history, get_program_week, list_programs, "
        "get_projections, get_data_summary, get_health_log\n"
        "Write tools: log_lift (record a session), log_bodyweight (record weight)\n"
        "Visual: send_chart\n\n"
        "Rules:\n"
        "- Never invent numbers. Fetch or admit you don't have it.\n"
        "- If athlete mentions a lift or bodyweight, log it immediately with the write tools.\n"
        "- If data looks sparse, call get_data_summary, then fetch more with weeks=24.\n"
        "- For past programs, use list_programs → get_program_week with sheet_id.\n"
        "- Be concise: 1-4 sentences. No headers. Natural coach voice.\n"
        "- Est 1RM is from prescribed weights unless athlete logged actuals.\n"
        "- If athlete states a channel preference ('reach me on Telegram', 'use email for X', etc.), "
        "acknowledge it immediately. It gets saved to their preferences automatically and will take effect."
    )

    # Pre-seed with recent conversation for continuity
    history = _get_recent_conversation_turns(limit_pairs=3)
    messages = history + [{"role": "user", "content": f"Athlete message: {user_message}"}]

    max_rounds = 8
    total_in, total_out = 0, 0

    for _ in range(max_rounds):
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=600,
            system=system,
            tools=_TOOL_DEFINITIONS,
            messages=messages,
        )

        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens

        # Collect any text from this turn
        text_parts = [b.text for b in response.content if hasattr(b, "text") and b.text]

        if response.stop_reason == "end_turn":
            _log_token_cost(total_in, total_out, intent)
            return " ".join(text_parts).strip() or "(No response)"

        if response.stop_reason != "tool_use":
            _log_token_cost(total_in, total_out, intent)
            return " ".join(text_parts).strip() or f"(Unexpected stop: {response.stop_reason})"

        # Execute tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            name = block.name
            inp = block.input or {}
            print(f"[ToolUse] {name}({inp})")

            if name == "send_chart":
                result = await _send_chart_tool(inp.get("chart_type", "1rm"), chat_id, bot)
            else:
                result = _execute_data_tool(name, inp)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    _log_token_cost(total_in, total_out, intent)
    return "(Response generation exceeded tool round limit — please try again.)"


def _log_token_cost(input_tokens: int, output_tokens: int, label: str = "") -> None:
    """Print token usage and persist to Coach Log. Sonnet: $3/M in, $15/M out."""
    cost = (input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0)
    print(f"[Cost] {label} | in={input_tokens} out={output_tokens} | ~${cost:.4f}")
    try:
        from memory import log_coach_run
        log_coach_run(
            observations=f"[Telegram/{label}] in={input_tokens} out={output_tokens}",
            email_summary="",
            cost_usd=cost,
        )
    except Exception:
        pass  # non-fatal — cost logging must never break the bot


async def _process_incoming_message_background() -> None:
    """
    Run the Telegram processor in a thread-pool so it doesn't block the bot response.
    Picks up the just-logged message and extracts structured facts into memory immediately.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        from processor import process_telegram_messages
        processed = await loop.run_in_executor(None, process_telegram_messages)
        if processed:
            print(f"[Processor] Background run: {processed} message(s) processed")
    except Exception as e:
        print(f"[Processor] Background run failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Confirmation flow for PENDING_PROPOSALs
# ---------------------------------------------------------------------------

_CONFIRM_WORDS = {"yes", "yep", "yeah", "confirm", "confirmed", "do it", "go ahead",
                  "sí", "si", "dale", "ok", "okay", "sure"}
_DECLINE_WORDS = {"no", "nope", "cancel", "reject", "rejected", "don't", "dont",
                  "stop", "forget it", "never mind", "nevermind"}

# Phrases that end an active SKIP_UNTIL (resume emails)
_RESUME_PHRASES = ["resume emails", "end skip", "cancel skip", "unpause", "resume coaching",
                   "reanudar emails", "fin de pausa"]


def _end_skip_until() -> bool:
    """
    Mark any active SKIP_UNTIL command as Applied=Y, effectively ending the email pause.
    Returns True if a SKIP_UNTIL was found and cleared, False otherwise.
    """
    try:
        from memory import read_commands, mark_command_applied
        commands = read_commands()
        cleared = False
        for cmd in commands:
            if (cmd.get("Command", "").upper().strip() == "SKIP_UNTIL"
                    and cmd.get("Applied", "").upper().strip() not in ("Y", "DECLINED")):
                row_index = cmd.get("_row_index")
                if row_index:
                    mark_command_applied(row_index)
                    cleared = True
        return cleared
    except Exception as e:
        print(f"[Telegram] End-skip failed (non-fatal): {e}")
        return False


def _get_pending_proposals() -> list[dict]:
    """Return unapplied PENDING_PROPOSAL rows from Commands tab."""
    try:
        from memory import read_commands
        return [
            c for c in read_commands()
            if c.get("Command", "").upper() == "PENDING_PROPOSAL"
            and c.get("Applied", "").upper() not in ("Y", "DECLINED")
        ]
    except Exception:
        return []


def _resolve_proposal(row_index: int, decision: str, proposal_text: str) -> str:
    """Mark proposal applied/declined, handle program activation, log to Coach Focus."""
    extra = ""
    try:
        from memory import mark_command_applied, append_coach_focus
        if decision == "Y":
            mark_command_applied(row_index)
            append_coach_focus("LANDMARK", f"[Confirmed via Telegram] {proposal_text}")
            # If this looks like a new program creation, activate it
            lower = proposal_text.lower()
            if any(kw in lower for kw in ("created", "new program", "activate", "weeks,")):
                try:
                    from memory import activate_pending_program
                    name = activate_pending_program()
                    if name:
                        extra = f"\n\nProgram **{name}** is now ACTIVE. Old program marked COMPLETED."
                        append_coach_focus("LANDMARK", f"[Program activated] {name} → ACTIVE")
                        print(f"  [ProgramTransition] Activated: {name}")
                except Exception as e:
                    print(f"  [ProgramTransition] Failed (non-fatal): {e}")
        else:
            from memory import _get_memory_sheet, TAB_COMMANDS, COMMANDS_HEADERS
            sheet = _get_memory_sheet()
            ws = sheet.worksheet(TAB_COMMANDS)
            applied_col = COMMANDS_HEADERS.index("Applied") + 1
            ws.update_cell(row_index, applied_col, "DECLINED")
            append_coach_focus("LANDMARK", f"[Declined via Telegram] {proposal_text}")
    except Exception as e:
        print(f"[Telegram] Proposal resolution failed (non-fatal): {e}")
    return extra


async def _handle_confirmation(update: Update, user_text: str) -> bool:
    """
    Check if the message is a yes/no response to a pending proposal.
    Returns True if handled (and no further processing needed), False otherwise.
    """
    words = set(user_text.lower().split())
    is_yes = bool(words & _CONFIRM_WORDS)
    is_no = bool(words & _DECLINE_WORDS)

    if not is_yes and not is_no:
        return False

    proposals = _get_pending_proposals()
    if not proposals:
        return False  # Not a confirmation context — treat as normal message

    if len(proposals) == 1:
        p = proposals[0]
        proposal_text = p.get("Value", "")
        row_index = p.get("_row_index")

        if is_yes:
            transition_msg = _resolve_proposal(row_index, "Y", proposal_text)

            # Attempt write-back immediately
            wb_msg = ""
            try:
                from writeback import apply_writeback
                from config import compute_current_week, resolve_program_start_date
                current_week = compute_current_week(resolve_program_start_date())
                success, wb_result = apply_writeback(
                    proposal_text, current_week=current_week
                )
                if success:
                    wb_msg = f" Done — {wb_result}."
                else:
                    wb_msg = f" Note: couldn't auto-update the sheet ({wb_result})."
                print(f"  [WriteBack] {wb_result}")
            except Exception as e:
                wb_msg = " (Sheet update failed — please update manually.)"
                print(f"  [WriteBack] Error: {e}")

            reply = f"Confirmed.{wb_msg} I'll reference this in the next email.{transition_msg}"
        else:
            _resolve_proposal(row_index, "DECLINED", proposal_text)
            reply = "Understood, I won't make that change."

        await update.message.reply_text(reply)
        _log_message("OUT", reply)
        return True

    # Multiple proposals — ask which one
    proposal_list = "\n".join(
        f"{i+1}. {p.get('Value', '')[:100]}" for i, p in enumerate(proposals)
    )
    reply = (
        f"I have {len(proposals)} pending proposals. Which one are you confirming?\n\n"
        f"{proposal_list}\n\nReply with the number."
    )
    await update.message.reply_text(reply)
    _log_message("OUT", reply)
    return True


# ---------------------------------------------------------------------------
# Log to Coach Memory
# ---------------------------------------------------------------------------

def _log_message(direction: str, text: str) -> None:
    """Log a Telegram message to Coach Memory (best-effort, non-fatal)."""
    try:
        from memory import append_telegram_log
        append_telegram_log(direction=direction, message=text)
    except Exception as e:
        print(f"[Telegram] Log failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Guard: only respond to the athlete's chat
# ---------------------------------------------------------------------------

def _is_authorized(update: Update) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True  # No restriction set — allow all (dev mode)
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_endsession_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /endsession command — triggers structured post-session check-in immediately."""
    if not _is_authorized(update):
        return
    extra = " ".join(context.args) if context.args else ""
    user_text = extra or "session done"
    _log_message("IN", f"/endsession {extra}".strip())
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        from run_coach import run_endsession_protocol
        response = await loop.run_in_executor(
            None, lambda: run_endsession_protocol(user_message=user_text, dry_run=False)
        )
    except Exception as e:
        print(f"[EndSession/cmd] Failed: {e}")
        response = "No pude cargar el check-in ahora mismo — mándame los datos directamente."
    await update.message.reply_text(response)
    _log_message("OUT", response)


async def handle_start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    greeting = (
        f"Hey {ATHLETE_NAME}. I'm here whenever you need me — ask about training, "
        "progress, how you're tracking, anything. What's on your mind?"
    )
    await update.message.reply_text(greeting)
    _log_message("OUT", greeting)


async def handle_summary(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    await update.message.reply_text("Pulling your summary...")

    ctx = _build_bot_context()
    response = _generate_response(
        "Give me a quick summary of how training is going this week — key numbers, momentum, anything I should know.",
        ctx,
        HAIKU_MODEL,
    )
    await update.message.reply_text(response)
    _log_message("IN", "/summary")
    _log_message("OUT", response)


async def handle_data(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a quick summary of what training data is available (/data)."""
    if not _is_authorized(update):
        return
    await update.message.reply_text("Checking available data...")
    result = _tool_get_data_summary()
    await update.message.reply_text(result)
    _log_message("IN", "/data")
    _log_message("OUT", "[data summary]")


async def handle_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show this week's full schedule with done/not done status."""
    if not _is_authorized(update):
        return

    args = context.args
    try:
        week_num = int(args[0]) if args else None
    except (ValueError, IndexError):
        week_num = None

    result = _tool_get_program_week(week_num=week_num)

    # Annotate with completion summary
    lines = result.split("\n")
    done = sum(1 for l in lines if "✓" in l and l.strip().startswith(("    ")))
    total = sum(1 for l in lines if l.strip().startswith(("    ")) and ":" in l)
    if total:
        summary = f"\n{done}/{total} exercises completed"
        result += summary

    await update.message.reply_text(result[:4000])
    _log_message("IN", "/week")
    _log_message("OUT", f"[week schedule, {done}/{total} done]")


async def handle_compare(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show cross-program 1RM comparison (/compare)."""
    if not _is_authorized(update):
        return
    await update.message.reply_text("Comparing across programs...")
    result = _tool_get_program_comparison()
    await update.message.reply_text(result[:4000])
    _log_message("IN", "/compare")
    _log_message("OUT", "[cross-program comparison]")


async def handle_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a progress chart. Usage: /chart [1rm|bodyweight|volume]"""
    if not _is_authorized(update):
        return

    args = context.args
    chart_type = args[0].lower() if args else "1rm"

    # Aliases
    if chart_type in ("bw", "weight"):
        chart_type = "bodyweight"

    if chart_type not in ("1rm", "bodyweight", "volume"):
        await update.message.reply_text(
            "Available charts:\n/chart 1rm — estimated 1RM over time\n"
            "/chart bodyweight — bodyweight trend\n/chart volume — weekly training volume"
        )
        return

    await update.message.reply_text(f"Generating {chart_type} chart...")
    result = await _send_chart_tool(chart_type, update.effective_chat.id, context.bot)

    # _send_chart_tool sends the photo itself; only reply if there was an error
    if "sent" not in result.lower():
        await update.message.reply_text(result)

    _log_message("IN", f"/chart {chart_type}")
    _log_message("OUT", f"[chart: {chart_type}] {result}")


def _classify_intent(message: str) -> str:
    """
    Use Haiku to classify the athlete's message into one of five routing categories:
      WORKOUT  — today's session, substitutions, adaptation, specific exercises/sets/reps
      HEALTH   — nutrition, recovery, sleep, blood tests, HRV, injury, supplement questions
      PROGRAM  — structural program change requests (new block, periodization, deload week)
      META     — asking the coach to self-critique, suggest improvements, or explain its reasoning
      GENERAL  — everything else (progress check, motivation, life context, chat)

    Returns one of: "WORKOUT" | "HEALTH" | "PROGRAM" | "META" | "GENERAL"
    Defaults to "GENERAL" on any error.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        result = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=10,
            system=(
                "You are a message router for a strength coaching app. "
                "Classify the athlete's message into exactly one category:\n"
                "ENDSESSION — athlete signals they just finished a training session. Signals: "
                "'/endsession', 'just finished training', 'done with the session', 'session done', "
                "'workout done', 'just got done', 'finished my workout', 'salí del gym', "
                "'terminé el entreno', 'acabo de entrenar', 'terminé de entrenar', "
                "'acabo de salir', 'terminé la sesión'\n"
                "WORKOUT — questions about today's session, exercise substitutions, sets/reps/weights, "
                "fatigue during training, skipping/modifying a session\n"
                "HEALTH — nutrition, recovery, sleep, blood tests, HRV, injury/pain management, supplements\n"
                "PROGRAM — requests to restructure the ENTIRE training program: create a new block, "
                "add a deload week, redesign the overall periodization plan, switch programs entirely\n"
                "META — athlete asks the coach to self-critique, suggest improvements to itself, explain "
                "its reasoning, review its own coaching quality, or do a meta-analysis. Examples: "
                "'how could you improve?', 'what are you missing?', 'critique your coaching', "
                "'suggest updates to how you coach me', 'meta coach', 'improve yourself'\n"
                "GENERAL — everything else: progress updates, checking in, motivation, life context, "
                "adding notes or comments to the sheet, simple weight/reps tweaks, anything that is NOT "
                "a full program redesign\n\n"
                "Reply with exactly one word: ENDSESSION, WORKOUT, HEALTH, PROGRAM, META, or GENERAL."
            ),
            messages=[{"role": "user", "content": message}],
        )
        intent = result.content[0].text.strip().upper()
        if intent in ("ENDSESSION", "WORKOUT", "HEALTH", "PROGRAM", "META", "GENERAL"):
            return intent
        return "GENERAL"
    except Exception as e:
        print(f"[Router] Classification failed (defaulting to GENERAL): {e}")
        return "GENERAL"


def _parse_rpe_reply(text: str, exercises: list) -> dict:
    """
    Parse an athlete's RPE reply into {exercise_name: rpe_value_str} pairs.

    Handles:
      - Named:      "squat 8, bench 7.5"  → {Squat: "8", Bench: "7.5"}
      - Positional: "8, 7" (same count as exercises) → {Squat: "8", Bench: "7"}
      - Mixed:      partial named match, positional fills the rest

    Only accepts values 1–10 as valid RPE numbers.
    Returns empty dict if nothing parseable.
    """
    import re as _re
    result = {}
    text_lower = text.lower()

    # Try named pattern first
    for ex in exercises:
        ex_lower = ex.lower()
        m = _re.search(
            r"\b" + _re.escape(ex_lower) + r"[\s:=→\-]*(\d+(?:\.\d+)?)\b",
            text_lower
        )
        if m:
            val = float(m.group(1))
            if 1 <= val <= 10:
                result[ex] = m.group(1)

    # If named matched all exercises → done
    if len(result) == len(exercises):
        return result

    # Positional fallback — only if nothing named matched yet
    if not result:
        nums = _re.findall(r"\b(\d+(?:\.\d+)?)\b", text)
        valid_nums = [n for n in nums if 1 <= float(n) <= 10]
        if len(valid_nums) == len(exercises):
            for ex, num in zip(exercises, valid_nums):
                result[ex] = num

    return result


def _maybe_write_rpe_from_reply(user_text: str, current_flow: str) -> None:
    """
    If the athlete's reply contains RPE data matching exercises from the active
    endsession thread, write those values to the program sheet.

    current_flow format: "endsession | DATE | SESSION_LABEL | asked: RPE for Ex1, Ex2"
    Silent on any failure — this is best-effort.
    """
    import re as _re
    try:
        # Extract "asked: RPE for Ex1, Ex2" part from current_flow
        asked_match = _re.search(r"asked:\s*RPE for\s*(.+)$", current_flow, _re.I)
        if not asked_match:
            return

        exercises_raw = asked_match.group(1).strip()
        # Split by comma or semicolon
        exercises = [e.strip() for e in _re.split(r"[,;]", exercises_raw) if e.strip()]
        if not exercises:
            return

        rpe_map = _parse_rpe_reply(user_text, exercises)
        if not rpe_map:
            return

        from config import compute_current_week, resolve_program_start_date, PROGRAM_SHEET_ID
        from sheets import get_client
        from writeback import _apply_rpe_log

        week_num = compute_current_week(resolve_program_start_date())
        client = get_client()
        sheet = client.open_by_key(PROGRAM_SHEET_ID)

        for exercise_name, rpe_value in rpe_map.items():
            op = {"week": week_num, "exercise": exercise_name, "rpe": rpe_value}
            success, msg = _apply_rpe_log(sheet, op)
            print(f"  [RPE Reply Write-back] {msg}")

    except Exception as e:
        print(f"  [RPE Reply Write-back] Non-fatal: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await update.message.reply_text("Sorry, I only talk to my athlete.")
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    _log_message("IN", user_text)

    # Run processor in background immediately — extracts structured facts into memory
    # without blocking the bot response
    import asyncio as _asyncio
    _asyncio.create_task(_process_incoming_message_background())

    # Check for SKIP_UNTIL control phrases
    lower = user_text.lower()
    if any(phrase in lower for phrase in _RESUME_PHRASES):
        cleared = _end_skip_until()
        if cleared:
            reply = "Done — emails resume tonight. I'll be in touch as usual."
        else:
            reply = "No active email pause found. Emails are already running normally."
        await update.message.reply_text(reply)
        _log_message("OUT", reply)
        return

    # Check for yes/no confirmation of a pending proposal before normal routing
    if await _handle_confirmation(update, user_text):
        return

    # Show typing indicator while generating
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
    )

    # V17: Iteration Zero intercept — must run BEFORE intent routing so all replies are captured
    try:
        from iteration_zero import read_iteration_zero_state, handle_iteration_zero_reply
        _iz_state = read_iteration_zero_state()
        if _iz_state.get("status") in ("IN_PROGRESS", "COVERAGE_TESTING"):
            import asyncio as _aio_iz
            _loop_iz = _aio_iz.get_event_loop()
            _iz_response = await _loop_iz.run_in_executor(
                None, lambda: handle_iteration_zero_reply(user_text)
            )
            if _iz_response:
                await update.message.reply_text(_iz_response)
                _log_message("OUT", _iz_response)
                return
    except Exception as _iz_err:
        print(f"  [IterationZero] Non-fatal: {_iz_err}")

    # Classify intent with Haiku — single fast call instead of cascading keyword checks
    intent = _classify_intent(user_text)
    print(f"[Router] Intent: {intent} | Message: {user_text[:60]}")

    if intent == "PROGRAM":
        ctx = _build_bot_context()
        try:
            from program_agent import respond as program_respond
            await update.message.reply_text("Thinking about your program... give me 30 seconds.")
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            response = program_respond(user_text, ctx)
            if len(response) > 4000:
                await update.message.reply_text(response[:4000])
                await update.message.reply_text(response[4000:])
            else:
                await update.message.reply_text(response)
            _log_message("OUT", response)
            return
        except Exception as e:
            print(f"[ProgramDesigner] Failed (falling back): {e}")

    elif intent == "HEALTH":
        try:
            response = await _generate_response_with_tools(
                user_text, update.effective_chat.id, context.bot, intent="HEALTH"
            )
            await update.message.reply_text(response)
            _log_message("OUT", response)
            return
        except Exception as e:
            print(f"[HealthToolUse] Failed (falling back): {e}")

    elif intent == "WORKOUT":
        try:
            response = await _generate_response_with_tools(
                user_text, update.effective_chat.id, context.bot, intent="WORKOUT"
            )
            await update.message.reply_text(response)
            _log_message("OUT", response)
            return
        except Exception as e:
            print(f"[WorkoutToolUse] Failed (falling back): {e}")

    elif intent == "ENDSESSION":
        try:
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            from run_coach import run_endsession_protocol
            response = await loop.run_in_executor(
                None, lambda: run_endsession_protocol(user_message=user_text, dry_run=False)
            )
            await update.message.reply_text(response)
            _log_message("OUT", response)
            return
        except Exception as e:
            print(f"[EndSession] Failed (falling back to GENERAL): {e}")

    elif intent == "META":
        try:
            await update.message.reply_text(
                "Running a self-critique now — give me 20 seconds..."
            )
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="typing"
            )
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            from run_coach import run_meta_improvement
            analysis = await loop.run_in_executor(None, lambda: run_meta_improvement(dry_run=False))
            # run_meta_improvement already sends the Telegram message; just log it
            _log_message("OUT", f"[meta-improvement] {analysis[:200]}")
            return
        except Exception as e:
            print(f"[Meta] Failed (falling back): {e}")
            # Fall through to GENERAL handler

    # If we're in an active endsession RPE thread, try to parse and write RPE values
    # from the reply before passing to the GENERAL tool-use loop for coach acknowledgment
    try:
        from datetime import date as _date
        _today_str = str(_date.today())
        from memory import read_coach_state as _rcs
        _cs = _rcs()
        _flow = _cs.get("CURRENT_FLOW", {}).get("Summary", "") or _cs.get("CURRENT_FLOW", {}).get("summary", "")
        if _flow and _flow.startswith(f"endsession | {_today_str}") and "asked: RPE" in _flow:
            import asyncio as _aio2
            loop2 = _aio2.get_event_loop()
            await loop2.run_in_executor(None, lambda: _maybe_write_rpe_from_reply(user_text, _flow))
    except Exception as _rpe_err:
        print(f"  [RPE intercept] Non-fatal: {_rpe_err}")

    # GENERAL intent (or fallback from failed specialized agent/workout/meta) — tool use
    try:
        response = await _generate_response_with_tools(
            user_text, update.effective_chat.id, context.bot, intent="GENERAL"
        )
    except Exception as e:
        print(f"[ToolUse] Failed, falling back to context injection: {e}")
        ctx = _build_bot_context()
        response = _generate_response(user_text, ctx, _choose_model(user_text))

    await update.message.reply_text(response)
    _log_message("OUT", response)


# ---------------------------------------------------------------------------
# Health data ingestion — document (PDF) and photo uploads
# ---------------------------------------------------------------------------

async def _extract_pdf_text(file_bytes: bytes) -> str:
    """Extract text from a PDF byte string using pypdf."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages).strip()
        return text if text else "(PDF had no extractable text — may be scanned image)"
    except ImportError:
        return "(pypdf not installed — cannot extract PDF text)"
    except Exception as e:
        return f"(PDF extraction failed: {e})"


async def _extract_photo_text(file_bytes: bytes) -> str:
    """
    Use Claude vision to extract health data from a photo (blood test screenshot,
    watch summary, nutrition label, etc.). Returns extracted text/values.
    """
    import base64
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    response = client.messages.create(
        model=SONNET_MODEL,  # vision requires Sonnet
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    }
                },
                {
                    "type": "text",
                    "text": (
                        "This is a health data image (blood test, watch summary, nutrition, etc.). "
                        "Extract all numerical values, markers, and relevant health information as structured text. "
                        "List every metric you can read: name, value, unit, reference range if shown. "
                        "Be thorough — the coach will use this to give personalized advice."
                    )
                }
            ]
        }]
    )
    return response.content[0].text


def _infer_file_type(caption: str, fallback: str = "health_data") -> str:
    """
    Infer what kind of health data a file/photo contains based on the caption.
    Returns a short string used as the file_type key in HealthAgent's extra_data dict.
    """
    if not caption:
        return fallback
    lower = caption.lower()
    if any(w in lower for w in ["blood", "sangre", "analítica", "analitica", "lab", "ferritin",
                                  "hemoglobin", "glucose", "cholesterol", "tsh", "creatinine"]):
        return "blood_data"
    if any(w in lower for w in ["hrv", "heart rate", "watch", "garmin", "oura", "whoop",
                                  "sleep score", "recovery", "readiness"]):
        return "hrv_data"
    if any(w in lower for w in ["food", "meal", "nutrition", "macros", "calories", "comida",
                                  "dieta", "proteína", "proteina"]):
        return "nutrition_data"
    if any(w in lower for w in ["photo", "picture", "foto", "progress", "body", "physique"]):
        return "body_photo"
    return fallback


async def _handle_health_file(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               extracted_text: str, file_type: str,
                               caption: str = "") -> None:
    """
    Common handler: given extracted text from a PDF/photo, call HealthAgent and reply.
    Also persists the raw extracted data to the Health Log for future reference.
    """
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    from datetime import date as _date
    today = str(_date.today())

    # Persist extracted data to Health Log + Coach Focus
    try:
        from memory import append_health_log, append_coach_focus
        # Store full extracted text in Notes; flag file type in the entry
        source_label = "PDF" if file_type == "blood_data" and "pdf" in file_type.lower() else file_type.upper()
        append_health_log([{
            "date": today,
            "notes": f"[{source_label} upload] {extracted_text[:500]}",
        }])
        append_coach_focus(
            "TRACKING",
            f"[Health file uploaded {today}] {file_type}: {extracted_text[:120]}",
            last_mentioned=today,
        )
        print(f"[Telegram] Health data persisted to Health Log ({len(extracted_text)} chars)")
    except Exception as e:
        print(f"[Telegram] Health data persist failed (non-fatal): {e}")

    # Use caption as the user's question (if any), otherwise a generic prompt
    user_question = caption.strip() if caption else f"Here's my {file_type} data — what do you see?"
    _log_message("IN", f"[{file_type.upper()} upload] {caption or '(no caption)'}")

    ctx = _build_bot_context()
    try:
        from health_agent import respond as health_respond
        response = health_respond(
            user_message=user_question,
            base_context=ctx,
            extra_data={file_type: extracted_text}
        )
    except Exception as e:
        response = f"Got the {file_type} but hit an error analysing it: {e}"

    await update.message.reply_text(response)
    _log_message("OUT", response)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PDF and document uploads — treat as health data."""
    if not _is_authorized(update):
        return

    doc = update.message.document
    if not doc:
        return

    mime = doc.mime_type or ""
    caption = update.message.caption or ""

    # Only process PDFs for now — other file types get a polite redirect
    if mime != "application/pdf" and not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text(
            "I can read PDFs (blood tests, lab results, etc.). "
            "Send me a PDF and I'll analyse it. Other file types aren't supported yet."
        )
        return

    await update.message.reply_text("Reading your PDF...")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        extracted = await _extract_pdf_text(bytes(file_bytes))
        print(f"[Telegram] PDF received: {doc.file_name} ({len(extracted)} chars extracted)")
    except Exception as e:
        await update.message.reply_text(f"Couldn't download the PDF: {e}")
        return

    await _handle_health_file(update, context, extracted, _infer_file_type(caption, "pdf"), caption)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads — use Claude vision to extract health data."""
    if not _is_authorized(update):
        return

    photos = update.message.photo
    if not photos:
        return

    caption = update.message.caption or ""

    await update.message.reply_text("Reading your photo...")
    try:
        # Use the largest available size
        largest = max(photos, key=lambda p: p.file_size or 0)
        file = await context.bot.get_file(largest.file_id)
        file_bytes = await file.download_as_bytearray()
        print(f"[Telegram] Photo received ({len(file_bytes)} bytes)")
        extracted = await _extract_photo_text(bytes(file_bytes))
        print(f"[Telegram] Vision extracted: {extracted[:100]}")
    except Exception as e:
        await update.message.reply_text(f"Couldn't read the photo: {e}")
        return

    await _handle_health_file(update, context, extracted, _infer_file_type(caption, "photo"), caption)


# ---------------------------------------------------------------------------
# Voice message transcription (Whisper API)
# ---------------------------------------------------------------------------

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Transcribe a voice message via OpenAI Whisper, then route as normal text.
    Requires OPENAI_API_KEY env var. Gracefully degrades if not set.
    """
    if not _is_authorized(update):
        return

    voice = update.message.voice
    if not voice:
        return

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        await update.message.reply_text(
            "Voice messages require Whisper transcription — OPENAI_API_KEY not set. "
            "Please send a text message instead."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        import io
        import openai

        # Download voice file (OGG/Opus from Telegram)
        file = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()
        print(f"[Voice] Received {len(file_bytes)} bytes (duration: {voice.duration}s)")

        # Transcribe with Whisper
        oai_client = openai.OpenAI(api_key=openai_key)
        audio_buffer = io.BytesIO(bytes(file_bytes))
        audio_buffer.name = "voice.ogg"  # Whisper needs a filename hint

        transcript = oai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_buffer,
            language="es",  # Nacho speaks Spanish; Whisper handles English too
        )
        transcribed_text = transcript.text.strip()
        print(f"[Voice] Transcribed: {transcribed_text[:100]}")

    except ImportError:
        await update.message.reply_text(
            "openai package not installed — can't transcribe voice. "
            "Run: pip install openai"
        )
        return
    except Exception as e:
        await update.message.reply_text(f"Couldn't transcribe your voice message: {e}")
        return

    if not transcribed_text:
        await update.message.reply_text("Couldn't transcribe that — please try again or send text.")
        return

    # Echo the transcript so athlete can confirm what was heard
    await update.message.reply_text(f'_(Heard: "{transcribed_text}")_', parse_mode="Markdown")

    # Route through normal message handling (log, process, respond)
    _log_message("IN", f"[Voice] {transcribed_text}")

    import asyncio as _asyncio
    _asyncio.create_task(_process_incoming_message_background())

    # Reuse the same confirmation + intent routing as handle_message
    lower = transcribed_text.lower()
    if any(phrase in lower for phrase in _RESUME_PHRASES):
        cleared = _end_skip_until()
        reply = "Done — emails resume tonight." if cleared else "No active email pause found."
        await update.message.reply_text(reply)
        _log_message("OUT", reply)
        return

    # Build a synthetic Update-like check for proposals
    class _FakeUpdate:
        message = update.message
        effective_chat = update.effective_chat

    if await _handle_confirmation(_FakeUpdate(), transcribed_text):
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    intent = _classify_intent(transcribed_text)
    print(f"[Router/Voice] Intent: {intent}")

    try:
        response = await _generate_response_with_tools(
            transcribed_text, update.effective_chat.id, context.bot, intent=intent
        )
    except Exception as e:
        print(f"[Voice] Tool-use failed, falling back: {e}")
        ctx = _build_bot_context()
        response = _generate_response(transcribed_text, ctx, SONNET_MODEL)

    await update.message.reply_text(response)
    _log_message("OUT", response)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set. Cannot start bot.", file=sys.stderr)
        sys.exit(1)

    print(f"Starting Telegram bot for {ATHLETE_NAME}...")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("endsession", handle_endsession_cmd))
    app.add_handler(CommandHandler("summary", handle_summary))
    app.add_handler(CommandHandler("week", handle_week))
    app.add_handler(CommandHandler("chart", handle_chart))
    app.add_handler(CommandHandler("compare", handle_compare))
    app.add_handler(CommandHandler("data", handle_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Bot running. Waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
