"""
Telegram Message Processor — runs at the start of each daily run.

Uses Haiku to classify unprocessed Telegram messages into structured events,
dispatches them to the appropriate memory tabs, and marks them processed.

This is how raw athlete messages become durable facts the coach can reason about.
"""

from datetime import date

import anthropic

from config import ANTHROPIC_API_KEY, ATHLETE_NAME


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

PROCESSOR_SYSTEM = f"""\
You are an information extractor for {ATHLETE_NAME}'s coaching system.

You receive raw Telegram messages from the athlete. Your job is to extract structured facts from them — not to reply, not to coach.

For each message (or cluster of related messages), output one or more structured lines.

CATEGORIES:
- SCHEDULE_CHANGE  — workout definitively skipped/missed with no plan to make it up
- PENDING_CATCHUP  — athlete plans to do a session on a different day ("will train Monday instead",
                     "catching up tomorrow", "doing it Wednesday"). Extract the intended day/date if mentioned.
                     FACT format: "Week N Day D → planned for [day/date]", e.g. "Week 9 Day 3 → Monday 2026-03-16"
- LIFE_EVENT       — travel, stress, illness, injury, life change that affects training
- PREFERENCE       — athlete feedback about coaching output (charts, email length, topics, preferred channel).
                     Channel preferences: if athlete says "reach me on Telegram", "use Telegram primarily",
                     "contact me by email", etc. — format as: "primary_channel: telegram" or "primary_channel: email"
- WORKOUT_UNPLANNED — unplanned/spontaneous session not on the program
- LIFT_UPDATE      — athlete reports a specific weight, set, PR, or performance.
                     FACT format: "exercise: <name> | weight: <kg> | sets_reps: <NxN> | date: <ISO or 'unknown'> | rpe: <N> | rir: <N>"
                     Include as many fields as can be extracted. RPE (Rate of Perceived Exertion, 1-10) and RIR (Reps in Reserve)
                     are optional — only include if explicitly mentioned. Always extract date if mentioned.
                     Examples:
                       "exercise: Squat | weight: 100 | sets_reps: 3x3 | date: 2026-03-11"
                       "exercise: Bench Press | weight: 90 | sets_reps: 4x5 | date: 2026-03-11 | rpe: 8"
                       "exercise: Deadlift | weight: 150 | sets_reps: 1x5 | date: unknown | rir: 2"
- MOOD_PERFORMANCE — qualitative notes about how a session felt, energy level during training, pain/discomfort
                     during specific exercises, RPE estimates, mental state. Capture anything that affects
                     how we interpret lift numbers. NOT pure health metrics (those are HEALTH_DATA).
                     Examples: "felt weak today, squats moved slow", "elbow pain on last bench set",
                     "energy was 6/10, slept poorly before", "RPE 9 on the last set — close to failure",
                     "back felt tight, stopped after 3 sets", "strongest session in weeks"
- TRACK_LIFT       — athlete wants to add or remove a lift as a tracked main/auxiliary lift
                     (phrases like "track X", "add X as main lift", "start monitoring X", "drop X from main lifts")
- HEALTH_DATA      — athlete reports health metrics: lab values (blood test, ferritin, TSH, glucose, etc.),
                     HRV, bodyweight, sleep hours, energy level (out of workout context), food quality, steps,
                     resting HR, watch data, nutrition logs. The FACT should preserve all numeric values verbatim.
                     Examples: "ferritin: 45 ng/mL, TSH: 2.1", "HRV 58, resting HR 52", "slept 7.5h, energy 8/10"
- PROGRAM_REQUEST  — athlete explicitly asks for a structural program change: new program, new block, deload week,
                     comeback sessions, scaling weights after vacation/illness, next training cycle.
                     This is DIFFERENT from a coaching question — it's a request to modify or create program structure.
                     Examples: "I need a deload week", "design me the next block", "scale back my weights I've been away",
                     "can we do comeback sessions?", "I'm ready for a new program"
- QUESTION         — athlete has a coaching question or wants advice. NOT structural program changes.
                     Examples: "why is my bench stalling?", "should I eat more on training days?",
                     "how is my squat progressing?", "what's my estimated 1RM?"
- NOISE            — chitchat, acknowledgment, emoji-only, irrelevant

OUTPUT FORMAT (one line per extracted fact):
CATEGORY | DATE | FACT

Rules:
- DATE: use the message date if known, otherwise write "unknown"
- FACT: one concise sentence. What happened. No coaching.
- One message can produce multiple lines (e.g. a message about skipping + asking a question = 2 lines)
- NOISE lines are optional — only include them if useful to log
- Do NOT include JSON, markdown, or any other format. Plain lines only.
- Distinguish QUESTION (coaching advice request) from PROGRAM_REQUEST (structural change request)
- Distinguish HEALTH_DATA (numeric metrics, resting state) from MOOD_PERFORMANCE (how training felt in-session)

Examples:
SCHEDULE_CHANGE | 2026-03-07 | Athlete skipped Day 3, no plan to make it up
PENDING_CATCHUP | 2026-03-11 | Week 9 Day 2 → planned for 2026-03-13 (Thursday)
PENDING_CATCHUP | 2026-03-11 | Week 9 Day 3 → planned for Monday (date unknown)
LIFE_EVENT | 2026-03-07 | Athlete traveling Mon-Thu this week, training may be disrupted
PREFERENCE | 2026-03-06 | Athlete says weekly charts are not useful, prefers text only
PREFERENCE | 2026-03-06 | primary_channel: telegram
PREFERENCE | 2026-03-06 | primary_channel: email
WORKOUT_UNPLANNED | 2026-03-05 | Athlete did spontaneous pull day with pull-ups and rows
LIFT_UPDATE | 2026-03-07 | exercise: Squat | weight: 100 | sets_reps: 3x3 | date: 2026-03-07 | rpe: 8
LIFT_UPDATE | 2026-03-07 | exercise: Bench Press | weight: 87.5 | sets_reps: 4x5 | date: 2026-03-07 | rir: 2
MOOD_PERFORMANCE | 2026-03-07 | Squats felt slow and heavy, energy low, stopped at set 3
MOOD_PERFORMANCE | 2026-03-09 | Sharp elbow pain on last bench set, stopped early
MOOD_PERFORMANCE | 2026-03-11 | Best session in weeks, everything moved fast, RPE 8
TRACK_LIFT | 2026-03-07 | Athlete wants to track Romanian Deadlift as a main lift
TRACK_LIFT | 2026-03-07 | Athlete wants to remove Dip from tracked lifts
HEALTH_DATA | 2026-03-07 | ferritin: 45 ng/mL, TSH: 2.1 mU/L, glucose: 95 mg/dL
HEALTH_DATA | 2026-03-07 | HRV: 58ms, resting HR: 52bpm, sleep: 7.5h
HEALTH_DATA | 2026-03-07 | bodyweight: 83.2kg, food quality: 8/10, energy: 7/10
PROGRAM_REQUEST | 2026-03-12 | Athlete wants comeback sessions after 2-week vacation
PROGRAM_REQUEST | 2026-03-14 | Athlete asking for deload week after next heavy week
QUESTION | 2026-03-07 | Athlete asks whether to add calories on training days
QUESTION | 2026-03-10 | Athlete asks why bench press has been stalling for 3 weeks
"""


