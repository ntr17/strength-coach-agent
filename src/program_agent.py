"""
ProgramDesignerAgent — a thoughtful coach decision-maker, not a program factory.

Triggered from telegram_bot.py when the athlete says anything about their program
structure (new program, deload, comeback, next block, etc.).

The agent REASONS FIRST before acting. It reads full context (current week,
remaining weeks, recent weights, health, goals) and produces one of three decisions:

  REJECT         — request doesn't make sense; agent challenges the athlete
  MODIFY_CURRENT — best to adjust the existing program (comeback weeks, deload
                   in-place, weight adjustments, etc.)
  CREATE_NEW     — a genuinely new program is warranted

The agent uses extended thinking so it can reason deeply before committing.
It will push back if it sees 5 weeks left in a block, if weights are still
progressing, if the athlete is just frustrated after one bad session, etc.

Trigger examples:
  "design me a deload week"
  "create a new 4-week block after this program"
  "I need comeback sessions after vacation"
  "my squat is stalling, new program?"
  "what should I do next?"
  "nuevo programa" / "programa nuevo"
"""

import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))

import anthropic
import gspread

from config import ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL, GMAIL_TO, PROGRAM_SHEET_ID

# Extended thinking needs sufficient tokens
REASONING_MODEL = CLAUDE_MODEL          # claude-sonnet-4-6 (or override with opus)
THINKING_BUDGET  = 16000                # tokens for internal reasoning
MAX_TOKENS       = 24000                # must be > thinking_budget

# Only trigger on clearly STRUCTURAL requests — not questions, not workout logs.
# "plateau", "stalling", "struggling" → WorkoutAdvisorAgent (coaching question, not structural)
TRIGGER_KEYWORDS = [
    "new program", "new block", "next block", "design program", "build program",
    "create program", "write program", "design block", "new cycle",
    "deload", "deload week", "transition week",
    "after this program", "after this block", "after vacation",
    "next phase", "next program",
    "comeback sessions", "come back sessions", "ease back in",
    "restart weights", "reset weights", "scale back weights", "scale weights",
    "ramp back up", "ramp up weights",
    "programa", "nuevo programa", "diseña", "crea un programa",
    "bloque", "nuevo bloque", "siguiente bloque",
    "semanas de vuelta", "semanas de regreso",
]


# ---------------------------------------------------------------------------
# Keyword detection
# ---------------------------------------------------------------------------

def is_program_design_query(message: str) -> bool:
    """Return True if the message is requesting a program design or modification."""
    lower = message.lower()
    return any(kw in lower for kw in TRIGGER_KEYWORDS)


# ---------------------------------------------------------------------------
# System prompt for the reasoning + decision pass
# ---------------------------------------------------------------------------

