"""
HealthAgent — specialized agent for health, recovery, and nutrition questions.

Triggered from telegram_bot.py when the athlete asks health-related questions.
Also contributes to the proactive pass for health-focused check-ins.

Reads: health log, Health Coach State domain, Athlete Profile.
Designed to work with incomplete data — accepts optional extra data blobs
(blood analysis, HRV/watch data, nutrition logs) when available.

Future data sources (not yet wired, just accepted as optional params):
  blood_data    — blood test results (dict or free text)
  hrv_data      — HRV / watch data (dict or free text)
  nutrition_data — food log or nutritional summary
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import anthropic
from config import ANTHROPIC_API_KEY, ATHLETE_NAME, CLAUDE_MODEL

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = CLAUDE_MODEL

TRIGGER_KEYWORDS = [
    # Recovery / energy
    "sleep", "tired", "fatigue", "energy", "rest", "recovery", "sore", "soreness",
    "exhausted", "burn out", "burnout", "overtraining", "deload",
    # Health / medical
    "sick", "ill", "cold", "flu", "injury", "pain", "hurt", "elbow", "golfer",
    "inflammation", "doctor", "blood", "test", "analysis", "marker",
    # Body composition
    "weight", "bodyweight", "fat", "lean", "bulk", "cut", "calories", "deficit",
    # Nutrition
    "eat", "eating", "food", "nutrition", "carb", "carbs", "protein", "diet",
    "meal", "fast", "fasting", "insulin", "glucose", "sugar",
    # Watch / metrics
    "hrv", "heart rate", "vo2", "steps", "resting", "zone",
    # General wellbeing
    "stress", "anxiety", "mood", "mental", "cortisol", "supplement", "creatine",
    "feel", "feeling", "how am i", "how are my",
]

SYSTEM_PROMPT = f"""You are {ATHLETE_NAME}'s coach, answering a specific health, recovery, or nutrition question.

You know {ATHLETE_NAME}'s health profile:
- Insulin resistance: carb timing matters. Carbs around training, lower at rest.
- Golfer's elbow (medial epicondylitis): watch pull volume and grip-heavy work.
- Works 14-16h/day, travels Mon-Thu biweekly — sleep and recovery are often compromised.
- Goals include body composition alongside strength.

How to respond:
- Work with whatever data is available. Incomplete data is normal — reason from what you have.
- Be specific. "Your sleep averaged 5.8h last week" beats "sleep seems low."
- Connect health signals to training. Poor sleep = expect lower output, longer recovery.
- Ask exactly one clarifying question if it would meaningfully change your advice.
- Don't lecture. One insight + one action is better than a list of five.

Tone: direct, caring, no fluff. This is Telegram — 2-5 sentences unless the question is complex.
No headers. Natural coach voice."""


def is_health_query(message: str) -> bool:
    """Return True if the message is likely a health, recovery, or nutrition question."""
    lower = message.lower()
    return any(kw in lower for kw in TRIGGER_KEYWORDS)


def _format_health_log(health_log: list, limit: int = 14) -> str:
    """Format recent health log entries as readable context."""
    if not health_log:
        return "(no health log data)"

    entries = health_log[-limit:]
    lines = []
    for e in entries:
        date = e.get("Date", "?")
        bw = e.get("Bodyweight (kg)", "")
        sleep = e.get("Sleep (hrs)", "")
        food = e.get("Food Quality (1-10)", "")
        notes = e.get("Notes", "")

        parts = [f"[{date}]"]
        if bw:
            parts.append(f"BW:{bw}kg")
        if sleep:
            parts.append(f"sleep:{sleep}h")
        if food:
            parts.append(f"food:{food}/10")
        if notes:
            parts.append(f"note:{notes[:60]}")
        lines.append(" ".join(parts))

    return "\n".join(lines)


def _format_health_coach_state(coach_state: dict) -> str:
    """Extract the HEALTH domain summary from Coach State."""
    if not coach_state:
        return "(no health coach state)"

    health = coach_state.get("HEALTH") or coach_state.get("health")
    if not health:
        return "(no HEALTH domain in coach state)"

    if isinstance(health, dict):
        return health.get("Summary", str(health))
    return str(health)


def _build_health_context(base_context: str, health_log: list,
                          coach_state: dict, extra_data: dict = None) -> str:
    """Assemble full context for the health agent."""
    sections = []

    if base_context:
        sections.append(f"## ATHLETE CONTEXT\n{base_context}")

    health_state = _format_health_coach_state(coach_state)
    sections.append(f"## HEALTH STATE (coach summary)\n{health_state}")

    log_text = _format_health_log(health_log)
    sections.append(f"## RECENT HEALTH LOG (last 14 days)\n{log_text}")

    if extra_data:
        if extra_data.get("blood_data"):
            sections.append(f"## BLOOD ANALYSIS\n{extra_data['blood_data']}")
        if extra_data.get("hrv_data"):
            sections.append(f"## HRV / WATCH DATA\n{extra_data['hrv_data']}")
        if extra_data.get("nutrition_data"):
            sections.append(f"## NUTRITION LOG\n{extra_data['nutrition_data']}")

    return "\n\n---\n\n".join(sections)


def respond(user_message: str, base_context: str,
            health_log: list = None, coach_state: dict = None,
            extra_data: dict = None) -> str:
    """
    Generate a health/recovery-focused response via Claude Sonnet.

    Args:
        user_message:  The athlete's Telegram message
        base_context:  Standard bot context (profile, goals, lift history)
        health_log:    Recent health log entries (list of dicts)
        coach_state:   Current Coach State dict (domain → summary)
        extra_data:    Optional dict with keys: blood_data, hrv_data, nutrition_data

    Returns:
        Response string from Claude
    """
    # Load health data if not passed in
    if health_log is None or coach_state is None:
        try:
            from memory import read_health_log, read_coach_state
            if health_log is None:
                health_log = read_health_log(limit=30)
            if coach_state is None:
                coach_state = read_coach_state()
        except Exception as e:
            print(f"[HealthAgent] Memory load failed (non-fatal): {e}")
            health_log = health_log or []
            coach_state = coach_state or {}

    full_context = _build_health_context(base_context, health_log, coach_state, extra_data)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"{full_context}\n\n"
                f"---\n\n"
                f"ATHLETE (via Telegram): {user_message}\n\n"
                f"Respond as the coach."
            )
        }]
    )
    return response.content[0].text


def run_health_proactive(health_log: list, coach_state: dict,
                         athlete_profile: str = "") -> str:
    """
    Lightweight proactive health reasoning pass (Haiku).

    Checks for patterns in health log + HEALTH Coach State and returns
    a Telegram message if outreach is warranted, or empty string if not.

    Called from run_proactive() — no extra API cost beyond the main proactive pass.
    """
    health_state = _format_health_coach_state(coach_state)
    log_text = _format_health_log(health_log, limit=10)

    prompt = f"""You are {ATHLETE_NAME}'s coach doing a brief health check-in.

