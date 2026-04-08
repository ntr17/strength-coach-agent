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

# Severe/explicit → immediate cascade escalation (structural damage, can't train, medical)
INJURY_ESCALATION_KEYWORDS = [
    "can't train", "cannot train", "torn", "pulled muscle", "sprain",
    "strain", "fracture", "doctor", "physio", "physiotherapist",
    "rest weeks", "surgery", "mri", "x-ray", "hospital", "emergency",
    "serious injury", "badly hurt", "can't lift", "can not lift",
    "no puedo entrenar", "lesión grave", "rotura", "descanso forzado",
]
# Casual mention → coach notes and asks, but does NOT fire cascade escalation
INJURY_WATCH_KEYWORDS = [
    "pain", "hurt", "sore", "tight", "ache",
    "shoulder", "elbow", "knee", "wrist", "hip",
    "tendon", "inflammation", "tweak", "tweaked",
    "molestia", "dolor", "codo", "rodilla", "hombro",
]
# Legacy alias — close_day() uses the full combined set for its soft check
INJURY_KEYWORDS = INJURY_ESCALATION_KEYWORDS + INJURY_WATCH_KEYWORDS
GOAL_CHANGE_KEYWORDS = [
    "change my goal", "new goal", "different goal", "pivot", "rethink",
    "not olympic", "switch to", "give up on", "no longer want",
]
SKIP_COUNT_FOR_MONTHLY_ESCALATION = 3  # skips in one week triggers MONTHLY escalation


# ---------------------------------------------------------------------------
# Constitutional helpers (shared across all eval levels)
# ---------------------------------------------------------------------------

def _check_golden_rules() -> dict:
    """
    Reads GOLDEN_RULES from memory and checks for constitutional violations.

    Returns:
      - "has_conflict": bool — True if any rule_id overridden 3+ times
      - "conflicted_rules": list of rule_ids overridden >= 3 times
      - "override_log": the raw override log list
      - "total_overrides": int — total entries in override_log
    Never raises — returns safe defaults on any error.
    """
    _safe = {"has_conflict": False, "conflicted_rules": [], "override_log": [], "total_overrides": 0}
    try:
        from memory import read_coach_state as _rcs_gr
        cs = _rcs_gr()
        raw = (cs.get("GOLDEN_RULES", {}).get("summary", "") or
               cs.get("GOLDEN_RULES", {}).get("Summary", ""))
        if not raw:
            return _safe
        rules_data = json.loads(raw)
        override_log = rules_data.get("override_log", [])
        override_counts: dict = {}
        for entry in override_log:
            rule_id = entry.get("rule_id", "unknown")
            override_counts[rule_id] = override_counts.get(rule_id, 0) + 1
        conflicted = [rid for rid, cnt in override_counts.items() if cnt >= 3]
        return {
            "has_conflict": bool(conflicted),
            "conflicted_rules": conflicted,
            "override_log": override_log,
            "total_overrides": len(override_log),
        }
    except Exception:
        return _safe