_DECISION_SYSTEM = f"""You are {ATHLETE_NAME}'s head strength coach. You have been coaching them for a long time and you know their history, goals, and tendencies.

An athlete has come to you with a program-related request. Your job is NOT to automatically create or change things — it is to REASON, then DECIDE, then ACT.

You have three possible decisions:

1. REJECT — Use for ANY of these:
   a. The request doesn't make sense right now. Push back honestly with data. Examples:
      - "You have 5 weeks left in your current block — abandoning it now would waste accumulated fatigue."
      - "Your squat has gone up 10kg in 6 weeks. That is not stalling. Stay the course."
      - "One bad session after vacation is not a reason to redesign your program."
   b. The message is NOT actually a program design request. It might be:
      - A workout log ("did squats today, felt strong") — log it, not my job here
      - A performance question ("why is my bench stalling?") — coaching question, not structural
      - A tracking request ("track hip thrust as main lift") — memory update, not structural
      - General chitchat or a vague question
      For these cases, REJECT and redirect: "That's a coaching question, not a program design request — I'll cover it in the next email / via the workout agent."

2. MODIFY_CURRENT — The existing program should be adjusted, not replaced. Examples:
   - Back from vacation → scale weights in the next 2 weeks, then ramp back up
   - Persistent joint pain → swap a heavy compound for a variation across remaining weeks
   - Explicit deload request mid-block → reduce weights/volume in-place for current week
   - Coming back from illness → reduce load for next 1-2 weeks
   This is the DEFAULT choice for anything that can be handled within the existing structure.

3. CREATE_NEW — A genuinely new program is warranted. Only choose this if:
   - The current program is complete (at or past final week)
   - The athlete's goals have fundamentally shifted
   - A major life event (surgery, long illness > 3 weeks) has fully reset their capacity
   - They have explicitly finished the current block and are asking for the next one

Rules:
- DEFAULT to MODIFY_CURRENT or REJECT before CREATE_NEW.
- If fewer than 4 weeks remain, lean toward MODIFY_CURRENT for remaining weeks; CREATE_NEW only for what comes AFTER.
- Never create a new program while a current one is active unless truly justified — and say so.
- Be direct and honest. Nacho prefers data over motivation and hates pandering.
- When rejecting misdirected messages (type b above), keep the challenge_message short and redirect clearly.

Output a single valid JSON object — NO explanation, no markdown, just JSON.

Schema:
{{
  "decision": "REJECT" | "MODIFY_CURRENT" | "CREATE_NEW",
  "reasoning": "<your internal reasoning summary — 2-4 sentences explaining WHY you chose this decision>",
  "challenge_message": "<if REJECT: the message to send the athlete — direct, honest, with data. Null if not REJECT.>",
  "modifications": [
    {{
      "operation": "WEIGHT_SCALE" | "WEIGHT_CHANGE" | "SETS_REPS_CHANGE" | "EXERCISE_SWAP" | "NOTE_ADD",
      "week": <int or null>,
      "day": <int or null>,
      "exercise": "<exercise name or null>",
      "new_value": "<new value as string or null>",
      "scale_pct": <float or null — for WEIGHT_SCALE, e.g. 85.0 for 85%>,
      "weeks_affected": [<int>, ...],
      "note_text": "<note text or null>",
      "description": "<human-readable description of this specific change>"
    }}
  ],
  "modification_summary": "<if MODIFY_CURRENT: 1-2 sentence summary of all changes made, to send the athlete. Null otherwise.>",
  "program": {{
    "name": "<program name>",
    "type": "deload" | "strength" | "hypertrophy" | "transition" | "custom",
    "total_weeks": <int>,
    "start_date": "<YYYY-MM-DD or 'TBD'>",
    "notes": "<coaching rationale>",
    "weeks": [
      {{
        "week_num": 1,
        "theme": "<week theme>",
        "days": [
          {{
            "day_num": 1,
            "label": "<day label>",
            "exercises": [
              {{
                "name": "<exercise name>",
                "weight": "<weight in kg>",
                "sets_reps": "<NxN format>",
                "notes": "<optional>"
              }}
            ]
          }}
        ]
      }}
    ]
  }}
}}

Notes:
- "modifications" should be [] if decision is REJECT or CREATE_NEW.
- "program" should be null if decision is REJECT or MODIFY_CURRENT.
- "challenge_message" is null unless decision is REJECT.
- "modification_summary" is null unless decision is MODIFY_CURRENT.
- For WEIGHT_SCALE: scale_pct is a percentage of current weight (85 = 85% = 15% reduction). Always include weeks_affected.
- For specific weights: be precise (use athlete's current weights from context, round to nearest 2.5kg).
- Standard 4-day split unless requested otherwise: Upper/Lower or Push/Pull/Legs/Full.
"""


# ---------------------------------------------------------------------------
# Reasoning + decision pass (extended thinking)
# ---------------------------------------------------------------------------