HEALTH STATE: {health_state}

RECENT HEALTH LOG:
{log_text}

ATHLETE PROFILE (brief): {athlete_profile[:300] if athlete_profile else '(not loaded)'}

Should you reach out proactively about a health or recovery concern right now?

Criteria for YES:
- Sleep has been consistently poor (< 6h for 3+ days in a row)
- Energy or food quality is declining over a week
- A known health issue (golfer's elbow, insulin response) needs a check-in
- Bodyweight has drifted significantly without comment
- More than 5 days since any health data — worth asking if he's tracking

If YES, write a short Telegram message (1-3 sentences, direct, no emoji).
If NO, respond with just: NO_OUTREACH

Respond with either the Telegram message text or NO_OUTREACH."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    result = response.content[0].text.strip()

    if result.upper().startswith("NO_OUTREACH") or result.upper() == "NO":
        return ""
    return result


def generate_nutrition_summary(health_log: list, training_load: dict = None,
                                athlete_profile: str = "") -> str:
    """
    Generate a NUTRITION Coach State domain summary.
    Called weekly from write_coach_state_summaries().

    Analyzes health log trends (food quality, bodyweight, sleep) alongside training
    load (volume, fatigue) to produce actionable nutrition recommendations.

    Returns a 2-3 sentence summary for storage in Coach State as NUTRITION domain.
    Budget: Haiku, max 300 tokens.
    """
    if not health_log:
        return ""

    # Summarize health data
    log_text = _format_health_log(health_log, limit=14)

    # Training load summary
    load_text = ""
    if training_load:
        tsb = training_load.get("TSB")
        ctl = training_load.get("CTL")
        atl = training_load.get("ATL")
        if tsb is not None:
            load_text = f"Current training load: TSB={tsb:+.1f}, CTL={ctl:.1f}, ATL={atl:.1f}"
            if training_load.get("deload_recommended"):
                load_text += " — DELOAD week recommended"

    prompt = (
        f"You are {ATHLETE_NAME}'s nutrition and health coach.\n\n"
        f"ATHLETE PROFILE: {athlete_profile[:300] if athlete_profile else 'Insulin resistance, golfer elbow, finance professional, trains 4x/week'}\n\n"
        f"RECENT HEALTH LOG (14 days):\n{log_text}\n\n"
        f"TRAINING LOAD: {load_text or 'not available'}\n\n"
        f"Write 2-3 sentences covering: current carb timing recommendations given training load, "
        f"protein target adequacy based on bodyweight trend, and one actionable nutrition focus for "
        f"this week. Be specific to the data — don't give generic advice. "
        f"Note insulin resistance implications when relevant."
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"  Nutrition summary generation failed (non-fatal): {e}")
        return ""
