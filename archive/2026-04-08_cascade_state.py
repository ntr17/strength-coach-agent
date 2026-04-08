"""
cascade_state.py — V17 Cascade State Machine

State machine per cascade level, snapshot create/restore/revert,
and Telegram thread priority queue.

State machine states per level:
  IDLE         — waiting for trigger
  GATHERING    — reading parent output + own summaries
  REASONING    — model thinking (no human needed yet)
  AWAITING_USER— sent Telegram message, waiting for reply
  COMMITTING   — writing outputs to Coach State / memory
  LOCKED       — higher-level change pending; provisional mode only

Cascade levels (top-down planning order):
  LONGTERM → ANNUAL → MONTHLY → WEEKLY → DAILY

Propagation rule: if MONTHLY is AWAITING_USER → WEEKLY goes LOCKED.

Snapshot: pre-escalation state stored as JSON in SNAPSHOT_LOG Coach State domain.
Debounce window: 15 minutes. Free rollback within window. After window: snapshot committed,
reversion requires a new inverse cascade.
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LEVELS = ["LONGTERM", "ANNUAL", "MONTHLY", "WEEKLY", "DAILY"]
STATES = ["IDLE", "GATHERING", "REASONING", "AWAITING_USER", "COMMITTING", "LOCKED"]

# Propagation map: if this level is AWAITING_USER, which lower levels get LOCKED?
_LOCK_PROPAGATION: dict[str, list[str]] = {
    "LONGTERM": ["ANNUAL", "MONTHLY", "WEEKLY", "DAILY"],
    "ANNUAL": ["MONTHLY", "WEEKLY", "DAILY"],
    "MONTHLY": ["WEEKLY", "DAILY"],
    "WEEKLY": ["DAILY"],
    "DAILY": [],
}

# Thread priority queue — lower number = higher priority
THREAD_PRIORITIES: dict[str, int] = {
    "GOAL_CHANGE": 1,
    "INJURY": 1,
    "MONTHLY_CONFIRM": 2,
    "ANNUAL_CONFIRM": 2,
    "WEEKLY_CONFIRM": 2,
    "DAILY_MORNING": 3,
    "CLOSE_SESSION": 4,  # never blocked
    "ENDSESSION": 4,
}

# Debounce window for free snapshot rollback (seconds)
SNAPSHOT_DEBOUNCE_SECONDS = 15 * 60  # 15 minutes

# Max snapshots kept in SNAPSHOT_LOG
MAX_SNAPSHOTS = 10


# ---------------------------------------------------------------------------
# Cascade state read / write
# ---------------------------------------------------------------------------

def read_cascade_state() -> dict:
    """
    Read CASCADE_STATE domain from Coach State.

    Returns dict keyed by level:
    {
      "DAILY": {"state": "IDLE", "locked_by": None, "awaiting_message_id": None,
                "last_updated": "2026-03-21T19:00:00Z", "context": {}},
      ...
    }
    """
    try:
        from memory import read_coach_state
        cs = read_coach_state()
        raw = cs.get("CASCADE_STATE", {}).get("summary", "")
        if not raw:
            return _default_cascade_state()
        data = json.loads(raw)
        # Ensure all levels present
        defaults = _default_cascade_state()
        for level in LEVELS:
            if level not in data:
                data[level] = defaults[level]
        return data
    except Exception:
        return _default_cascade_state()


def write_cascade_state(cascade: dict) -> None:
    """Write the full cascade state dict back to CASCADE_STATE Coach State domain."""
    try:
        from memory import upsert_coach_state
        upsert_coach_state("CASCADE_STATE", json.dumps(cascade, ensure_ascii=False), "HIGH")
    except Exception:
        pass


def set_level_state(
    level: str,
    state: str,
    awaiting_message_id: Optional[int] = None,
    locked_by: Optional[str] = None,
    context: Optional[dict] = None,
) -> None:
    """
    Update the state machine entry for a single cascade level.
    Also propagates LOCKED to lower levels if state == AWAITING_USER.
    """
    if level not in LEVELS:
        raise ValueError(f"Unknown cascade level: {level}")
    if state not in STATES:
        raise ValueError(f"Unknown state: {state}")

    cascade = read_cascade_state()
    now = datetime.now(timezone.utc).isoformat()

    cascade[level]["state"] = state
    cascade[level]["last_updated"] = now
    if awaiting_message_id is not None:
        cascade[level]["awaiting_message_id"] = awaiting_message_id
    if locked_by is not None:
        cascade[level]["locked_by"] = locked_by
    if context is not None:
        cascade[level]["context"] = context

    # Propagate LOCKED to lower levels when this level becomes AWAITING_USER
    if state == "AWAITING_USER":
        for lower in _LOCK_PROPAGATION.get(level, []):
            if cascade[lower]["state"] not in ("AWAITING_USER", "COMMITTING"):
                cascade[lower]["state"] = "LOCKED"
                cascade[lower]["locked_by"] = level
                cascade[lower]["last_updated"] = now

    # Release LOCKED from lower levels when this level is done (IDLE or COMMITTING)
    if state in ("IDLE", "COMMITTING"):
        for lower in _LOCK_PROPAGATION.get(level, []):
            if cascade[lower].get("locked_by") == level:
                cascade[lower]["state"] = "IDLE"
                cascade[lower]["locked_by"] = None
                cascade[lower]["last_updated"] = now

    write_cascade_state(cascade)


def get_level_state(level: str) -> str:
    """Return the current state string for a given level."""
    return read_cascade_state().get(level, {}).get("state", "IDLE")


def is_level_locked(level: str) -> bool:
    """Return True if the given level is LOCKED by a higher level."""
    return get_level_state(level) == "LOCKED"


# ---------------------------------------------------------------------------
# Snapshot system
# ---------------------------------------------------------------------------

def create_snapshot(affected_levels: list[str], reason: str) -> str:
    """
    Create a snapshot of current cascade state + relevant Coach State domains.
    Stores in SNAPSHOT_LOG Coach State domain. Returns snapshot_id.

    Called before any escalation-triggered re-planning.
    """
    snapshot_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc)

    # Read current cascade state
    cascade = read_cascade_state()

    # Read relevant Coach State domains for snapshot
    coach_state_snapshot: dict = {}
    try:
        from memory import read_coach_state
        cs = read_coach_state()
        snapshot_domains = [
            "GOLDEN_RULES", "CASCADE_STATE", "WEEKLY_SUMMARIES", "MONTHLY_SUMMARIES",
            "ANNUAL_SUMMARY", "LONGTERM_PLAN", "DAILY_SUMMARY", "HEALTH_READINESS",
            "WEEKLY_SCHEDULE", "TOMORROW_PLAN", "WEEKLY_INTENT",
        ]
        for domain in snapshot_domains:
            val = cs.get(domain, {}).get("summary", "")
            if val:
                coach_state_snapshot[domain] = val
    except Exception:
        pass

    snapshot = {
        "snapshot_id": snapshot_id,
        "created_at": now.isoformat(),
        "reason": reason,
        "affected_levels": affected_levels,
        "cascade_state": {lvl: cascade.get(lvl, {}) for lvl in affected_levels},
        "coach_state": coach_state_snapshot,
    }

    # Read existing snapshot log
    existing = _read_snapshot_log()
    existing.append(snapshot)

    # Keep only last MAX_SNAPSHOTS
    if len(existing) > MAX_SNAPSHOTS:
        existing = existing[-MAX_SNAPSHOTS:]

    _write_snapshot_log(existing)
    print(f"  [cascade] Snapshot {snapshot_id} created — reason: {reason}")
    return snapshot_id


def get_snapshot(snapshot_id: str) -> Optional[dict]:
    """Return snapshot by ID, or None if not found."""
    for snap in _read_snapshot_log():
        if snap.get("snapshot_id") == snapshot_id:
            return snap
    return None


def restore_snapshot(snapshot_id: str) -> bool:
    """
    Restore cascade state from snapshot.

    Within 15-minute debounce window: free rollback — restores cascade state directly.
    After debounce window: returns False (caller must initiate inverse cascade).

    Returns True if restore was applied, False if debounce expired.
    """
    snap = get_snapshot(snapshot_id)
    if not snap:
        print(f"  [cascade] Snapshot {snapshot_id} not found.")
        return False

    created_at = datetime.fromisoformat(snap["created_at"])
    now = datetime.now(timezone.utc)
    if (now - created_at).total_seconds() > SNAPSHOT_DEBOUNCE_SECONDS:
        print(f"  [cascade] Snapshot {snapshot_id} debounce expired — inverse cascade required.")
        return False

    # Restore cascade state for affected levels
    cascade = read_cascade_state()
    for level, level_state in snap["cascade_state"].items():
        if level in LEVELS:
            cascade[level] = level_state
    write_cascade_state(cascade)

    # Restore Coach State domains
    try:
        from memory import upsert_coach_state
        for domain, value in snap["coach_state"].items():
            upsert_coach_state(domain, value, "HIGH")
    except Exception:
        pass

    print(f"  [cascade] Snapshot {snapshot_id} restored successfully.")
    return True


def latest_snapshot() -> Optional[dict]:
    """Return the most recent snapshot, or None."""
    log = _read_snapshot_log()
    return log[-1] if log else None


# ---------------------------------------------------------------------------
# Telegram thread priority queue
# ---------------------------------------------------------------------------

def push_thread(
    thread_type: str,
    message_id: int,
    context: Optional[dict] = None,
) -> None:
    """
    Add a new thread to the active thread priority queue.
    thread_type must be a key in THREAD_PRIORITIES.
    """
    threads = _read_active_threads()
    now = datetime.now(timezone.utc).isoformat()
    priority = THREAD_PRIORITIES.get(thread_type, 99)
    threads.append({
        "thread_type": thread_type,
        "message_id": message_id,
        "context": context or {},
        "priority": priority,
        "created_at": now,
        "resolved": False,
    })
    _write_active_threads(threads)
    print(f"  [cascade] Thread pushed: {thread_type} (msg_id={message_id}, priority={priority})")


def resolve_thread(message_id: int) -> None:
    """Mark a thread as resolved by its Telegram message ID."""
    threads = _read_active_threads()
    for t in threads:
        if t.get("message_id") == message_id:
            t["resolved"] = True
    _write_active_threads(threads)


def get_active_threads() -> list[dict]:
    """Return all unresolved threads sorted by priority (ascending = highest priority first)."""
    threads = _read_active_threads()
    active = [t for t in threads if not t.get("resolved", False)]
    return sorted(active, key=lambda t: (t.get("priority", 99), t.get("created_at", "")))


def get_highest_priority_thread() -> Optional[dict]:
    """Return the highest-priority unresolved thread, or None."""
    active = get_active_threads()
    return active[0] if active else None


def get_thread_by_message_id(message_id: int) -> Optional[dict]:
    """Return a thread by Telegram message_id, or None if not found / already resolved."""
    for t in _read_active_threads():
        if t.get("message_id") == message_id and not t.get("resolved", False):
            return t
    return None


def has_active_thread_of_type(thread_type: str) -> bool:
    """Return True if there's an unresolved thread of the given type."""
    return any(t["thread_type"] == thread_type for t in get_active_threads())


