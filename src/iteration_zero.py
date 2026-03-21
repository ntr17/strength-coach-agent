"""
iteration_zero.py — V17 Interactive Initialization Interview

The coach interviews the athlete until it has everything it needs.
Does NOT stop after surface-level answers — iterates with probing follow-ups
until it can pass its own internal coverage test (10 edge-case scenarios).

Coverage areas:
  1. Golden Rules — stress-tested until edge cases resolved
  2. Long-term vision (3yr picture — not just gym numbers)
  3. Medium goals (current program targets + next 12-18mo)
  4. Health snapshot (biometrics, conditions, medications, supplements)
  5. Athletic profile (training history, movement, recovery, tendencies)
  6. Profile & identity (profession, psychology, personality, preferences)
  7. Availability (next 6 months, gyms and equipment)

State stored in ITERATION_ZERO Coach State domain (progress tracking).
Can be interrupted and resumed (idempotent).

Entry point:
  run_iteration_zero(dry_run=False) — called via --init flag
  handle_iteration_zero_reply(message: str) — called from telegram_bot.py
    when ITERATION_ZERO state is IN_PROGRESS
"""

import json
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# Coverage areas and their completion schema
# ---------------------------------------------------------------------------

COVERAGE_AREAS = [
    "golden_rules",
    "longterm_vision",
    "medium_goals",
    "health_snapshot",
    "athletic_profile",
    "profile_identity",
    "availability",
]

COVERAGE_PROMPTS = {
    "golden_rules": """What are your Golden Rules — the 3-5 things you'd never trade away in training?
Don't give me a wish list. Give me the things you'd refuse to compromise even if a coach told you it would get better results faster.""",

    "longterm_vision": """3 years from now — what does success look like? Not just in the gym.
What are you building toward? Who do you want to be as an athlete?""",

    "medium_goals": """What are the concrete targets you're chasing in the next 12-18 months?
Specific lifts, body composition, performance milestones — numbers.""",

    "health_snapshot": """Health baseline: bodyweight, any known conditions (injuries, metabolic, hormonal),
medications or supplements you take regularly. Blood work if you have it.""",

    "athletic_profile": """Training history: how long, what styles (powerlifting, bodybuilding, functional, etc.)?
Any movement restrictions, things that consistently cause issues?
How do you recover — fast, slow? How do you handle high volume vs high intensity?""",

    "profile_identity": """Work situation: hours, travel frequency, when can you actually train?
What makes you skip sessions? What keeps you consistent? What coaching style works for you
and what pisses you off?""",

    "availability": """Next 6 months: what does your calendar look like? Any extended trips, vacations,
periods without gym access?
Gyms: what's in your Madrid gym? What do you have at home? What's available when you travel?""",
}

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def read_iteration_zero_state() -> dict:
    """Read ITERATION_ZERO Coach State domain. Returns progress dict."""
    try:
        from memory import read_coach_state
        cs = read_coach_state()
        raw = cs.get("ITERATION_ZERO", {}).get("summary", "")
        if not raw:
            return _default_state()
        data = json.loads(raw)
        # Ensure all fields present
        defaults = _default_state()
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return _default_state()


def write_iteration_zero_state(state: dict) -> None:
    """Write ITERATION_ZERO state to Coach State."""
    try:
        from memory import upsert_coach_state
        upsert_coach_state("ITERATION_ZERO", json.dumps(state, ensure_ascii=False), "HIGH")
    except Exception as e:
        print(f"  [iteration_zero] State write failed: {e}")


def _default_state() -> dict:
    return {
        "status": "NOT_STARTED",  # NOT_STARTED | IN_PROGRESS | COVERAGE_TESTING | COMPLETE
        "current_area": None,
        "areas_complete": [],
        "areas_data": {},          # area → collected data summary
        "conversation_log": [],    # list of {role, text} for current session
        "coverage_test_passed": False,
        "started_at": None,
        "completed_at": None,
        "awaiting_message_id": None,
    }


# ---------------------------------------------------------------------------
# Coverage test
# ---------------------------------------------------------------------------

def run_coverage_test(areas_data: dict) -> dict:
    """
    Run the internal coverage test: can the coach reason through 10 edge-case scenarios
    without gaps in knowledge?

    Returns: {"passed": bool, "gaps": [str], "score": int/10}
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

    data_summary = json.dumps(areas_data, indent=2, ensure_ascii=False)

    prompt = f"""You are a strength coaching AI. You have collected the following athlete data during an initialization interview:

{data_summary}

Run your internal coverage test: can you reason through ALL of these 10 scenarios
without missing critical context? For each scenario, give a brief answer AND flag any
information gap that would prevent a confident answer.

