"""
MockMemory — drop-in replacement for memory.py read/write operations.

Backed by an in-memory dict. Mirrors the production data model exactly:

  Production stores all Coach State domains as rows in a Google Sheet:
    columns: Domain | Summary (JSON string) | Confidence | Last Updated

  read_coach_state() returns: {domain: {"summary": str, "confidence": str, "last_updated": str}}
  upsert_coach_state(domain, summary_string, confidence) stores the envelope.

  read_summary_list / append_summary / write_single_summary / read_single_summary
  all go through this envelope format, same as production.

This class stores coach_state values in the same envelope format so that
all production code paths work without modification.
"""
import copy
import json

_SUMMARY_MAX_KEEP = {
    "WEEKLY_SUMMARIES": 52,
    "DAILY_SUMMARIES": 14,
    "MONTHLY_SUMMARIES": 24,
}

_DEFAULT_DATE = "2026-03-23"


def _to_envelope(value, confidence="MEDIUM") -> dict:
    """
    Convert a fixture initial_state value to production envelope format.
    Production always stores: {"summary": str, "confidence": str, "last_updated": str}
    """
    if isinstance(value, dict) and "summary" in value:
        # Already in envelope format
        return value
    if value is None:
        summary_str = ""
    elif isinstance(value, str):
        summary_str = value
    else:
        # dict or list → JSON-serialize (matches how production writes complex domains)
        summary_str = json.dumps(value)
    return {"summary": summary_str, "confidence": confidence, "last_updated": _DEFAULT_DATE}


