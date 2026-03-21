"""
cascade_levels.py — V17 Cascade Level Operations

Implements the bottom-up closing operations:
  close_day()    — runs at 22:00 UTC; classifies day; produces DAILY_SUMMARY JSON
  weekly_eval()  — runs Sunday; produces WEEKLY_SUMMARY from daily summaries
  monthly_eval() — runs end of month; produces MONTHLY_SUMMARY
  annual_eval()  — runs end of year; produces ANNUAL_SUMMARY
  longterm_eval()— runs quarterly; updates LONGTERM_PLAN

Each function:
  1. Reads parent summaries + own Tier 0 data
  2. Calls LLM (Haiku for daily/weekly, Sonnet for monthly+)
  3. Validates typed JSON output
  4. Appends to summary list via memory.append_summary()
  5. Checks escalation thresholds (Python, no LLM)
  6. Sets cascade state to COMMITTING then IDLE

close_day() also checks for escalation triggers (Python-side, deterministic):
  - Injury keywords in Telegram → escalate to ANNUAL
  - Goal change keywords → escalate to LONGTERM
  - 3+ sessions skipped this week → escalate to MONTHLY
  - Single session skipped → weekly note only
"""

import json
from datetime import date, datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INJURY_KEYWORDS = [
    "injury", "injured", "pain", "hurt", "pulled", "torn", "sprain",
    "strain", "shoulder", "elbow", "knee", "back pain", "wrist", "hip",
    "can't train", "doctor", "physio", "rest weeks", "tendon", "inflammation",
]
GOAL_CHANGE_KEYWORDS = [
    "change my goal", "new goal", "different goal", "pivot", "rethink",
    "not olympic", "switch to", "give up on", "no longer want",
]
SKIP_COUNT_FOR_MONTHLY_ESCALATION = 3  # skips in one week triggers MONTHLY escalation


# ---------------------------------------------------------------------------
# close_day()
# ---------------------------------------------------------------------------

def close_day(dry_run: bool = False) -> Optional[dict]:
    """
    Daily closing pass — runs at 22:00 UTC.

    Reads today's Telegram messages, sheet session status, and health data.
    Uses Haiku to classify what happened and produce a typed DAILY_SUMMARY.
    Checks escalation thresholds (deterministic Python) and initiates escalation if needed.
    Appends summary to DAILY_SUMMARIES Coach State domain (list, last 8 kept).

    Returns the DAILY_SUMMARY dict, or None on failure.
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_HAIKU
    from memory import (
        read_coach_state, upsert_coach_state,
        read_telegram_log, read_health_log, read_lift_history,
        read_summary_list, append_summary,
    )
    from cascade_state import (
        set_level_state, initiate_escalation,
        get_active_threads, push_thread,
    )

    today = date.today()
    today_str = str(today)
    print(f"[{today_str}] close_day() starting...")

    set_level_state("DAILY", "GATHERING")

    # --- 1. Gather today's data ---

    # Today's Telegram messages
    try:
        all_tg = read_telegram_log(limit=50)
        today_tg = [
            m for m in all_tg
            if (m.get("Date") or m.get("date") or "") == today_str
        ]
    except Exception:
        today_tg = []

    # Today's health entry
    health_today: dict = {}
    try:
        health_log = read_health_log(limit=7)
        for entry in reversed(health_log):
            if (entry.get("Date") or "") == today_str:
                health_today = entry
                break
    except Exception:
        pass

    # Was today's session done? Check lift history for today's date
    session_done_today = False
    session_label = ""
    try:
        lift_history = read_lift_history(limit=30)
        today_lifts = [e for e in lift_history if (e.get("Date") or e.get("date") or "") == today_str]
        session_done_today = len(today_lifts) > 0
        if session_done_today and today_lifts:
            session_label = today_lifts[0].get("Day") or today_lifts[0].get("day") or ""
    except Exception:
        pass

    # Recent daily summaries (to assess week's session count)
    recent_daily = []
    try:
        recent_daily = read_summary_list("DAILY_SUMMARIES", limit=8)
    except Exception:
        pass

    # Recent coach state
    coach_state: dict = {}
    try:
        coach_state = read_coach_state()
    except Exception:
        pass

    week_intent = coach_state.get("WEEKLY_INTENT", {}).get("summary", "")
    coaching_reason = coach_state.get("COACHING_REASON", {}).get("summary", "")

    # Build Telegram text for today
    tg_text = ""
    if today_tg:
        tg_text = "\n".join(
            f"[{m.get('Direction', '?').upper()}] {m.get('Message', '')[:150]}"
            for m in today_tg[-20:]
        )
    else:
        tg_text = "(no Telegram messages today)"

    # Health summary text
    health_text = ""
    if health_today:
        health_text = (
            f"Sleep: {health_today.get('Sleep (hrs)', '?')}h | "
            f"BW: {health_today.get('Bodyweight (kg)', '?')}kg | "
            f"Steps: {health_today.get('Steps', '?')} | "
            f"Food quality: {health_today.get('Food Quality (1-10)', '?')}/10"
        )
    else:
        health_text = "(no health data logged today)"

    # Session context
    session_context = (
        f"Session logged in lift history: {'YES' if session_done_today else 'NO'}\n"
        f"Session label: {session_label or 'unknown'}"
    )
    if not session_done_today:
        # Check if today was supposed to be a training day
        weekly_schedule = coach_state.get("WEEKLY_SCHEDULE", {}).get("summary", "")
        today_abbrev = today.strftime("%a").lower()
        today_full = today.strftime("%A").lower()
        was_training_day = (
            any(kw in weekly_schedule.lower() for kw in (today_abbrev, today_full))
            if weekly_schedule else None
        )
        session_context += f"\nWas today a scheduled training day: {'likely YES' if was_training_day else 'unknown'}"

    set_level_state("DAILY", "REASONING")

    prompt = f"""You are a strength coaching AI assistant. Classify today's training day and produce a daily summary.