def check_if_session_planned_today(weekly_state: dict) -> bool:
    """
    Returns True if a training session was scheduled for today according to
    WEEKLY_INTENT's sessions_planned field. Returns False for rest days,
    travel days, or when sessions_planned is absent.
    """
    today_name = datetime.utcnow().strftime("%A").lower()
    sessions = weekly_state.get("sessions_planned", [])
    return any(
        s.get("day", "").lower() == today_name and
        s.get("type") not in ("rest", "travel", None)
        for s in sessions
    )


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
        read_single_summary, write_single_summary,
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
            metadata={"mode": "close_day"},
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

    # --- 3b. Harvest flags from DAILY_FOCUS into PENDING_FLAGS ---
    # DAILY_FOCUS is written by daily_planning conversations. Any flags stored there
    # (flag_for_weekly_review, program_change_request, flag_for_next_session) would
    # otherwise die silently. close_day() reads them and appends to PENDING_FLAGS so
    # weekly_eval() and the Sunday weekly planning pipeline can surface them.
    try:
        import json as _json_flags
        _df_raw = coach_state.get("DAILY_FOCUS", {}).get("summary", "") or \
                  coach_state.get("DAILY_FOCUS", {}).get("Summary", "")
        if _df_raw and _df_raw.strip().startswith("{"):
            _df = _json_flags.loads(_df_raw)
            _flags_to_add = []
            if _df.get("flag_for_weekly_review"):
                _flags_to_add.append({
                    "date": today_str, "type": "weekly_review",
                    "content": _df["flag_for_weekly_review"],
                })
            if _df.get("flag_for_next_session"):
                _flags_to_add.append({
                    "date": today_str, "type": "next_session",
                    "content": _df["flag_for_next_session"],
                })
            if _df.get("program_change_request"):
                _flags_to_add.append({
                    "date": today_str, "type": "program_change",
                    "content": _df["program_change_request"],
                })
            if _flags_to_add:
                _pf_raw = coach_state.get("PENDING_FLAGS", {}).get("summary", "") or \
                          coach_state.get("PENDING_FLAGS", {}).get("Summary", "")
                _existing = _json_flags.loads(_pf_raw) if _pf_raw and _pf_raw.strip().startswith("[") else []
                _existing.extend(_flags_to_add)
                upsert_coach_state("PENDING_FLAGS", _json_flags.dumps(_existing[-20:]), "MEDIUM")
                print(f"  close_day(): {len(_flags_to_add)} flag(s) harvested from DAILY_FOCUS → PENDING_FLAGS.")
    except Exception as _flag_err:
        print(f"  close_day(): Flag harvest failed (non-fatal): {_flag_err}")

    # --- 3d. Update skip_patterns counter in WEEKLY_INTENT ---
    # Deterministic redundancy layer for behavioral pattern detection (supplements LLM).
    # Increments consecutive-skip counter for today's weekday if a session was planned
    # but not completed. Resets to 0 when session is completed. weekly_eval() reads this
    # and injects a code-verified signal into its LLM prompt when streak >= 3.
    try:
        _today_weekday_sp = datetime.utcnow().strftime("%A").lower()
        _wi_sp = read_single_summary("WEEKLY_INTENT") or {}
        if _wi_sp:
            _session_planned_sp = check_if_session_planned_today(_wi_sp)
            _session_completed_sp = summary.get("session", {}).get("completed", False)
            if _session_planned_sp:
                # Only touch skip_patterns when the day was in the schedule
                _skip_patterns_sp = dict(_wi_sp.get("skip_patterns", {}))
                if not _session_completed_sp:
                    _skip_patterns_sp[_today_weekday_sp] = _skip_patterns_sp.get(_today_weekday_sp, 0) + 1
                    print(f"  close_day(): {_today_weekday_sp} skip streak → "
                          f"{_skip_patterns_sp[_today_weekday_sp]}")
                else:
                    _skip_patterns_sp[_today_weekday_sp] = 0
                _wi_sp["skip_patterns"] = _skip_patterns_sp
                write_single_summary("WEEKLY_INTENT", _wi_sp)
    except Exception as _sp_err:
        print(f"  close_day(): skip_patterns update failed (non-fatal): {_sp_err}")

    # --- 4. Send end-of-day Telegram message ---
    if not dry_run:
        try:
            from telegram_utils import send_telegram_message
            _sess = summary.get("session", {})
            _week = summary.get("week") or "?"
            _effort = _sess.get("effort_quality", "unknown")
            _label = _sess.get("label", "")
            _notable = _sess.get("notable", "")
            _esc = summary.get("escalation_check", "none")

            if _sess.get("completed"):
                _status_line = f"Done ({_effort})"
            elif _effort == "rest_day":
                _status_line = "Rest day"
            else:
                _status_line = f"Missed ({_effort})"

            if _label:
                _status_line = f"{_label} — {_status_line}"

            lines = [f"Day closed — Week {_week}.", _status_line]
            if _notable:
                lines.append(_notable)
            if _esc and _esc != "none":
                lines.append(f"Flagged: {_esc}")

            send_telegram_message("\n".join(lines))
            print(f"  close_day(): End-of-day message sent.")
        except Exception as _tg_err:
            print(f"  close_day(): Telegram message failed (non-fatal): {_tg_err}")

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
    from memory import read_summary_list, append_summary, read_coach_state, read_single_summary, write_single_summary
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

    # --- Golden Rules alert (prompt decoration only — no gate) ---
    _gr_weekly = _check_golden_rules()
    _gr_warning_weekly = ""
    if _gr_weekly["has_conflict"]:
        _gr_warning_weekly = (
            f"\nCONSTITUTIONAL ALERT: The following golden rules have been overridden "
            f"3+ times: {', '.join(_gr_weekly['conflicted_rules'])}. "
            f"Flag any patterns that conflict with these rules in your weekly summary.\n"
        )

    # --- Skip patterns signal (code-verified behavioral counter) ---
    _skip_signal_weekly = ""
    try:
        _wi_sp_w = read_single_summary("WEEKLY_INTENT") or {}
        _sp_w = _wi_sp_w.get("skip_patterns", {})
        _flagged_w = [(day, cnt) for day, cnt in _sp_w.items() if cnt >= 3]
        if _flagged_w:
            _flag_lines_w = [f"  {day}: {cnt} consecutive skips" for day, cnt in _flagged_w]
            _skip_signal_weekly = (
                "\nBEHAVIORAL PATTERN DETECTED (code-verified):\n"
                + "\n".join(_flag_lines_w)
                + "\nRecommend flagging in weekly summary and markov_note_for_next_week.\n"
            )
    except Exception:
        pass

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
- Return ONLY the JSON object.""" + _gr_warning_weekly + _skip_signal_weekly

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
            metadata={"mode": "weekly_eval"},
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

    # Reset skip_patterns for the new week (consecutive counter resets at week boundary)
    try:
        _wi_reset = read_single_summary("WEEKLY_INTENT") or {}
        if _wi_reset:
            _wi_reset["skip_patterns"] = {}
            write_single_summary("WEEKLY_INTENT", _wi_reset)
    except Exception:
        pass

    set_level_state("WEEKLY", "IDLE")
    return summary


# ---------------------------------------------------------------------------
# monthly_eval()
# ---------------------------------------------------------------------------

def monthly_eval(dry_run: bool = False, escalation_context: Optional[dict] = None) -> Optional[dict]:
    """
    Monthly evaluation — runs end of each month.
    Reads last 4-5 WEEKLY_SUMMARIES, produces MONTHLY_SUMMARY.
    Uses Sonnet (strategic reasoning).

    escalation_context: if provided, eval was triggered by disruption (injury, skips, etc.).
    In this case: prompt focuses on specific concern, result sent to athlete, state → AWAITING_USER.
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
    from memory import read_summary_list, append_summary, read_single_summary, upsert_coach_state
    from cascade_state import set_level_state

    today = date.today()
    is_escalation = escalation_context is not None
    print(f"[{today}] monthly_eval() starting... (escalation={is_escalation})")
    set_level_state("MONTHLY", "GATHERING")

    weekly_summaries = read_summary_list("WEEKLY_SUMMARIES", limit=5)
    if not weekly_summaries:
        print("  monthly_eval(): No weekly summaries — skipping.")
        set_level_state("MONTHLY", "IDLE")
        return None

    annual = read_single_summary("ANNUAL_SUMMARY") or {}
    weekly_json = json.dumps(weekly_summaries, indent=2, ensure_ascii=False)

    # --- Golden Rules alert (prompt decoration only) ---
    _gr_monthly = _check_golden_rules()
    _gr_warning_monthly = ""
    if _gr_monthly["has_conflict"]:
        _gr_warning_monthly = (
            f"\nCONSTITUTIONAL ALERT: The following golden rules have been overridden "
            f"3+ times: {', '.join(_gr_monthly['conflicted_rules'])}. "
            f"Flag this in your monthly patterns summary.\n"
        )

    set_level_state("MONTHLY", "REASONING")

    escalation_block = ""
    if escalation_context:
        esc_type = escalation_context.get("type", "unknown")
        esc_detail = escalation_context.get("context", {})
        session_label = escalation_context.get("session_label", "recent session")
        escalation_block = f"""
ESCALATION TRIGGER (reason this eval was triggered now, not on schedule):
  Type: {esc_type}
  Detail: {json.dumps(esc_detail, ensure_ascii=False)}
  Triggered during: {session_label}

Focus your analysis on this concern. Produce a specific proposal the athlete can confirm.
E.g.: "I recommend reducing pull volume by 20% for weeks 10-12. This does not impact bench/squat timeline."
"""

    prompt = f"""You are a strength coaching AI. Produce a monthly evaluation from the weekly summaries.

ANNUAL SUMMARY (for context):
{json.dumps(annual, indent=2, ensure_ascii=False) or "(not yet set)"}

WEEKLY SUMMARIES (last 4-5 weeks):
{weekly_json}
{escalation_block}
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
  "to_annual": "<1-2 sentences: what annual-level planner needs to know>",
  "escalation_recommendation": "<if escalation triggered: specific proposal for athlete to confirm, or null>",
  "escalation_adjustments": ["<list of specific program changes being proposed, or empty>"]
}}

Return ONLY the JSON object.""" + _gr_warning_monthly

    if dry_run:
        print(f"  [DRY RUN] monthly_eval() would evaluate {len(weekly_summaries)} weekly summaries")
        set_level_state("MONTHLY", "IDLE")
        return None

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
            metadata={"mode": "monthly_eval"},
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
    append_summary("MONTHLY_SUMMARIES", summary)
    print(f"  monthly_eval(): Month summary committed — {summary.get('training', {}).get('overall_quality', '?')}")

    # Open monthly planning conversation (non-escalation) or send escalation recommendation
    if is_escalation and summary.get("escalation_recommendation"):
        try:
            from telegram_utils import send_telegram_message
            rec = summary["escalation_recommendation"]
            adjustments = summary.get("escalation_adjustments", [])
            adj_text = "\n".join(f"  • {a}" for a in adjustments) if adjustments else ""
            msg = (
                f"I've looked at this from the monthly planning level.\n\n"
                f"{rec}"
                + (f"\n\nSpecific changes:\n{adj_text}" if adj_text else "")
                + "\n\nReply 'confirm' to apply these changes, or tell me what you'd adjust."
            )
            send_telegram_message(msg)
            set_level_state("MONTHLY", "AWAITING_USER")
            upsert_coach_state(
                "CURRENT_FLOW",
                f"cascade_awaiting_confirm | MONTHLY | {today} | {escalation_context.get('type', 'unknown')}",
                "HIGH",
            )
            print(f"  monthly_eval(): Escalation recommendation sent to athlete. State → AWAITING_USER.")
        except Exception as e:
            print(f"  monthly_eval(): Failed to send escalation message: {e}")
            set_level_state("MONTHLY", "IDLE")
    else:
        # Non-escalation: open collaborative monthly planning conversation
        try:
            import anthropic as _ant_mp
            import json as _json_mp
            from config import ANTHROPIC_API_KEY as _ak_mp, CLAUDE_MODEL as _model_mp, ATHLETE_NAME as _an_mp
            from telegram_utils import send_telegram_message as _stm_mp
            annual = read_single_summary("ANNUAL_SUMMARY") or {}
            _open_prompt_mp = (
                f"You are opening the monthly planning conversation with {_an_mp}.\n\n"
                f"ANNUAL ARC:\n{json.dumps(annual.get('next_12_months', annual), indent=2, ensure_ascii=False)[:600]}\n\n"
                f"LAST MONTH SUMMARY:\n{json.dumps(summary, indent=2, ensure_ascii=False)[:800]}\n\n"
                f"Write the opening message for next month's planning (120-140 words):\n"
                f"1. One honest sentence on how last month went (use the summary data).\n"
                f"2. Your concrete recommendation for next month: specific focus areas, volume direction,\n"
                f"   any adjustments needed. Reference the annual arc.\n"
                f"3. Ask for the athlete's input — schedule constraints, life context, priorities.\n"
                f"Direct, specific. No filler. No emojis."
            )
            _open_resp_mp = _ant_mp.Anthropic(api_key=_ak_mp).messages.create(
                model=_model_mp, max_tokens=250,
                messages=[{"role": "user", "content": _open_prompt_mp}],
                metadata={"mode": "monthly_planning_open"},
            )
            _opening_mp = _open_resp_mp.content[0].text.strip()
            _thread_mp = {"month": str(today)[:7], "thread": [{"role": "assistant", "content": _opening_mp}]}
            upsert_coach_state("MONTHLY_PLAN_THREAD", _json_mp.dumps(_thread_mp), "HIGH")
            upsert_coach_state("CURRENT_FLOW", f"monthly_planning | {today}", "MEDIUM")
            _stm_mp(_opening_mp)
            print(f"  monthly_eval(): Monthly planning conversation opened.")
        except Exception as _mp_err:
            print(f"  monthly_eval(): Planning conversation failed (non-fatal): {_mp_err}")
        set_level_state("MONTHLY", "IDLE")

    return summary


