"""
WorkoutAdvisorAgent — specialized agent for real-time workout adaptation.

Triggered from telegram_bot.py when the athlete asks about today's workout,
needs to modify a session, asks for substitutions, or wants to train shorter/lighter.

Unlike the generic Telegram responder (which uses Coach State only), this agent
loads the actual program data for the current week — prescribed exercises, sets,
reps, weights — and reasons about it with Sonnet + full context.
"""

import os
import sys

# Allow running from repo root or src/
sys.path.insert(0, os.path.dirname(__file__))

import anthropic
from config import ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL, resolve_program_start_date, compute_current_week


TRIGGER_KEYWORDS = [
    "workout", "session", "train", "gym", "exercise", "lift",
    "tired", "exhausted", "fatigue", "sore", "skip", "skip day",
    "substitute", "alternative", "swap", "replace",
    "only have", "short on time", "not enough time",
    "can i do", "what should i", "what do i", "should i train",
    "today's", "today", "this week",
    "modified", "shorter", "lighter", "reduced",
    "day 1", "day 2", "day 3", "day 4",
    "squat", "bench", "deadlift", "overhead", "press", "row", "pull",
]

SYSTEM_PROMPT = f"""You are {ATHLETE_NAME}'s strength coach, answering a specific question about today's workout or training adaptation.

You have access to this week's prescribed program and recent training history. Use it to give concrete, practical advice.

When adapting a session:
- Prioritize the main compound lift for the day first
- Keep the most important accessory work, drop the rest
- Suggest adjusted weights/sets/reps if needed (e.g. fatigue, time constraints)
- If a substitution is needed, give a specific alternative — not "something similar"

Tone: direct, specific. No preamble. 2-6 sentences unless the question genuinely needs more.
This is a Telegram message — no formatting headers, natural coach voice."""


def is_workout_query(message: str) -> bool:
    """Return True if the message is likely asking about today's workout or training adaptation."""
    lower = message.lower()
    return any(kw in lower for kw in TRIGGER_KEYWORDS)


def _format_program_for_context(program_data: dict) -> str:
    """Extract today's and this week's prescribed workout from program_data."""
    lines = []

    week_num = program_data.get("current_week_num")
    if week_num:
        lines.append(f"CURRENT WEEK: Week {week_num}")

    current_week = program_data.get("current_week", {})
    days = current_week.get("days", [])

    if days:
        lines.append("\nTHIS WEEK'S PROGRAM:")
        for day_data in days:
            day_name = day_data.get("label", day_data.get("day_name", "Day"))
            exercises = day_data.get("exercises", [])
            lines.append(f"\n  {day_name}:")
            for idx, ex in enumerate(exercises, start=1):
                name = ex.get("name", ex.get("exercise", ""))
                weight = ex.get("weight", "")
                sets_reps = ex.get("sets_reps", "")
                actual = ex.get("actual", "")
                ex_done = ex.get("done", False)
                if name:
                    # Import inline to avoid circular dependency at module load
                    try:
                        from run_coach import _infer_muscle_group
                        muscle = f" [{_infer_muscle_group(name)}]"
                    except Exception:
                        muscle = ""
                    prescribed = f"{weight} {sets_reps}".strip()
                    actual_str = f" → actual: {actual}" if actual and ex_done else ""
                    tick = " ✓" if ex_done else ""
                    lines.append(f"    {idx}.{muscle} {name}: {prescribed}{actual_str}{tick}")

    session_notes = current_week.get("session_notes", "")
    if session_notes:
        lines.append(f"\nSESSION NOTES: {session_notes}")

    return "\n".join(lines) if lines else "(program data unavailable)"


def respond(user_message: str, base_context: str) -> str:
    """
    Generate a workout-specific response using full program context.

    Args:
        user_message: The athlete's Telegram message
        base_context: The standard bot context (lift history, profile, goals, etc.)

    Returns:
        Response string from Claude Sonnet
    """
    # Load current week's program data
    program_context = "(program sheet not accessible)"
    try:
        from sheets import read_program_data
        week_num = compute_current_week(resolve_program_start_date())
        program_data = read_program_data(week_num=week_num)
        program_context = _format_program_for_context(program_data)
    except Exception as e:
        print(f"[WorkoutAgent] Program load failed (non-fatal): {e}")

    full_context = f"""## THIS WEEK'S PROGRAM
{program_context}

---

## ATHLETE CONTEXT (history, goals, current levels)
{base_context}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=CLAUDE_MODEL,  # Sonnet — this warrants full reasoning
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"{full_context}\n\n---\n\nATHLETE (via Telegram): {user_message}\n\nRespond as the coach."
        }]
    )
    return response.content[0].text