def prune_old_threads(max_age_hours: int = 48) -> None:
    """Remove threads (resolved or not) older than max_age_hours."""
    threads = _read_active_threads()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    kept = []
    for t in threads:
        try:
            created = datetime.fromisoformat(t.get("created_at", ""))
            if created > cutoff:
                kept.append(t)
        except Exception:
            kept.append(t)  # keep if date unparseable
    _write_active_threads(kept)


# ---------------------------------------------------------------------------
# Escalation router
# ---------------------------------------------------------------------------

def classify_disruption(disruption_type: str, severity: str = "normal") -> str:
    """
    Return which cascade level this disruption should escalate to.

    disruption_type: one of the disruption keys in the escalation table.
    Returns level name: "DAILY", "WEEKLY", "MONTHLY", "ANNUAL", "LONGTERM".
    """
    table = {
        "single_session_skipped": "WEEKLY",
        "multiple_sessions_skipped": "MONTHLY",  # 3+ in a week
        "injury": "ANNUAL",
        "goal_change": "LONGTERM",
        "medical_event": "LONGTERM",
        "work_conflict_short": "MONTHLY",   # <1 week
        "work_conflict_long": "ANNUAL",     # >1 week
        "extended_vacation": "ANNUAL",
    }
    level = table.get(disruption_type, "WEEKLY")
    print(f"  [cascade] Disruption '{disruption_type}' → escalates to {level}")
    return level