# ---------------------------------------------------------------------------
# Parse Haiku output
# ---------------------------------------------------------------------------

def _parse_processor_output(output: str) -> list[dict]:
    """
    Parse Haiku's line-by-line output into structured event dicts.
    Returns list of {category, event_date, fact}.
    """
    events = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        category = parts[0].upper()
        event_date = parts[1]
        fact = "|".join(parts[2:]).strip()  # fact may contain | characters

        valid_categories = {
            "SCHEDULE_CHANGE", "PENDING_CATCHUP", "LIFE_EVENT", "PREFERENCE",
            "WORKOUT_UNPLANNED", "LIFT_UPDATE", "MOOD_PERFORMANCE", "TRACK_LIFT",
            "HEALTH_DATA", "PROGRAM_REQUEST", "QUESTION", "NOISE",
        }
        if category not in valid_categories:
            continue
        if not fact:
            continue

        events.append({
            "category": category,
            "event_date": event_date,
            "fact": fact,
        })
    return events


# ---------------------------------------------------------------------------
# Dispatch events to memory tabs
# ---------------------------------------------------------------------------

def _dispatch_events(events: list[dict], dry_run: bool = False) -> int:
    """
    Write extracted facts to the appropriate memory tabs.
    Returns number of events dispatched.
    """
    if not events:
        return 0

    from memory import (
        append_coach_focus,
        append_life_context,
        append_athlete_preference,
    )

    today = str(date.today())
    dispatched = 0

    for e in events:
        cat = e["category"]
        fact = e["fact"]
        event_date = e["event_date"]

        if dry_run:
            print(f"    [DRY RUN] {cat} | {event_date} | {fact}")
            dispatched += 1
            continue

        try:
            if cat == "SCHEDULE_CHANGE":
                # Log as FOLLOWUP so coach checks on next run
                append_coach_focus("FOLLOWUP", fact, last_mentioned=today)
                # Also update WEEKLY_SCHEDULE pattern so Sunday discovery can reference it
                try:
                    from memory import upsert_coach_state
                    upsert_coach_state("WEEKLY_SCHEDULE", f"{fact} — updated {today}", "MEDIUM")
                except Exception:
                    pass
                dispatched += 1

            elif cat == "PENDING_CATCHUP":
                # Athlete plans to do a session on a different day.
                # Stored in Commands as PENDING_CATCHUP so the prompt can display
                # the session as "⏳ catch-up planned" instead of "not done / missed".
                from memory import append_command
                append_command("PENDING_CATCHUP", fact)
                append_coach_focus(
                    "FOLLOWUP",
                    f"[Catch-up planned] {fact}",
                    last_mentioned=today,
                    priority="HIGH",
                )
                dispatched += 1

            elif cat == "LIFE_EVENT":
                # Goes to Life Context (permanent record) + FOLLOWUP in Coach Focus
                append_life_context(fact, event_date if event_date != "unknown" else today)
                append_coach_focus("TRACKING", f"[Life context] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "PREFERENCE":
                # Extract category + preference from fact
                # Heuristic: first word(s) before ":" or whole thing
                pref_category = _infer_preference_category(fact)
                append_athlete_preference(pref_category, fact, source=f"Telegram {today}")
                # If preference describes a recurring weekly schedule, also update WEEKLY_SCHEDULE
                schedule_keywords = ("always", "every week", "typical", "usually", "normally",
                                     "siempre", "semana", "my weeks")
                if any(k in fact.lower() for k in schedule_keywords):
                    try:
                        from memory import upsert_coach_state
                        upsert_coach_state("WEEKLY_SCHEDULE",
                                           f"[Stable pattern] {fact} — confirmed {today}", "HIGH")
                    except Exception:
                        pass
                dispatched += 1

            elif cat == "WORKOUT_UNPLANNED":
                # Flag as LANDMARK — coach evaluates in next analysis pass
                append_coach_focus("LANDMARK", f"[Unplanned session] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "LIFT_UPDATE":
                # Try to parse structured LIFT_UPDATE fact and write to Lift History.
                # Falls back to Coach Focus LANDMARK if fact is unstructured.
                parsed = _parse_lift_update_fact(fact, today)
                if parsed:
                    from memory import append_lift_history
                    append_lift_history([parsed])
                    append_coach_focus(
                        "LANDMARK",
                        f"[Lift logged via Telegram] {parsed['exercise_name']} "
                        f"{parsed.get('actual', '')} on {parsed['date']}",
                        last_mentioned=today,
                    )
                else:
                    append_coach_focus("LANDMARK", f"[Lift update via Telegram] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "TRACK_LIFT":
                # Athlete wants to add/remove a tracked lift.
                # Log as FOLLOWUP (HIGH priority) so coach proposes formally next run
                # via the PENDING_PROPOSAL flow — coach confirms before touching the registry.
                append_coach_focus(
                    "FOLLOWUP",
                    f"[Lift tracking request] {fact}",
                    last_mentioned=today,
                    priority="HIGH",
                )
                dispatched += 1

            elif cat == "HEALTH_DATA":
                # Extract any known standard fields (BW, sleep, food quality, HRV)
                # and store everything as a health log entry with raw data in Notes.
                from memory import append_health_log
                entry = _parse_health_data_fact(fact, event_date if event_date != "unknown" else today)
                append_health_log([entry])
                # Also surface in Coach Focus so the coach notices new data is available
                append_coach_focus(
                    "TRACKING",
                    f"[Health data logged via Telegram] {fact[:100]}",
                    last_mentioned=today,
                )
                dispatched += 1

            elif cat == "MOOD_PERFORMANCE":
                # How a session felt — important context for interpreting lift data.
                # Stored in Coach Focus (TRACKING) + Life Context for long-term reference.
                append_coach_focus(
                    "TRACKING",
                    f"[Session quality] {fact}",
                    last_mentioned=event_date if event_date != "unknown" else today,
                )
                # If pain/injury keywords present, also flag as CONCERN
                pain_keywords = ["pain", "hurt", "ache", "sharp", "swollen", "injury", "stopped early",
                                 "dolor", "lesión", "paré"]
                if any(kw in fact.lower() for kw in pain_keywords):
                    append_coach_focus(
                        "CONCERN",
                        f"[Possible injury/pain] {fact}",
                        last_mentioned=event_date if event_date != "unknown" else today,
                        priority="HIGH",
                    )
                dispatched += 1

            elif cat == "PROGRAM_REQUEST":
                # Structural program change request — log as HIGH-priority FOLLOWUP
                # so the coach sees it in the next email pass and can act on it.
                append_coach_focus(
                    "FOLLOWUP",
                    f"[Program change requested] {fact}",
                    last_mentioned=event_date if event_date != "unknown" else today,
                    priority="HIGH",
                )
                dispatched += 1

            elif cat == "QUESTION":
                # Open question — track as FOLLOWUP so coach addresses it
                append_coach_focus("FOLLOWUP", f"[Athlete question] {fact}", last_mentioned=today)
                dispatched += 1

            elif cat == "NOISE":
                # Skip — not worth logging
                pass

        except Exception as exc:
            print(f"    [Processor] Dispatch failed for {cat}: {exc}")

    return dispatched


def _normalize_date(date_str: str, reference_date: str = None) -> str:
    """
    Normalize a date string to ISO format (YYYY-MM-DD).
    Handles ISO dates, day names, relative expressions ("yesterday", "last Tuesday").
    Falls back to reference_date (today) if parsing fails.
    """
    import re as _re
    from datetime import date as _date, timedelta as _td

    today = _date.fromisoformat(reference_date) if reference_date else _date.today()

    if not date_str or date_str.lower() in ("unknown", "today", ""):
        return str(today)

    # Already ISO
    if _re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str[:10]

    # "yesterday"
    if "yesterday" in date_str.lower():
        return str(today - _td(days=1))

    # Day names: "Monday", "last Tuesday", "el martes", etc.
    day_names = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
    }
    for name, weekday in day_names.items():
        if name in date_str.lower():
            days_back = (today.weekday() - weekday) % 7
            if days_back == 0:
                days_back = 7  # "last Monday" when today is Monday → 7 days ago
            return str(today - _td(days=days_back))

    # "26 march", "march 26", "26/3", "3/26"
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
        "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }
    for month_name, month_num in month_names.items():
        if month_name in date_str.lower():
            day_match = _re.search(r"\b(\d{1,2})\b", date_str)
            if day_match:
                try:
                    from datetime import datetime as _dt
                    year = today.year
                    candidate = _dt(year, month_num, int(day_match.group(1))).date()
                    if candidate > today:
                        candidate = _dt(year - 1, month_num, int(day_match.group(1))).date()
                    return str(candidate)
                except ValueError:
                    pass

    return str(today)  # fallback


def _parse_lift_update_fact(fact: str, today: str) -> dict | None:
    """
    Parse a structured LIFT_UPDATE fact into a Lift History entry dict.
    Expected format: "exercise: <name> | weight: <kg> | sets_reps: <NxN> | date: <date>"
    Returns None if the fact doesn't contain enough data to log.
    """
    import re as _re

    # Parse pipe-separated key:value pairs
    fields = {}
    for part in fact.split("|"):
        kv = part.strip().split(":", 1)
        if len(kv) == 2:
            fields[kv[0].strip().lower()] = kv[1].strip()

    exercise = fields.get("exercise", "").strip()
    weight = fields.get("weight", "").strip()
    sets_reps = fields.get("sets_reps", "").strip()
    date_raw = fields.get("date", "unknown").strip()
    rpe_raw = fields.get("rpe", "").strip()
    rir_raw = fields.get("rir", "").strip()

    # Need at least exercise + weight to be worth logging
    if not exercise or not weight:
        return None

    # Normalize weight (strip "kg", commas)
    weight_clean = _re.sub(r"[^\d.]", "", weight.replace(",", "."))
    if not weight_clean:
        return None

    actual_str = f"{weight_clean}kg"
    if sets_reps:
        actual_str += f" {sets_reps}"

    # Build notes with RPE/RIR if provided
    notes_parts = [f"[Logged via Telegram on {today}]"]
    if rpe_raw:
        rpe_clean = _re.sub(r"[^\d.]", "", rpe_raw)
        if rpe_clean:
            notes_parts.append(f"RPE {rpe_clean}")
            actual_str += f" @RPE{rpe_clean}"
    if rir_raw:
        rir_clean = _re.sub(r"[^\d.]", "", rir_raw)
        if rir_clean:
            notes_parts.append(f"RIR {rir_clean}")

    normalized_date = _normalize_date(date_raw, today)

    return {
        "date": normalized_date,
        "exercise_name": exercise,
        "actual": actual_str,
        "prescribed_weight": weight_clean,
        "sets_reps": sets_reps,
        "completed": True,
        "notes": " | ".join(notes_parts),
        "week": "",
        "day_label": "Telegram",
    }


def _parse_health_data_fact(fact: str, entry_date: str) -> dict:
    """
    Parse a HEALTH_DATA fact string into a health log entry dict.
    Extracts standard fields (BW, sleep, food quality) if present.
    Everything is also stored verbatim in Notes for the HealthAgent to read.

    Known patterns (case-insensitive):
      bodyweight / bw / peso: <number>
      sleep / sueño: <number>h
      food (quality): <number>/10
      energy: <number>/10
      hrv: <number>
      steps: <number>
    """
    import re as _re

    entry = {"date": entry_date, "notes": fact}

    # Bodyweight
    bw_match = _re.search(r"(?:bodyweight|bw|peso)[:\s]+(\d+(?:[.,]\d+)?)", fact, _re.I)
    if bw_match:
        entry["bodyweight"] = bw_match.group(1).replace(",", ".")

    # Sleep
    sleep_match = _re.search(r"(?:sleep|sueño|slept)[:\s]+(\d+(?:[.,]\d+)?)", fact, _re.I)
    if sleep_match:
        entry["sleep"] = sleep_match.group(1).replace(",", ".")

    # Food quality (e.g. "food: 8/10" or "food quality: 7")
    food_match = _re.search(r"(?:food(?:\s+quality)?)[:\s]+(\d+)", fact, _re.I)
    if food_match:
        entry["food_quality"] = food_match.group(1)

    # Energy (stored in notes — no dedicated column, but useful for HealthAgent)
    # Steps
    steps_match = _re.search(r"steps[:\s]+(\d+)", fact, _re.I)
    if steps_match:
        entry["steps"] = steps_match.group(1)

    return entry


def _infer_preference_category(fact: str) -> str:
    """Infer a preference category from the fact text."""
    fact_lower = fact.lower()
    if any(w in fact_lower for w in ["chart", "graph", "visual"]):
        return "OUTPUT_CHARTS"
    if any(w in fact_lower for w in ["email", "length", "long", "short"]):
        return "OUTPUT_EMAIL"
    if any(w in fact_lower for w in ["telegram", "message", "notify"]):
        return "OUTPUT_TELEGRAM"
    if any(w in fact_lower for w in ["topic", "talk about", "mention"]):
        return "OUTPUT_TOPICS"
    return "OUTPUT"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_telegram_messages(dry_run: bool = False) -> int:
    """
    Read unprocessed Telegram messages, classify them with Haiku,
    dispatch extracted facts to memory, and mark messages processed.

    Returns number of messages processed.
    """
    from memory import read_telegram_unprocessed, mark_telegram_processed

    messages = read_telegram_unprocessed(limit=50)
    if not messages:
        return 0

    print(f"  Processing {len(messages)} unprocessed Telegram message(s)...")

    # Build the user message: one message per line with date/direction context
    lines = []
    for m in messages:
        direction = m.get("Direction", "IN")
        if direction != "IN":
            continue  # only process inbound messages from athlete
        msg_date = m.get("Date", "unknown")
        msg_time = m.get("Time", "")
        text = m.get("Message", "").strip()
        if not text:
            continue
        lines.append(f"[{msg_date} {msg_time}] {text}")

    if not lines:
        # All messages were outbound (coach → athlete), still mark as processed
        row_indices = [m.get("_row_index") for m in messages if m.get("_row_index")]
        if row_indices and not dry_run:
            mark_telegram_processed(row_indices)
        return 0

    user_content = "\n".join(lines)

    # Call Haiku
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=PROCESSOR_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        output = response.content[0].text
    except Exception as e:
        print(f"  Telegram processor call failed (non-fatal): {e}")
        return 0

    if dry_run:
        print("\n  --- TELEGRAM PROCESSOR OUTPUT ---")
        print(output)
        print("  --- END PROCESSOR OUTPUT ---\n")

    # Parse and dispatch
    events = _parse_processor_output(output)
    dispatched = _dispatch_events(events, dry_run=dry_run)

    if dispatched > 0:
        print(f"    → {dispatched} fact(s) dispatched to memory")

    # Mark all messages as processed (regardless of direction)
    if not dry_run:
        row_indices = [m.get("_row_index") for m in messages if m.get("_row_index")]
        if row_indices:
            mark_telegram_processed(row_indices)
            print(f"    → {len(row_indices)} message(s) marked processed")

    return len(messages)


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    dry = "--dry-run" in sys.argv

    print("Running Telegram processor...")
    count = process_telegram_messages(dry_run=dry)
    print(f"Done. Processed {count} message(s).")