SCENARIOS:
1. Athlete wants to train during a 2-week London work trip — what do you prescribe?
2. Blood work shows elevated HOMA-IR — how does this affect the next month of programming?
3. Athlete's squat has stalled for 3 consecutive weeks — what's your diagnosis and next step?
4. Athlete says "I want to add Olympic lifting" — does this conflict with any Golden Rules?
5. A close family event requires skipping 2 weeks of training — how do you adapt?
6. Athlete is traveling 3 days/week for the next month — how do you schedule training?
7. The current program ends in 4 weeks — what program do you design next?
8. Athlete reports right elbow pain on pulling movements — what changes immediately?
9. Athlete asks to cut body fat aggressively before a summer event — does this conflict with Golden Rules?
10. It's Week 22, athlete is behind on squat target — what's the realistic projection and what do you do?

For each: [ANSWER] / [GAP: <what's missing if any>]

After all 10, give:
COVERAGE SCORE: X/10
GAPS SUMMARY: <list any recurring gaps>
VERDICT: PASS (score >= 8 and no critical gaps) or FAIL"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = result.content[0].text.strip()

        import re
        # Parse verdict — accept both "VERDICT: PASS" and "PASS" near end of text
        passed = bool(re.search(r"VERDICT:\s*PASS", text, re.IGNORECASE))

        # Parse score — accept "COVERAGE SCORE: X/10" or just "X/10" near score label
        score = 0
        m = re.search(r"(?:COVERAGE SCORE|SCORE)[:\s]+(\d+)\s*/\s*10", text, re.IGNORECASE)
        if m:
            score = int(m.group(1))

        # Extract gaps — [GAP: ...] blocks; filter trivial
        gap_section = re.findall(r"\[GAP:\s*([^\]]+)\]", text)
        gaps = [g.strip() for g in gap_section if g.strip().lower() not in ("none", "no gap", "n/a", "")]

        # If score >= 8 and no critical gaps, treat as pass even if VERDICT line is missing
        if score >= 8 and not gaps:
            passed = True

        return {
            "passed": passed,
            "score": score,
            "gaps": gaps,
            "raw_output": text[:800],
        }
    except Exception as e:
        return {"passed": False, "score": 0, "gaps": [f"Coverage test error: {e}"], "raw_output": ""}


# ---------------------------------------------------------------------------
# Interview logic
# ---------------------------------------------------------------------------

def run_iteration_zero(dry_run: bool = False) -> None:
    """
    Entry point: start or resume the initialization interview.
    Sends the first question to the athlete via Telegram.
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, ATHLETE_NAME
    from cascade_state import push_thread, has_active_thread_of_type

    state = read_iteration_zero_state()

    if state["status"] == "COMPLETE":
        print(f"  [iteration_zero] Already complete. Run --init --force to restart.")
        return

    if state["status"] == "NOT_STARTED":
        state["status"] = "IN_PROGRESS"
        state["started_at"] = str(date.today())
        state["current_area"] = COVERAGE_AREAS[0]
        state["conversation_log"] = []
        print(f"  [iteration_zero] Starting initialization interview...")
    else:
        print(f"  [iteration_zero] Resuming interview — current area: {state['current_area']}")
        print(f"  [iteration_zero] Areas complete: {state['areas_complete']}")

    if dry_run:
        print(f"  [DRY RUN] Would send question for area: {state['current_area']}")
        print(f"  [DRY RUN] Question: {COVERAGE_PROMPTS.get(state['current_area'], '?')[:100]}")
        return

    # Build the opening message
    current_area = state["current_area"]
    question = _build_question(current_area, state, ATHLETE_NAME)

    try:
        from telegram_utils import send_telegram_message
        sent = send_telegram_message(question)
        if sent and isinstance(sent, dict):
            msg_id = sent.get("message_id") or sent.get("result", {}).get("message_id")
            state["awaiting_message_id"] = msg_id
            push_thread("MONTHLY_CONFIRM", message_id=msg_id or 0,
                        context={"type": "ITERATION_ZERO", "area": current_area})

        write_iteration_zero_state(state)
        print(f"  [iteration_zero] Question sent for area: {current_area}")
    except Exception as e:
        print(f"  [iteration_zero] Send failed: {e}")
        write_iteration_zero_state(state)


def handle_iteration_zero_reply(message: str) -> Optional[str]:
    """
    Process an athlete reply during the initialization interview.

    Called from telegram_bot.py when state is IN_PROGRESS.
    Returns the next question/response to send to the athlete,
    or None if the interview is complete.
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, ATHLETE_NAME

    state = read_iteration_zero_state()

    if state["status"] not in ("IN_PROGRESS", "COVERAGE_TESTING"):
        return None

    current_area = state["current_area"]

    # Add athlete reply to conversation log
    state["conversation_log"].append({"role": "athlete", "text": message})

    # Evaluate the reply with Sonnet — does it satisfy the area?
    eval_result = _evaluate_reply(
        area=current_area,
        conversation_log=state["conversation_log"],
        athlete_name=ATHLETE_NAME,
    )

    if eval_result["satisfied"]:
        # Store the collected data for this area
        state["areas_data"][current_area] = eval_result["summary"]
        state["areas_complete"].append(current_area)

        # Move to next area
        remaining = [a for a in COVERAGE_AREAS if a not in state["areas_complete"]]

        if not remaining:
            # All areas covered — run coverage test
            state["status"] = "COVERAGE_TESTING"
            write_iteration_zero_state(state)

            coverage = run_coverage_test(state["areas_data"])

            if coverage["passed"]:
                # Commit everything to Coach State
                _commit_iteration_zero(state)
                state["status"] = "COMPLETE"
                state["completed_at"] = str(date.today())
                state["coverage_test_passed"] = True
                write_iteration_zero_state(state)

                return (
                    f"Interview complete — I now have everything I need.\n\n"
                    f"Coverage score: {coverage['score']}/10.\n\n"
                    f"I'm ready to build your first full cascade plan. "
                    f"I'll start with your Monthly plan and work down from there."
                )
            else:
                # Coverage failed — re-open gaps and immediately ask the next question
                state["status"] = "IN_PROGRESS"
                gap_area = _map_gap_to_area(coverage["gaps"][0] if coverage["gaps"] else "")
                state["current_area"] = gap_area
                state["conversation_log"] = []
                write_iteration_zero_state(state)

                from config import ATHLETE_NAME as _aname
                next_q = _build_question(gap_area, state, _aname)
                score_line = f"Coverage score {coverage['score']}/10 — " if coverage["score"] else ""
                return f"{score_line}need a bit more before I'm ready.\n\n{next_q}"
        else:
            # Move to next area
            state["current_area"] = remaining[0]
            state["conversation_log"] = []  # fresh conversation for new area
            write_iteration_zero_state(state)

            response = eval_result.get("transition_message", "")
            next_q = _build_question(remaining[0], state, ATHLETE_NAME)
            return f"{response}\n\n{next_q}" if response else next_q

    else:
        # Not yet satisfied — send follow-up probe
        state["conversation_log"].append({"role": "coach", "text": eval_result["follow_up"]})
        write_iteration_zero_state(state)
        return eval_result["follow_up"]


# ---------------------------------------------------------------------------
# Evaluation and question building
# ---------------------------------------------------------------------------

def _build_question(area: str, state: dict, athlete_name: str) -> str:
    """Build the opening question for an area, potentially with context from previous answers."""
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

    base_question = COVERAGE_PROMPTS.get(area, "Tell me more about your training.")
    areas_done = state.get("areas_complete", [])

    if not areas_done:
        # First question — add framing
        return (
            f"I'm going to interview you until I have everything I need to coach you properly. "
            f"I won't accept vague answers — I'll push until I understand you well enough to "
            f"make coaching decisions I'm confident in. This might take a few rounds.\n\n"
            f"Let's start:\n\n{base_question}"
        )

    return base_question


def _evaluate_reply(area: str, conversation_log: list, athlete_name: str) -> dict:
    """
    Use Sonnet to evaluate if the athlete's answers are sufficient for this coverage area.

    Returns:
    {
        "satisfied": bool,
        "summary": str,            # if satisfied: compressed data for this area
        "follow_up": str,          # if not satisfied: specific probe question
        "transition_message": str, # if satisfied: brief acknowledgment before next area
    }
    """
    import anthropic
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

    conv_text = "\n".join(
        f"[{'ATHLETE' if m['role'] == 'athlete' else 'COACH'}]: {m['text']}"
        for m in conversation_log
    )

    criteria = {
        "golden_rules": (
            "3-5 non-negotiables are named AND at least 1 edge case has been stress-tested. "
            "I know what they'd sacrifice and what they'd never sacrifice."
        ),
        "longterm_vision": (
            "3-year picture is clear: type of athlete, performance level, lifestyle goals. "
            "Not just gym numbers."
        ),
        "medium_goals": (
            "At least 3 specific lift targets with numbers. Program end-state is defined."
        ),
        "health_snapshot": (
            "Bodyweight known. Any chronic conditions noted (or confirmed none). "
            "Medication/supplements noted (or confirmed none)."
        ),
        "athletic_profile": (
            "Training history in years. At least one movement restriction noted or confirmed none. "
            "Recovery speed characterized."
        ),
        "profile_identity": (
            "Work hours and travel pattern clear. At least one skip trigger identified. "
            "Preferred coaching tone characterized."
        ),
        "availability": (
            "Next 6 months calendar: any known blocks. Primary gym confirmed with key equipment. "
            "Home and travel options noted."
        ),
    }

    prompt = f"""You are evaluating whether an athlete's answers during a coaching initialization interview
are sufficient for the coverage area: {area.upper().replace('_', ' ')}.

SUFFICIENCY CRITERIA:
{criteria.get(area, "Core questions about this topic are answered.")}

CONVERSATION:
{conv_text}

Evaluate:
1. Is the sufficiency criteria MET? (yes/no)
2. If yes: write a 2-3 sentence compressed summary of what was learned.
   Also write a brief 1-sentence transition to the next topic.
3. If no: write ONE specific follow-up question that would get the missing information.
   Make it targeted — not a repeat of the original question.
   Challenge vague or surface-level answers. Don't accept "I want to be healthy" without probing.

RESPOND IN THIS EXACT JSON FORMAT:
{{
  "satisfied": true or false,
  "summary": "<compressed data summary — only if satisfied>",
  "transition_message": "<1 sentence bridging to next topic — only if satisfied>",
  "follow_up": "<specific probe question — only if not satisfied>"
}}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        result = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = result.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        # Safe fallback: treat as not satisfied, ask for more
        return {
            "satisfied": False,
            "summary": "",
            "transition_message": "",
            "follow_up": f"Can you tell me more about {area.replace('_', ' ')}?",
        }


def _map_gap_to_area(gap_description: str) -> str:
    """Map a gap description to the most relevant coverage area to re-open."""
    gap_lower = gap_description.lower()
    if any(kw in gap_lower for kw in ("golden rule", "trade", "sacrifice", "priority")):
        return "golden_rules"
    if any(kw in gap_lower for kw in ("gym", "equipment", "travel", "availability", "calendar")):
        return "availability"
    if any(kw in gap_lower for kw in ("health", "condition", "injury", "blood", "medical")):
        return "health_snapshot"
    if any(kw in gap_lower for kw in ("goal", "target", "lift", "squat", "bench")):
        return "medium_goals"
    if any(kw in gap_lower for kw in ("profile", "work", "psychology", "skip", "motivation")):
        return "profile_identity"
    return "golden_rules"  # default: re-stress-test the most important area


# ---------------------------------------------------------------------------
# Commit to Coach State on completion
# ---------------------------------------------------------------------------

def _commit_iteration_zero(state: dict) -> None:
    """
    Write all collected Iteration 0 data to the appropriate Coach State domains.
    Called when coverage test passes.
    """
    from memory import upsert_coach_state, write_single_summary

    areas = state.get("areas_data", {})

    # Golden Rules → GOLDEN_RULES domain
    if "golden_rules" in areas:
        rules_data = {
            "rules": [
                {"id": f"gr{i+1}", "rule": rule.strip(), "priority": 1 if i < 3 else 2}
                for i, rule in enumerate(areas["golden_rules"].split("\n")[:5])
                if rule.strip()
            ],
            "last_updated": str(date.today()),
            "override_log": [],
            "source": "iteration_zero",
        }
        upsert_coach_state("GOLDEN_RULES", json.dumps(rules_data, ensure_ascii=False), "HIGH")

    # Long-term + medium goals → ANNUAL_ARC (extends existing)
    combined_goals = ""
    if "longterm_vision" in areas:
        combined_goals += f"LONG-TERM VISION:\n{areas['longterm_vision']}\n\n"
    if "medium_goals" in areas:
        combined_goals += f"MEDIUM GOALS (12-18mo):\n{areas['medium_goals']}\n\n"
    if combined_goals:
        upsert_coach_state("ANNUAL_ARC", combined_goals.strip(), "HIGH")

    # Athletic profile + health → ATHLETE_MODEL
    athlete_model = ""
    if "athletic_profile" in areas:
        athlete_model += f"ATHLETIC PROFILE:\n{areas['athletic_profile']}\n\n"
    if "health_snapshot" in areas:
        athlete_model += f"HEALTH SNAPSHOT:\n{areas['health_snapshot']}\n\n"
    if "profile_identity" in areas:
        athlete_model += f"PROFILE & IDENTITY:\n{areas['profile_identity']}\n\n"
    if athlete_model:
        upsert_coach_state("ATHLETE_MODEL", athlete_model.strip(), "HIGH")

    # Availability → SCHEDULE domain
    if "availability" in areas:
        upsert_coach_state("SCHEDULE", areas["availability"], "HIGH")

    print(f"  [iteration_zero] Committed {len(areas)} coverage areas to Coach State.")