def initiate_escalation(disruption_type: str, context: dict) -> str:
    """
    Full escalation entry point:
    1. Classify disruption
    2. Create pre-escalation snapshot
    3. Set affected levels to LOCKED
    4. Return (snapshot_id, target_level) for caller to re-plan from

    Returns snapshot_id.
    """
    target_level = classify_disruption(disruption_type)
    target_idx = LEVELS.index(target_level)
    affected_levels = LEVELS[target_idx:]  # target + everything below

    reason = f"{disruption_type} — context: {json.dumps(context, ensure_ascii=False)[:200]}"
    snapshot_id = create_snapshot(affected_levels, reason)

    # Lock all levels from target downward (they'll be re-planned)
    cascade = read_cascade_state()
    now = datetime.now(timezone.utc).isoformat()
    for level in affected_levels:
        cascade[level]["state"] = "LOCKED"
        cascade[level]["locked_by"] = f"ESCALATION:{disruption_type}"
        cascade[level]["last_updated"] = now
    write_cascade_state(cascade)

    print(f"  [cascade] Escalation initiated: {disruption_type} → {target_level} (snapshot={snapshot_id})")
    return snapshot_id


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _default_cascade_state() -> dict:
    """Return default IDLE state for all levels."""
    return {
        level: {
            "state": "IDLE",
            "awaiting_message_id": None,
            "locked_by": None,
            "last_updated": None,
            "context": {},
        }
        for level in LEVELS
    }