# ---------------------------------------------------------------------------
# annual_eval() and longterm_eval() — Phase 7, stubs for now
# ---------------------------------------------------------------------------

def annual_eval(dry_run: bool = False, escalation_context: Optional[dict] = None) -> Optional[dict]:
    """
    Annual evaluation — runs monthly (re-evaluates year plan each month).
    Reads last 6 MONTHLY_SUMMARIES + GOLDEN_RULES + athlete long-term goals.
    Uses Sonnet (strategic, rare). Writes ANNUAL_SUMMARY.

    escalation_context: if provided, this eval was triggered by an escalation event (injury,
    goal change, etc.) not a schedule. In this case:
      - Prompt focuses on the specific concern
      - Result is sent to athlete via Telegram as a message + awaits confirmation
      - Cascade state set to AWAITING_USER
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
    from memory import read_summary_list, read_single_summary, write_single_summary, read_coach_state, upsert_coach_state
    from cascade_state import set_level_state

    today = date.today()
    is_escalation = escalation_context is not None
    print(f"[{today}] annual_eval() starting... (escalation={is_escalation})")
    set_level_state("ANNUAL", "GATHERING")

    monthly_summaries = read_summary_list("MONTHLY_SUMMARIES", limit=6)
    if not monthly_summaries:
        print("  annual_eval(): No monthly summaries — skipping.")
        set_level_state("ANNUAL", "IDLE")
        return None

    golden_rules = None
    coach_state = read_coach_state()
    try:
        import json as _json
        raw_rules = coach_state.get("GOLDEN_RULES", {}).get("summary", "")
        if raw_rules:
            golden_rules = _json.loads(raw_rules)
    except Exception:
        pass

    athlete_goals = coach_state.get("ANNUAL_ARC", {}).get("summary", "")
    monthly_json = json.dumps(monthly_summaries, indent=2, ensure_ascii=False)
    rules_text = json.dumps(golden_rules, indent=2, ensure_ascii=False) if golden_rules else "(not yet set)"

    set_level_state("ANNUAL", "REASONING")

    escalation_block = ""
    if escalation_context:
        esc_type = escalation_context.get("type", "unknown")
        esc_detail = escalation_context.get("context", {})
        session_label = escalation_context.get("session_label", "recent session")
        escalation_block = f"""