TODAY: {today_str}
WEEKLY INTENT: {week_intent or "(not set)"}
COACHING REASON: {coaching_reason or "(not set)"}

=== TODAY'S TELEGRAM MESSAGES ===
{tg_text}

=== SESSION STATUS ===
{session_context}

=== HEALTH DATA ===
{health_text}

=== TASK ===
Produce a JSON daily summary for today. Use this exact schema:

{{
  "date": "{today_str}",
  "week": <integer week number or null>,
  "session": {{
    "completed": <true|false>,
    "label": "<session label or empty string>",
    "rpe_avg": <float or null — infer from Telegram if athlete mentioned RPE>,
    "effort_quality": "<excellent|strong|moderate|poor|rest_day|unknown>",
    "notable": "<1-2 sentences on what happened, what to watch>"
  }},
  "health": {{
    "sleep_hrs": <float or null>,
    "bw_kg": <float or null>,
    "steps": <integer or null>,
    "readiness_score": <integer 0-100 or null — estimate from available data>
  }},
  "events": [
    // Only include events that actually happened today
    // Each: {{"type": "concern|life|achievement|note", "category": "HEALTH|SCHEDULE|TRAINING|INJURY|GOAL", "text": "..."}}
  ],
  "escalation_check": "<none|injury|goal_change|sessions_skipped>",
  "markov_note": "<1 sentence: what should next week's coach know about today?>"
}}