class MockMemory:
    def __init__(self, initial_state: dict):
        """
        initial_state: fixture dict containing 'coach_state', 'lift_history', etc.
        coach_state values may be raw strings, dicts, lists, or None — all are
        normalized to production envelope format on init.
        """
        raw_cs = initial_state.get("coach_state", {})
        normalized_cs = {
            domain: _to_envelope(value)
            for domain, value in raw_cs.items()
        }
        self.store = copy.deepcopy(initial_state)
        self.store["coach_state"] = normalized_cs
        self.mutation_log = []  # list of {domain, before, after}

    # -------------------------------------------------------------------------
    # Core Coach State reads
    # -------------------------------------------------------------------------

    def read_coach_state(self) -> dict:
        """Returns production-format envelope dict: {domain: {"summary": str, ...}}"""
        return copy.deepcopy(self.store.get("coach_state", {}))

    def get_domain(self, domain: str):
        """
        Returns the parsed Python value (dict/list/str) for a domain.
        Used by engine assertions and _get_nested() for dot-notation access.
        Parses JSON summary if possible; returns raw string otherwise.
        """
        envelope = self.store.get("coach_state", {}).get(domain)
        if envelope is None:
            return None
        summary = envelope.get("summary", "") if isinstance(envelope, dict) else str(envelope)
        if not summary:
            return None
        try:
            return json.loads(summary)
        except (json.JSONDecodeError, TypeError):
            return summary

    def read_lift_history(self, limit=200, after_date=None):
        rows = self.store.get("lift_history", [])
        if after_date:
            rows = [r for r in rows if r.get("Date", "") >= after_date]
        return copy.deepcopy(rows[-limit:])

    def read_health_log(self, limit=100, after_date=None):
        rows = self.store.get("health_log", [])
        if after_date:
            rows = [r for r in rows if r.get("Date", "") >= after_date]
        return copy.deepcopy(rows[-limit:])

    def read_telegram_log(self, limit=20):
        rows = self.store.get("telegram_log", [])
        return copy.deepcopy(rows[-limit:])

    def read_telegram_log_since(self, since_date, limit=50):
        rows = self.store.get("telegram_log", [])
        rows = [r for r in rows if r.get("Date", "") >= since_date]
        return copy.deepcopy(rows[-limit:])

    def read_athlete_profile(self) -> str:
        return self.store.get("athlete_profile", "Nacho, Spain. Strength training. Week 7.")

    def read_long_term_goals(self) -> str:
        return self.store.get("long_term_goals", "120kg squat, 105kg bench by Week 30.")

    def read_coach_focus(self, status_filter=None):
        items = self.store.get("coach_focus", [])
        if status_filter:
            items = [i for i in items if i.get("Status") == status_filter]
        return copy.deepcopy(items)

    def read_commitments(self, status_filter=None):
        items = self.store.get("commitments", [])
        if status_filter:
            items = [i for i in items if i.get("Status") == status_filter]
        return copy.deepcopy(items)

    def read_tracked_lifts(self, active_only=True):
        lifts = self.store.get("tracked_lifts", [])
        if active_only:
            lifts = [l for l in lifts if l.get("Active", "Y") == "Y"]
        return copy.deepcopy(lifts)

    def read_athlete_preferences(self):
        return copy.deepcopy(self.store.get("athlete_preferences", []))

    def read_commands(self):
        return copy.deepcopy(self.store.get("commands", []))

    def read_strategic_plan(self):
        return copy.deepcopy(self.store.get("strategic_plan", []))

    def read_planning_notes(self):
        return copy.deepcopy(self.store.get("planning_notes", []))

    def read_summary_list(self, domain: str, limit: int = 8) -> list:
        """Mirrors production: reads summary JSON string, parses as list."""
        cs = self.read_coach_state()
        raw = cs.get(domain.upper(), {}).get("summary", "")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data[-limit:]
            if isinstance(data, dict):
                return [data]
        except Exception:
            pass
        return []

    def read_single_summary(self, domain: str):
        """Mirrors production: reads summary JSON string, parses as dict."""
        cs = self.read_coach_state()
        raw = cs.get(domain.upper(), {}).get("summary", "")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    # -------------------------------------------------------------------------
    # Core Coach State writes
    # -------------------------------------------------------------------------

    def upsert_coach_state(self, domain: str, summary, confidence="MEDIUM"):
        """
        Mirrors production: summary is a string (typically JSON-serialized).
        Stores in envelope format: {"summary": str, "confidence": str, "last_updated": str}
        """
        before = self.get_domain(domain)
        envelope = {"summary": summary if isinstance(summary, str) else json.dumps(summary),
                    "confidence": confidence,
                    "last_updated": _DEFAULT_DATE}
        if "coach_state" not in self.store:
            self.store["coach_state"] = {}
        self.store["coach_state"][domain] = envelope
        after = self.get_domain(domain)
        self.mutation_log.append({
            "domain": domain,
            "before": before,
            "after": copy.deepcopy(after)
        })

    def append_summary(self, domain: str, summary_dict: dict, max_keep: int = None):
        """Mirrors production: reads list, appends, JSON-serializes, upserts."""
        if max_keep is None:
            max_keep = _SUMMARY_MAX_KEEP.get(domain.upper(), 8)
        existing = self.read_summary_list(domain, limit=max_keep)
        existing.append(summary_dict)
        if len(existing) > max_keep:
            existing = existing[-max_keep:]
        self.upsert_coach_state(domain.upper(), json.dumps(existing), "HIGH")

    def write_single_summary(self, domain: str, summary_dict: dict):
        """Mirrors production: JSON-serializes dict, upserts."""
        self.upsert_coach_state(domain.upper(), json.dumps(summary_dict), "HIGH")

    def append_lift_history(self, sessions: list):
        if "lift_history" not in self.store:
            self.store["lift_history"] = []
        self.store["lift_history"].extend(sessions)

    def upsert_lift_history(self, sessions: list):
        self.append_lift_history(sessions)

    def append_health_log(self, entries: list):
        if "health_log" not in self.store:
            self.store["health_log"] = []
        self.store["health_log"].extend(entries)

    def upsert_health_log_row(self, date_str: str, updates: dict):
        if "health_log" not in self.store:
            self.store["health_log"] = []
        for row in self.store["health_log"]:
            if row.get("Date") == date_str:
                row.update(updates)
                return
        self.store["health_log"].append({"Date": date_str, **updates})

    def append_telegram_log(self, direction: str, message: str, log_date=None):
        if "telegram_log" not in self.store:
            self.store["telegram_log"] = []
        self.store["telegram_log"].append({
            "Direction": direction,
            "Message": message,
            "Date": str(log_date) if log_date else _DEFAULT_DATE,
            "Processed": "N"
        })

    def append_life_context(self, context_note: str, context_date=None):
        if "life_context" not in self.store:
            self.store["life_context"] = []
        self.store["life_context"].append({
            "date": str(context_date) if context_date else _DEFAULT_DATE,
            "context": context_note
        })

    def append_coach_focus(self, category: str, item: str, last_mentioned=None, priority: str = "MEDIUM"):
        if "coach_focus" not in self.store:
            self.store["coach_focus"] = []
        self.store["coach_focus"].append({
            "Category": category,
            "Item": item,
            "Status": "OPEN",
            "Priority": priority
        })

    def log_coach_run(self, observations: str, email_summary: str, run_date=None, cost_usd: float = 0.0):
        if "coach_log" not in self.store:
            self.store["coach_log"] = []
        self.store["coach_log"].append({
            "Date": str(run_date) if run_date else _DEFAULT_DATE,
            "Observations": observations,
            "Email": email_summary,
            "Cost": cost_usd
        })

    # -------------------------------------------------------------------------
    # Inspection helpers
    # -------------------------------------------------------------------------

    def snapshot(self) -> dict:
        return copy.deepcopy(self.store)

    def get_mutations_for(self, domain: str) -> list:
        return [m for m in self.mutation_log if m["domain"] == domain]

    def was_mutated(self, domain: str) -> bool:
        return any(m["domain"] == domain for m in self.mutation_log)

    def reset_mutation_log(self):
        self.mutation_log = []