ESCALATION TRIGGER (reason this eval was triggered now, not on schedule):
  Type: {esc_type}
  Detail: {json.dumps(esc_detail, ensure_ascii=False)}
  Triggered during: {session_label}

Focus your analysis on this specific concern. Answer:
1. Does this disruption threaten the annual goals?
2. What program adjustment, if any, is needed?
3. Frame your recommendation as a specific proposal the athlete can confirm or reject.
   E.g.: "I recommend reducing pull volume by 20% for weeks 10-12 to protect the elbow.
   This does not impact the bench or squat timeline. Confirm and I'll update the program."
"""

    prompt = f"""You are a strength coaching AI. Produce an annual evaluation from the monthly summaries.

GOLDEN RULES (constitutional constraints — must be respected):
{rules_text}

LONG-TERM GOALS:
{athlete_goals or "(not yet set)"}

MONTHLY SUMMARIES (last 6 months):
{monthly_json}
{escalation_block}
Produce a JSON annual summary:
{{
  "year": <integer>,
  "evaluated_at": "{today}",
  "training": {{
    "months_evaluated": <integer>,
    "trend": "<ascending|stable|declining|mixed>",
    "primary_achievements": ["<list>"],
    "primary_gaps": ["<list>"]
  }},
  "health": {{
    "sleep_trend": "<improving|stable|declining>",
    "recovery_baseline": "<good|moderate|compromised>",
    "health_risks": ["<list any ongoing risks>"]
  }},
  "goal_alignment": {{
    "on_track": <true|false>,
    "gap_to_medium_goals": "<description>",
    "program_adjustments_needed": "<description or null>"
  }},
  "golden_rules_compliance": {{
    "violations": ["<any detected violations>"],
    "notes": "<observation>"
  }},
  "next_12_months": {{
    "primary_focus": "<one clear sentence>",
    "monthly_objectives": ["<up to 3 key objectives>"],
    "risks_to_watch": ["<list>"]
  }},
  "markov_note": "<what the next quarterly review should know about this year so far>",
  "escalation_recommendation": "<if escalation triggered: specific proposal for athlete to confirm, or null>",
  "escalation_adjustments": ["<list of specific program changes being proposed, or empty>"]
}}