Rules:
- If no session happened and it was a training day → completed=false, effort_quality="poor"
- If it was a rest day → completed=false, effort_quality="rest_day"
- If you cannot determine → effort_quality="unknown"
- escalation_check: "injury" if injury mentioned, "goal_change" if goal changed, "sessions_skipped" if training missed
- markov_note must be specific, not generic. "Good session" is not acceptable.
- Return ONLY the JSON object, no markdown, no explanation."""

    if dry_run:
        print(f"  [DRY RUN] close_day() would classify: session_done={session_done_today}, health={bool(health_today)}")
        set_level_state("DAILY", "IDLE")
        return None

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = result.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        summary = json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"  close_day(): JSON parse error — {e}. Raw: {raw[:200]}")
        # Fallback minimal summary
        summary = {
            "date": today_str,
            "week": None,
            "session": {"completed": session_done_today, "label": session_label,
                        "rpe_avg": None, "effort_quality": "unknown", "notable": "Parse error — manual review needed"},
            "health": {},
            "events": [],
            "escalation_check": "none",
            "markov_note": f"Parse error on {today_str}. Session done: {session_done_today}.",
        }
    except Exception as e:
        print(f"  close_day(): LLM error — {e}")
        set_level_state("DAILY", "IDLE")
        return None

    # --- 2. Python-side escalation check (deterministic, overrides LLM if needed) ---
    escalation = _check_escalation(summary, today_tg, recent_daily)
    if escalation:
        summary["escalation_check"] = escalation["type"]
        try:
            snapshot_id = initiate_escalation(escalation["disruption"], escalation["context"])
            summary["escalation_snapshot_id"] = snapshot_id
            print(f"  close_day(): Escalation triggered — {escalation['disruption']} (snapshot {snapshot_id})")
        except Exception as e:
            print(f"  close_day(): Escalation error — {e}")

    # --- 3. Commit summary ---
    set_level_state("DAILY", "COMMITTING")
    try:
        append_summary("DAILY_SUMMARIES", summary, max_keep=10)
        print(f"  close_day(): Summary committed — {summary.get('session', {}).get('effort_quality', '?')} | escalation={summary.get('escalation_check', 'none')}")
    except Exception as e:
        print(f"  close_day(): Commit error — {e}")
        set_level_state("DAILY", "IDLE")
        return summary

    set_level_state("DAILY", "IDLE")
    return summary


# ---------------------------------------------------------------------------
# weekly_eval()
# ---------------------------------------------------------------------------

def weekly_eval(dry_run: bool = False) -> Optional[dict]:
    """
    Weekly evaluation pass — runs Sunday evening (after close_day).

    Reads the last 7 DAILY_SUMMARIES and produces a typed WEEKLY_SUMMARY.
    Uses Haiku (pattern recognition on structured data).
    Appends to WEEKLY_SUMMARIES (last 8 kept).
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_HAIKU
    from memory import read_summary_list, append_summary, read_coach_state
    from cascade_state import set_level_state

    today = date.today()
    print(f"[{today}] weekly_eval() starting...")
    set_level_state("WEEKLY", "GATHERING")

    daily_summaries = read_summary_list("DAILY_SUMMARIES", limit=7)
    if not daily_summaries:
        print("  weekly_eval(): No daily summaries found — skipping.")
        set_level_state("WEEKLY", "IDLE")
        return None

    coach_state = read_coach_state()
    week_num = None
    for ds in reversed(daily_summaries):
        if ds.get("week"):
            week_num = ds["week"]
            break

    daily_json = json.dumps(daily_summaries, indent=2, ensure_ascii=False)

    set_level_state("WEEKLY", "REASONING")

    prompt = f"""You are a strength coaching AI. Produce a weekly evaluation summary from the daily summaries below.

DAILY SUMMARIES (last 7 days):
{daily_json}

Produce a JSON weekly summary with this exact schema:
{{
  "week": {week_num or "null"},
  "closed": "{today}",
  "training": {{
    "sessions_done": <integer>,
    "avg_effort_quality": "<excellent|strong|moderate|poor|mixed>",
    "volume_achieved": "<full|partial|minimal|none>",
    "primary_lift_progress": {{"<lift_name>": "<+Xkg|0|-Xkg|unknown>"}},
    "notable": "<2-3 sentences on the week — what stood out, what to carry forward>"
  }},
  "health": {{
    "avg_sleep": <float or null>,
    "avg_readiness": <integer or null>,
    "bw_trend": "<stable|up|down|unknown>"
  }},
  "escalations": [<list of escalation types that fired this week, or empty>],
  "patterns": {{
    "recurring_concern": "<describe any pattern or null>",
    "behavioral_notes": "<coach observation about athlete behavior or null>"
  }},
  "markov_note_for_next_week": "<specific actionable note for next week's planner>",
  "to_monthly": "<1-2 sentences: what monthly-level planner needs to know>"
}}

Rules:
- sessions_done: count days where session.completed == true
- avg_effort_quality: majority vote across days
- Recurring concern: only if same issue appeared 3+ times in the week
- markov_note_for_next_week must be specific and actionable — no generic summaries
- Return ONLY the JSON object."""

    if dry_run:
        print(f"  [DRY RUN] weekly_eval() would evaluate {len(daily_summaries)} daily summaries")
        set_level_state("WEEKLY", "IDLE")
        return None

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = result.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        summary = json.loads(raw)
    except Exception as e:
        print(f"  weekly_eval(): Error — {e}")
        set_level_state("WEEKLY", "IDLE")
        return None

    set_level_state("WEEKLY", "COMMITTING")
    append_summary("WEEKLY_SUMMARIES", summary, max_keep=8)
    print(f"  weekly_eval(): Week {week_num} summary committed — {summary.get('training', {}).get('sessions_done', '?')} sessions")

    set_level_state("WEEKLY", "IDLE")
    return summary


# ---------------------------------------------------------------------------
# monthly_eval()
# ---------------------------------------------------------------------------

def monthly_eval(dry_run: bool = False) -> Optional[dict]:
    """
    Monthly evaluation — runs end of each month.
    Reads last 4-5 WEEKLY_SUMMARIES, produces MONTHLY_SUMMARY.
    Uses Sonnet (strategic reasoning).
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
    from memory import read_summary_list, append_summary, read_single_summary
    from cascade_state import set_level_state

    today = date.today()
    print(f"[{today}] monthly_eval() starting...")
    set_level_state("MONTHLY", "GATHERING")

    weekly_summaries = read_summary_list("WEEKLY_SUMMARIES", limit=5)
    if not weekly_summaries:
        print("  monthly_eval(): No weekly summaries — skipping.")
        set_level_state("MONTHLY", "IDLE")
        return None

    annual = read_single_summary("ANNUAL_SUMMARY") or {}
    weekly_json = json.dumps(weekly_summaries, indent=2, ensure_ascii=False)

    set_level_state("MONTHLY", "REASONING")

    prompt = f"""You are a strength coaching AI. Produce a monthly evaluation from the weekly summaries.