def _reason_and_decide(request: str, context: str) -> dict:
    """
    Use Claude with extended thinking to reason about the request and return
    a structured decision dict.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_content = (
        f"FULL CONTEXT:\n{context}\n\n"
        f"---\n\n"
        f"ATHLETE REQUEST: {request}\n\n"
        f"Reason carefully. Output only valid JSON."
    )

    try:
        # Extended thinking requires streaming for large max_tokens budgets
        with client.messages.stream(
            model=REASONING_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={
                "type": "enabled",
                "budget_tokens": THINKING_BUDGET,
            },
            system=_DECISION_SYSTEM,
            messages=[{"role": "user", "content": user_content}]
        ) as stream:
            response = stream.get_final_message()
    except anthropic.BadRequestError:
        # Extended thinking not available for this model version — fallback without it
        print("  [ProgramAgent] Extended thinking not available, falling back to standard call")
        response = client.messages.create(
            model=REASONING_MODEL,
            max_tokens=4096,
            system=_DECISION_SYSTEM,
            messages=[{"role": "user", "content": user_content}]
        )

    # Extract text block (skip thinking blocks)
    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    # Parse JSON
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"Could not parse decision JSON: {raw[:300]}")


# ---------------------------------------------------------------------------
# Google Sheet creation (CREATE_NEW path)
# ---------------------------------------------------------------------------

def _col_letter(n: int) -> str:
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _write_week_tab(ws: gspread.Worksheet, week: dict) -> None:
    rows = []
    week_num = week.get("week_num", "?")
    theme = week.get("theme", "")
    rows.append([f"WEEK {week_num}" + (f" — {theme}" if theme else "")])
    rows.append([])

    for day in week.get("days", []):
        day_num = day.get("day_num", "?")
        label = day.get("label", "")
        rows.append([f"DAY {day_num}: {label}"])
        rows.append(["Exercise", "Weight", "Sets x Reps", "Done", "Actual", "Notes", "Session Notes"])

        for ex in day.get("exercises", []):
            rows.append([
                ex.get("name", ""),
                ex.get("weight", ""),
                ex.get("sets_reps", ""),
                "☐",
                "",
                ex.get("notes", ""),
                "",
            ])
        rows.append([])

    rows.append(["WEEKLY NOTES"])
    rows.append(["Bodyweight:", ""])
    rows.append(["Sleep:", ""])
    rows.append(["Energy:", ""])
    rows.append(["Notes:", ""])

    ws.update(f"A1:G{len(rows)}", rows)

    try:
        ws.format("A1", {"textFormat": {"bold": True, "fontSize": 12}})
        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).startswith("DAY "):
                ws.format(f"A{i}", {"textFormat": {"bold": True}})
            elif row and row[0] == "Exercise":
                ws.format(f"A{i}:G{i}", {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}
                })
    except Exception:
        pass


def _create_program_sheet(program: dict, share_email: str = None) -> str:
    """Create a new Google Sheet for the program. Returns the sheet URL."""
    from sheets import get_client
    client = get_client()

    name = program.get("name", "New Program")

    spreadsheet = client.create(f"{ATHLETE_NAME} — {name}")
    print(f"  [ProgramAgent] Created sheet: {spreadsheet.url}")

    if share_email:
        try:
            spreadsheet.share(share_email, perm_type="user", role="writer")
            print(f"  [ProgramAgent] Shared with {share_email}")
        except Exception as e:
            print(f"  [ProgramAgent] Share failed (non-fatal): {e}")

    weeks = program.get("weeks", [])
    for i, week in enumerate(weeks):
        week_num = week.get("week_num", i + 1)
        tab_name = f"Week {week_num}"
        if i == 0:
            ws = spreadsheet.sheet1
            ws.update_title(tab_name)
        else:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=100, cols=10)
        _write_week_tab(ws, week)
        print(f"  [ProgramAgent] Wrote {tab_name}")

    return spreadsheet.url


def _register_program(sheet_id: str, program: dict) -> None:
    """Register the new program in the Active Sheets registry."""
    try:
        from memory import _get_memory_sheet, TAB_SHEET_REGISTRY

        name = program.get("name", "New Program")
        total_weeks = program.get("total_weeks", 1)
        start_date = program.get("start_date", "TBD")
        notes = program.get("notes", "")
        prog_type = program.get("type", "strength")

        sheet = _get_memory_sheet()
        ws = sheet.worksheet(TAB_SHEET_REGISTRY)
        ws.append_row([
            name, sheet_id, prog_type.upper(), "PENDING",
            str(date.today()), start_date, str(total_weeks), notes
        ])
        print(f"  [ProgramAgent] Registered '{name}' in Active Sheets")
    except Exception as e:
        print(f"  [ProgramAgent] Registry write failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# MODIFY_CURRENT path — apply via writeback
# ---------------------------------------------------------------------------

def _apply_modifications(modifications: list, program_sheet_id: str = None) -> list:
    """
    Apply a list of modification operations to the current program sheet.
    Returns a list of result strings (one per op).
    """
    if not modifications:
        return []

    if not program_sheet_id:
        program_sheet_id = PROGRAM_SHEET_ID

    if not program_sheet_id:
        return ["Program sheet not configured — could not apply modifications automatically."]

    try:
        from sheets import get_client
        gc = get_client()
        sheet = gc.open_by_key(program_sheet_id)
    except Exception as e:
        return [f"Could not open program sheet: {e}"]

    from writeback import (
        _apply_weight_change,
        _apply_sets_reps_change,
        _apply_exercise_swap,
        _apply_note_add,
        _apply_weight_scale,
    )

    results = []
    for op in modifications:
        op_type = op.get("operation", "UNKNOWN")
        try:
            if op_type == "WEIGHT_SCALE":
                ok, msg = _apply_weight_scale(sheet, op)
            elif op_type == "WEIGHT_CHANGE":
                ok, msg = _apply_weight_change(sheet, op)
            elif op_type == "SETS_REPS_CHANGE":
                ok, msg = _apply_sets_reps_change(sheet, op)
            elif op_type == "EXERCISE_SWAP":
                ok, msg = _apply_exercise_swap(sheet, op)
            elif op_type == "NOTE_ADD":
                ok, msg = _apply_note_add(sheet, op)
            else:
                ok, msg = False, f"Unknown operation: {op_type}"

            status = "✓" if ok else "✗"
            results.append(f"{status} {msg}")
            print(f"  [ProgramAgent] {status} {op_type}: {msg}")
        except Exception as e:
            results.append(f"✗ {op_type} failed: {e}")
            print(f"  [ProgramAgent] ✗ {op_type} exception: {e}")

    return results


# ---------------------------------------------------------------------------
# Build context for the reasoning pass
# ---------------------------------------------------------------------------

def _build_context(base_context: str, memory_data: dict = None) -> str:
    """Assemble rich context for the reasoning pass."""
    sections = [base_context] if base_context else []

    if not memory_data:
        return "\n\n".join(sections)

    # Coach State (compressed domain summaries)
    coach_state = memory_data.get("coach_state", {})
    if coach_state:
        lines = []
        for domain, data in coach_state.items():
            summary = data.get("Summary", str(data)) if isinstance(data, dict) else str(data)
            lines.append(f"  {domain}: {summary}")
        sections.append("CURRENT STATE (coach summaries)\n" + "\n".join(lines))

    # Goals
    goals = memory_data.get("long_term_goals", "")
    if goals:
        sections.append(f"LONG-TERM GOALS\n{str(goals)[:400]}")

    # Health
    health_log = memory_data.get("health_log", [])
    if health_log:
        recent = health_log[-7:]
        lines = []
        for e in recent:
            d = e.get("Date", "?")
            bw = e.get("Bodyweight (kg)", "")
            sleep = e.get("Sleep (hrs)", "")
            lines.append(f"  [{d}] BW:{bw}kg sleep:{sleep}h")
        sections.append("RECENT HEALTH\n" + "\n".join(lines))

    # Program registry (to know weeks remaining)
    sheet_registry = memory_data.get("sheet_registry", [])
    if sheet_registry:
        active = [r for r in sheet_registry if isinstance(r, dict)
                  and str(r.get("Status", "")).upper() in ("ACTIVE", "PENDING", "")]
        if active:
            reg_lines = []
            for r in active[:3]:
                reg_lines.append(
                    f"  {r.get('Name', '?')} | {r.get('Type', '?')} | "
                    f"Status:{r.get('Status', '?')} | Weeks:{r.get('Total Weeks', '?')} | "
                    f"Start:{r.get('Start Date', '?')}"
                )
            sections.append("ACTIVE PROGRAMS (registry)\n" + "\n".join(reg_lines))

    # Commands / pending proposals
    commands = memory_data.get("commands", [])
    if commands:
        active_cmds = [c for c in commands if isinstance(c, dict)
                       and str(c.get("Status", "")).upper() not in ("APPLIED", "CANCELLED")]
        if active_cmds:
            cmd_lines = [f"  {c.get('Type', '?')}: {c.get('Value', '')[:100]}"
                         for c in active_cmds[:5]]
            sections.append("ACTIVE COMMANDS / PROPOSALS\n" + "\n".join(cmd_lines))

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Main respond function
# ---------------------------------------------------------------------------

def respond(user_message: str, base_context: str, memory_data: dict = None,
            current_week: int = None, program_sheet_id: str = None) -> str:
    """
    Handle a program-related request from the athlete.

    Flow:
    1. Build rich context
    2. Extended thinking reasoning pass → REJECT / MODIFY_CURRENT / CREATE_NEW
    3. Execute the decision
    4. Return a Telegram reply

    Returns the reply string to send to the athlete.
    """
    # Load memory if not provided
    if not memory_data:
        try:
            from memory import read_all
            memory_data = read_all()
        except Exception as e:
            print(f"  [ProgramAgent] Memory load failed (non-fatal): {e}")

    full_context = _build_context(base_context, memory_data)

    # Resolve current week if not passed
    if current_week is None:
        try:
            from config import resolve_program_start_date
            from datetime import date as _date, timedelta
            start = resolve_program_start_date()
            start_dt = _date.fromisoformat(start) if start else None
            if start_dt:
                current_week = max(1, (((_date.today() - start_dt).days) // 7) + 1)
        except Exception:
            pass

    if not program_sheet_id:
        program_sheet_id = PROGRAM_SHEET_ID

    print(f"  [ProgramAgent] Reasoning about: {user_message[:80]}")
    if current_week:
        print(f"  [ProgramAgent] Current week: {current_week}")

    # Step 1: Reason and decide
    try:
        decision_obj = _reason_and_decide(user_message, full_context)
    except Exception as e:
        return f"I tried to reason about your request but hit an error: {e}. Be more specific and try again."

    decision = decision_obj.get("decision", "REJECT")
    reasoning = decision_obj.get("reasoning", "")
    print(f"  [ProgramAgent] Decision: {decision} — {reasoning[:120]}")

    # Step 2: Execute

    today = str(date.today())

    # --- REJECT ---
    if decision == "REJECT":
        challenge = decision_obj.get("challenge_message")
        reply = challenge or f"I don't think that's the right move right now. {reasoning}"

        # Log so the daily email can reference it
        _log_decision("REJECT", f"[Program agent] Rejected: {user_message[:80]}. Reason: {reasoning[:120]}", today)

        return reply

    # --- MODIFY_CURRENT ---
    if decision == "MODIFY_CURRENT":
        modifications = decision_obj.get("modifications") or []
        summary = decision_obj.get("modification_summary") or ""

        if not modifications:
            # Agent decided modify but gave no specific ops — return coaching note only
            _log_decision("MODIFY_NOTE", f"[Program agent] Modification note: {reasoning[:120]}", today)
            return summary or f"I've reviewed your program. {reasoning}"

        if not program_sheet_id:
            # No sheet ID — produce manual instructions + log as proposal
            change_list = "\n".join(
                f"  • {op.get('description', str(op))}" for op in modifications
            )
            proposal_text = f"Program modifications (apply manually):\n{change_list}"
            try:
                from memory import append_command
                append_command("PENDING_PROPOSAL", proposal_text)
            except Exception as e:
                print(f"  [ProgramAgent] Could not log proposal: {e}")
            _log_decision("MODIFY_PROPOSAL", f"[Program agent] Queued as proposal (no sheet ID): {reasoning[:80]}", today)
            return (
                f"{summary}\n\n"
                f"I can't auto-apply these without your program sheet configured, "
                f"so I've queued them as a proposal:\n{change_list}\n\n"
                f"Confirm and I'll guide you through the manual changes."
            )

        results = _apply_modifications(modifications, program_sheet_id=program_sheet_id)

        applied = [r for r in results if r.startswith("✓")]
        failed  = [r for r in results if r.startswith("✗")]

        # Log as LANDMARK
        applied_summary = "; ".join(r[2:] for r in applied[:3])
        _log_decision(
            "MODIFY_APPLIED",
            f"[Program agent] Modified current program: {applied_summary or reasoning[:80]}",
            today,
            priority="HIGH" if applied else "NORMAL",
        )

        lines = []
        if summary:
            lines.append(summary)
        if applied:
            lines.append("")
            lines.append(f"Applied {len(applied)} change{'s' if len(applied) != 1 else ''} to your program:")
            lines.extend(f"  {r}" for r in applied)
        if failed:
            lines.append("")
            lines.append(f"Couldn't auto-apply {len(failed)} change{'s' if len(failed) != 1 else ''}:")
            lines.extend(f"  {r}" for r in failed)
            lines.append("Update those manually in the sheet.")

        return "\n".join(lines).strip() or "Program modifications done."

    # --- CREATE_NEW ---
    if decision == "CREATE_NEW":
        program = decision_obj.get("program")
        if not program:
            return (
                "I decided a new program is appropriate, but something went wrong "
                "generating the structure. Try again with more details about what you want."
            )

        name = program.get("name", "New Program")
        total_weeks = program.get("total_weeks", "?")
        notes = program.get("notes", "")
        weeks_data = program.get("weeks", [])
        day_count = sum(len(w.get("days", [])) for w in weeks_data)

        print(f"  [ProgramAgent] Creating sheet: {name} ({total_weeks} weeks, {day_count} training days)")

        try:
            sheet_url = _create_program_sheet(program, share_email=GMAIL_TO)
            sheet_id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
            sheet_id = sheet_id_match.group(1) if sheet_id_match else ""
        except Exception as e:
            _log_decision("CREATE_FAILED", f"[Program agent] Sheet creation failed for '{name}': {e}", today)
            return (
                f"I designed the program but couldn't create the sheet: {e}. "
                f"Program: {name} ({total_weeks} weeks). {notes}"
            )

        if sheet_id:
            _register_program(sheet_id, program)
            # Persist new sheet ID to Coach State so the bot picks it up without
            # requiring a manual Railway/GitHub Secrets update
            try:
                from memory import upsert_coach_state
                upsert_coach_state("ACTIVE_PROGRAM_SHEET_ID", sheet_id, "HIGH")
                print(f"  [ProgramAgent] ACTIVE_PROGRAM_SHEET_ID updated in Coach State")
            except Exception as _e:
                print(f"  [ProgramAgent] Could not persist sheet ID to Coach State: {_e}")

        _log_decision(
            "CREATE_NEW",
            f"[Program agent] Created new program: {name} ({total_weeks} weeks). {reasoning[:80]}",
            today,
            priority="HIGH",
        )

        reply_lines = [
            f"Done. Created **{name}** ({total_weeks} week{'s' if total_weeks != 1 else ''}, {day_count} training days).",
            "",
            notes if notes else "",
            "",
            f"Reasoning: {reasoning}" if reasoning else "",
            "",
            f"Sheet: {sheet_url}",
            "",
            "Review it and confirm when you want to activate it — I'll start reading from it as your program.",
        ]
        return "\n".join(line for line in reply_lines if line is not None).strip()

    # Fallback (should not reach here)
    return f"Unexpected decision state. Raw: {json.dumps(decision_obj)[:300]}"


# ---------------------------------------------------------------------------
# Memory logging helper
# ---------------------------------------------------------------------------

def _log_decision(event_type: str, message: str, today: str, priority: str = "NORMAL") -> None:
    """Log a program agent decision to Coach Focus (best-effort, non-fatal)."""
    try:
        from memory import append_coach_focus
        focus_type = "LANDMARK" if event_type in ("MODIFY_APPLIED", "CREATE_NEW") else "FOLLOWUP"
        append_coach_focus(focus_type, message, last_mentioned=today, priority=priority)
    except Exception as e:
        print(f"  [ProgramAgent] Memory log failed (non-fatal): {e}")