Return ONLY the JSON object."""

    if dry_run:
        print(f"  [DRY RUN] annual_eval() would evaluate {len(monthly_summaries)} monthly summaries")
        set_level_state("ANNUAL", "IDLE")
        return None

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1400,
            messages=[{"role": "user", "content": prompt}],
            metadata={"mode": "annual_eval"},
        )
        raw = result.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        summary = json.loads(raw.strip())
    except Exception as e:
        print(f"  annual_eval(): Error — {e}")
        set_level_state("ANNUAL", "IDLE")
        return None

    set_level_state("ANNUAL", "COMMITTING")
    write_single_summary("ANNUAL_SUMMARY", summary)
    print(f"  annual_eval(): Annual summary committed — on_track={summary.get('goal_alignment', {}).get('on_track', '?')}")

    # Open annual planning conversation (non-escalation) or send escalation recommendation
    if is_escalation and summary.get("escalation_recommendation"):
        try:
            from telegram_utils import send_telegram_message
            rec = summary["escalation_recommendation"]
            adjustments = summary.get("escalation_adjustments", [])
            adj_text = "\n".join(f"  • {a}" for a in adjustments) if adjustments else ""
            msg = (
                f"I've reviewed the situation at the annual planning level.\n\n"
                f"{rec}"
                + (f"\n\nSpecific changes:\n{adj_text}" if adj_text else "")
                + "\n\nReply 'confirm' to apply these changes, or tell me what you'd adjust."
            )
            send_telegram_message(msg)
            set_level_state("ANNUAL", "AWAITING_USER")
            upsert_coach_state(
                "CURRENT_FLOW",
                f"cascade_awaiting_confirm | ANNUAL | {today} | {escalation_context.get('type', 'unknown')}",
                "HIGH",
            )
            print(f"  annual_eval(): Escalation recommendation sent to athlete. State → AWAITING_USER.")
        except Exception as e:
            print(f"  annual_eval(): Failed to send escalation message: {e}")
            set_level_state("ANNUAL", "IDLE")
    else:
        # Non-escalation: open collaborative annual planning conversation
        try:
            import anthropic as _ant_ap
            import json as _json_ap
            from config import ANTHROPIC_API_KEY as _ak_ap, CLAUDE_MODEL as _model_ap, ATHLETE_NAME as _an_ap
            from memory import read_single_summary as _rss_ap
            from telegram_utils import send_telegram_message as _stm_ap
            longterm = _rss_ap("LONGTERM_PLAN") or {}
            _open_prompt_ap = (
                f"You are opening the annual arc review conversation with {_an_ap}.\n\n"
                f"LONGTERM PLAN:\n{json.dumps(longterm, indent=2, ensure_ascii=False)[:500]}\n\n"
                f"ANNUAL SUMMARY JUST WRITTEN:\n{json.dumps(summary, indent=2, ensure_ascii=False)[:800]}\n\n"
                f"Write the opening message for the annual arc review (120-150 words):\n"
                f"1. One honest sentence on the year so far — achievement and gaps.\n"
                f"2. Your recommendation for the next 12 months: primary focus, key milestones,\n"
                f"   any changes to existing goals based on what you see in the data.\n"
                f"3. Ask for the athlete's input — life changes, shifting priorities, new goals.\n"
                f"Direct. Specific. Reference actual numbers from the summaries. No emojis."
            )
            _open_resp_ap = _ant_ap.Anthropic(api_key=_ak_ap).messages.create(
                model=_model_ap, max_tokens=280,
                messages=[{"role": "user", "content": _open_prompt_ap}],
                metadata={"mode": "annual_planning_open"},
            )
            _opening_ap = _open_resp_ap.content[0].text.strip()
            _thread_ap = {"year": today.year, "thread": [{"role": "assistant", "content": _opening_ap}]}
            upsert_coach_state("ANNUAL_PLAN_THREAD", _json_ap.dumps(_thread_ap), "HIGH")
            upsert_coach_state("CURRENT_FLOW", f"annual_planning | {today}", "MEDIUM")
            _stm_ap(_opening_ap)
            print(f"  annual_eval(): Annual planning conversation opened.")
        except Exception as _ap_err:
            print(f"  annual_eval(): Planning conversation failed (non-fatal): {_ap_err}")
        set_level_state("ANNUAL", "IDLE")

    return summary


def longterm_eval(dry_run: bool = False) -> Optional[dict]:
    """
    Long-term evaluation — runs quarterly.
    Reads ANNUAL_SUMMARY + GOLDEN_RULES + Golden Rules override log.
    Uses Sonnet. Updates LONGTERM_PLAN.
    Checks if Golden Rules have been violated 3+ times → re-runs constitutional check.
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
    from memory import read_single_summary, write_single_summary, read_coach_state
    from cascade_state import set_level_state

    today = date.today()
    print(f"[{today}] longterm_eval() starting...")
    set_level_state("LONGTERM", "GATHERING")

    annual = read_single_summary("ANNUAL_SUMMARY")
    if not annual:
        print("  longterm_eval(): No annual summary — skipping.")
        set_level_state("LONGTERM", "IDLE")
        return None

    coach_state = read_coach_state()
    golden_rules_raw = coach_state.get("GOLDEN_RULES", {}).get("summary", "")
    existing_longterm = read_single_summary("LONGTERM_PLAN")
    athlete_goals = coach_state.get("ANNUAL_ARC", {}).get("summary", "")

    # Check for constitutional violations using shared helper
    _gr_longterm = _check_golden_rules()
    constitutional_issue = _gr_longterm["has_conflict"]
    if constitutional_issue:
        print(f"  longterm_eval(): Constitutional issue — rules overridden 3+ times: "
              f"{_gr_longterm['conflicted_rules']}")

    set_level_state("LONGTERM", "REASONING")

    prompt = f"""You are a strength coaching AI doing a quarterly long-term review.

GOLDEN RULES (the constitution):
{golden_rules_raw or "(not yet set)"}

LONG-TERM GOALS:
{athlete_goals or "(not yet set)"}

ANNUAL SUMMARY (most recent):
{json.dumps(annual, indent=2, ensure_ascii=False)}

EXISTING LONGTERM PLAN (for continuity):
{json.dumps(existing_longterm, indent=2, ensure_ascii=False) if existing_longterm else "(first evaluation)"}

{"CONSTITUTIONAL ALERT: A Golden Rule has been overridden 3+ times. This review must address whether the rule should be updated or whether behavior must change." if constitutional_issue else ""}

Produce a JSON long-term plan:
{{
  "evaluated_at": "{today}",
  "phase_map": [
    {{"phase": "<name>", "timeframe": "<e.g., Now - Month 6>", "focus": "<primary objective>",
     "milestones": ["<list>"]}}
  ],
  "3yr_vision": "<1-2 sentences: what does success look like in 3 years?>",
  "annual_objectives": ["<up to 3 concrete annual objectives>"],
  "constitutional_status": {{
    "rules_stable": <true|false>,
    "issues": ["<any violations or tensions>"],
    "recommendation": "<update rules | change behavior | no action needed>"
  }},
  "markov_note": "<what next quarter's review should know about the trajectory>",
  "updated_at": "{today}"
}}

Return ONLY the JSON object."""

    if dry_run:
        print(f"  [DRY RUN] longterm_eval() would produce 3-year arc update")
        set_level_state("LONGTERM", "IDLE")
        return None

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            metadata={"mode": "longterm_eval"},
        )
        raw = result.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        plan = json.loads(raw.strip())
    except Exception as e:
        print(f"  longterm_eval(): Error — {e}")
        set_level_state("LONGTERM", "IDLE")
        return None

    set_level_state("LONGTERM", "COMMITTING")
    write_single_summary("LONGTERM_PLAN", plan)
    print(f"  longterm_eval(): Long-term plan committed — 3yr vision set, {len(plan.get('phase_map', []))} phases")

    # Always notify athlete when long-term arc is updated (structural change)
    if not dry_run:
        try:
            from telegram_utils import send_telegram_message
            phases = plan.get("phase_map", [])
            vision = plan.get("three_year_vision", "")
            annual_objectives = plan.get("annual_objectives", [])
            phase_summary = "\n".join(
                f"  Phase {i+1}: {p.get('label', '?')} ({p.get('start', '?')} - {p.get('end', '?')})"
                for i, p in enumerate(phases[:4])
            ) if phases else "  (no phases defined)"
            obj_text = "\n".join(f"  - {o}" for o in annual_objectives[:3]) if annual_objectives else "  (none)"
            notif = (
                f"Long-term arc updated ({today}).\n\n"
                f"3-year vision: {vision}\n\n"
                f"Phase map:\n{phase_summary}\n\n"
                f"This year's objectives:\n{obj_text}\n\n"
                f"Reply 'show arc' to see the full plan."
            )
            send_telegram_message(notif)
        except Exception as _notify_err:
            print(f"  longterm_eval(): Structural notification failed (non-fatal): {_notify_err}")

    set_level_state("LONGTERM", "IDLE")
    return plan


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