ANNUAL SUMMARY (for context):
{json.dumps(annual, indent=2, ensure_ascii=False) or "(not yet set)"}

WEEKLY SUMMARIES (last 4-5 weeks):
{weekly_json}

Produce a JSON monthly summary:
{{
  "month": "<YYYY-MM>",
  "closed": "{today}",
  "training": {{
    "weeks_completed": <integer>,
    "overall_quality": "<excellent|strong|moderate|poor>",
    "volume_trend": "<ascending|stable|declining>",
    "strength_progress": {{"<lift>": "<trend description>"}},
    "notable": "<3-4 sentences on the month>"
  }},
  "health": {{
    "sleep_trend": "<improving|stable|declining>",
    "bw_trend": "<up Xkg|stable|down Xkg>",
    "readiness_trend": "<improving|stable|declining>"
  }},
  "escalations_handled": [<list>],
  "patterns": {{
    "key_insight": "<most important pattern observed this month>",
    "risk_flags": "<anything that needs attention next month>"
  }},
  "markov_note_for_next_month": "<specific actionable guidance for next month's planning>",
  "to_annual": "<1-2 sentences: what annual-level planner needs to know>"
}}

Return ONLY the JSON object."""

    if dry_run:
        print(f"  [DRY RUN] monthly_eval() would evaluate {len(weekly_summaries)} weekly summaries")
        set_level_state("MONTHLY", "IDLE")
        return None

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = result.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        summary = json.loads(raw.strip())
    except Exception as e:
        print(f"  monthly_eval(): Error — {e}")
        set_level_state("MONTHLY", "IDLE")
        return None

    set_level_state("MONTHLY", "COMMITTING")
    append_summary("MONTHLY_SUMMARIES", summary, max_keep=6)
    print(f"  monthly_eval(): Month summary committed — {summary.get('training', {}).get('overall_quality', '?')}")

    set_level_state("MONTHLY", "IDLE")
    return summary


# ---------------------------------------------------------------------------
# annual_eval() and longterm_eval() — Phase 7, stubs for now
# ---------------------------------------------------------------------------

def annual_eval(dry_run: bool = False) -> Optional[dict]:
    """Annual evaluation — Phase 7. Stub."""
    print(f"[{date.today()}] annual_eval() — Phase 7 (not yet implemented).")
    return None


def longterm_eval(dry_run: bool = False) -> Optional[dict]:
    """Long-term evaluation (quarterly) — Phase 7. Stub."""
    print(f"[{date.today()}] longterm_eval() — Phase 7 (not yet implemented).")
    return None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _check_escalation(
    daily_summary: dict,
    today_tg: list,
    recent_daily: list,
) -> Optional[dict]:
    """
    Deterministic Python escalation check.
    Returns {"type": str, "disruption": str, "context": dict} or None.
    Priority: injury > goal_change > sessions_skipped.
    """
    # Check today's Telegram for injury keywords
    tg_text = " ".join(m.get("Message", "").lower() for m in today_tg)
    for kw in INJURY_KEYWORDS:
        if kw in tg_text:
            return {
                "type": "injury",
                "disruption": "injury",
                "context": {"keyword": kw, "date": str(date.today())},
            }

    # Check for goal change keywords
    for kw in GOAL_CHANGE_KEYWORDS:
        if kw in tg_text:
            return {
                "type": "goal_change",
                "disruption": "goal_change",
                "context": {"keyword": kw, "date": str(date.today())},
            }

    # Check session skip count this week (look at this week's daily summaries)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_summaries = [
        ds for ds in recent_daily
        if ds.get("date") and ds["date"] >= str(week_start)
    ]
    # Also include today's summary
    week_summaries.append(daily_summary)

    sessions_missed = sum(
        1 for ds in week_summaries
        if ds.get("session", {}).get("completed") is False
        and ds.get("session", {}).get("effort_quality") != "rest_day"
    )
    if sessions_missed >= SKIP_COUNT_FOR_MONTHLY_ESCALATION:
        return {
            "type": "sessions_skipped",
            "disruption": "multiple_sessions_skipped",
            "context": {"missed_count": sessions_missed, "week_start": str(week_start)},
        }

    return None