def _read_snapshot_log() -> list:
    """Read SNAPSHOT_LOG from Coach State, return list of snapshots."""
    try:
        from memory import read_coach_state
        cs = read_coach_state()
        raw = cs.get("SNAPSHOT_LOG", {}).get("summary", "")
        if not raw:
            return []
        return json.loads(raw)
    except Exception:
        return []


def _write_snapshot_log(snapshots: list) -> None:
    """Write snapshot list to SNAPSHOT_LOG Coach State domain."""
    try:
        from memory import upsert_coach_state
        upsert_coach_state("SNAPSHOT_LOG", json.dumps(snapshots, ensure_ascii=False), "HIGH")
    except Exception:
        pass


def _read_active_threads() -> list:
    """Read ACTIVE_THREADS from Coach State, return list of thread dicts."""
    try:
        from memory import read_coach_state
        cs = read_coach_state()
        raw = cs.get("ACTIVE_THREADS", {}).get("summary", "")
        if not raw:
            return []
        return json.loads(raw)
    except Exception:
        return []


def _write_active_threads(threads: list) -> None:
    """Write thread list to ACTIVE_THREADS Coach State domain."""
    try:
        from memory import upsert_coach_state
        upsert_coach_state("ACTIVE_THREADS", json.dumps(threads, ensure_ascii=False), "HIGH")
    except Exception:
        pass
