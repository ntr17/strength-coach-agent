"""
SheetSyncEngine — delta-based sync from sheet to Coach State.

Compares current sheet state against a stored watermark to detect:
  - Sessions marked Done (program sheet Week tab)
  - New health log entries (Health Log tab)
  - New lift history rows (Lift History tab)

Dispatches changes deterministically (zero LLM calls):
  - session_done   → resolve matching PENDING_CATCHUP + update SCHEDULE domain
  - new_health_row → update HEALTH Coach State domain
  - new_lift_row   → update relevant lift domain (SQUAT, BENCH, etc.)

Watermark is stored in Coach State domain "SHEET_SYNC" as JSON.

First run: saves baseline state, emits no events (avoids false positives).
Week advance: resets done_per_day for the new week.

Called at start of each pipeline run (proactive, brief, post-session, evening-protocol)
before cascade builds so the cascade always sees fresh Coach State.

Fully non-fatal at the call site — wrapped in try/except there.
"""

import json
import re
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))


class SheetSyncEngine:
    """
    Detects deltas between current sheet state and stored watermark,
    then dispatches those changes to Coach State / Commands.
    """

    WATERMARK_DOMAIN = "SHEET_SYNC"

    # ---------------------------------------------------------------------------
    # Watermark I/O
    # ---------------------------------------------------------------------------

    def load_watermark(self) -> dict | None:
        """Load watermark from Coach State SHEET_SYNC domain. Returns None if absent."""
        try:
            from memory import read_coach_state
            state = read_coach_state()
            entry = state.get(self.WATERMARK_DOMAIN)
            if not entry:
                return None
            raw = entry.get("summary", "")
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            print(f"  [SheetSync] Watermark load failed: {e}")
            return None

    def save_watermark(self, data: dict) -> None:
        """Persist watermark to Coach State SHEET_SYNC domain."""
        try:
            from memory import upsert_coach_state
            upsert_coach_state(self.WATERMARK_DOMAIN, json.dumps(data), "HIGH")
        except Exception as e:
            print(f"  [SheetSync] Watermark save failed: {e}")

    # ---------------------------------------------------------------------------
    # Snapshot helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _done_per_day(current_week_days: list) -> list:
        """Return [True/False, ...] per day — True if any exercise Done=True."""
        return [
            any(ex.get("done") is True for ex in day.get("exercises", []))
            for day in current_week_days
        ]

    # ---------------------------------------------------------------------------
    # Delta detection
    # ---------------------------------------------------------------------------

    def detect_deltas(
        self,
        week_num: int,
        current_week_days: list,
        health_log: list,
        lift_history: list,
    ) -> list[dict]:
        """
        Compare current sheet state against stored watermark.
        Returns list of delta events. First run saves baseline and returns [].
        """
        today_str = str(date.today())
        current_done = self._done_per_day(current_week_days)
        current_health_rows = len(health_log)
        current_lift_rows = len(lift_history)

        watermark = self.load_watermark()

        # First run — save baseline, no events
        if watermark is None:
            self.save_watermark({
                "week": week_num,
                "done_per_day": current_done,
                "lift_history_rows": current_lift_rows,
                "health_log_rows": current_health_rows,
                "last_sync": today_str,
            })
            print(
                f"  [SheetSync] No watermark — saved baseline "
                f"(Week {week_num}, {current_lift_rows} lifts, {current_health_rows} health rows)"
            )
            return []

        events = []

        # Week boundary — reset done tracking for new week
        wm_week = watermark.get("week", week_num)
        if wm_week != week_num:
            print(f"  [SheetSync] Week advanced {wm_week} → {week_num}, resetting done_per_day")
            watermark["done_per_day"] = [False] * len(current_week_days)
            watermark["week"] = week_num

        # --- session_done events ---
        prev_done = watermark.get("done_per_day", [])
        for i, is_done_now in enumerate(current_done):
            was_done = prev_done[i] if i < len(prev_done) else False
            if is_done_now and not was_done:
                label = (
                    current_week_days[i].get("label", f"Day {i + 1}")
                    if i < len(current_week_days)
                    else f"Day {i + 1}"
                )
                events.append({
                    "type": "session_done",
                    "day_index": i,
                    "day_number": i + 1,
                    "label": label,
                    "date": today_str,
                })
                print(f"  [SheetSync] Delta: session_done — {label}")

        # --- new_health_row events (health_log is newest-first after read_health_log) ---
        prev_health_rows = watermark.get("health_log_rows", 0)
        if current_health_rows > prev_health_rows:
            new_count = current_health_rows - prev_health_rows
            # health_log is ordered newest-first (read_health_log reverses); new entries are first
            for entry in health_log[:new_count]:
                events.append({"type": "new_health_row", "entry": entry})
                print(f"  [SheetSync] Delta: new_health_row — {entry.get('Date', '?')}")

        # --- new_lift_row events ---
        prev_lift_rows = watermark.get("lift_history_rows", 0)
        if current_lift_rows > prev_lift_rows:
            new_count = current_lift_rows - prev_lift_rows
            for entry in lift_history[:new_count]:
                print(
                    f"  [SheetSync] Delta: new_lift_row — "
                    f"{entry.get('exercise_name', entry.get('Exercise', '?'))} "
                    f"{entry.get('date', entry.get('Date', '?'))}"
                )
                events.append({"type": "new_lift_row", "entry": entry})

        # Advance watermark
        self.save_watermark({
            "week": week_num,
            "done_per_day": current_done,
            "lift_history_rows": current_lift_rows,
            "health_log_rows": current_health_rows,
            "last_sync": today_str,
        })

        return events

    # ---------------------------------------------------------------------------
    # Dispatch
    # ---------------------------------------------------------------------------

    def dispatch(self, events: list[dict], commands: list) -> dict:
        """
        Deterministic dispatch — zero LLM calls.
        session_done   → resolve matching PENDING_CATCHUP + update SCHEDULE domain
        new_health_row → update HEALTH Coach State domain
        new_lift_row   → update relevant lift domain via tracked_lifts mapping
        Returns: {"resolved_catchups": N, "updated_domains": [...]}
        """
        if not events:
            return {"resolved_catchups": 0, "updated_domains": []}

        from memory import upsert_coach_state, mark_command_applied
        today_str = str(date.today())

        resolved_catchups = 0
        updated_domains = []

        # Open PENDING_CATCHUPs available for session_done resolution
        open_catchups = [
            c for c in commands
            if c.get("Command", "").upper() == "PENDING_CATCHUP"
            and c.get("Applied", "").upper() not in ("Y", "DECLINED")
        ]

        # Tracked lifts domain map: match_pattern (lower) → domain (upper)
        lift_domain_map: dict = {}
        try:
            from memory import read_tracked_lifts
            for tl in read_tracked_lifts(active_only=True):
                pattern = tl.get("match_pattern", "").lower()
                domain = tl.get("domain", "").upper()
                if pattern and domain:
                    lift_domain_map[pattern] = domain
        except Exception:
            pass

        for event in events:
            etype = event.get("type")

            # ------------------------------------------------------------------
            if etype == "session_done":
                day_num = event["day_number"]
                label = event["label"]
                event_date = event["date"]

                # Resolve any open PENDING_CATCHUP that references this Day N
                for cmd in open_catchups:
                    m = re.search(r"Day\s+(\d+)", cmd.get("Value", ""), re.I)
                    if m and int(m.group(1)) == day_num:
                        row_idx = cmd.get("_row_index")
                        if row_idx:
                            try:
                                mark_command_applied(row_idx)
                                resolved_catchups += 1
                                print(
                                    f"  [SheetSync] Resolved PENDING_CATCHUP: "
                                    f"{cmd.get('Value', '')[:60]}"
                                )
                            except Exception as e:
                                print(f"  [SheetSync] Failed to resolve catchup: {e}")

                # Update SCHEDULE domain
                upsert_coach_state(
                    "SCHEDULE",
                    f"[sync {event_date}] Session completed: {label}",
                    "HIGH",
                )
                updated_domains.append("SCHEDULE")

            # ------------------------------------------------------------------
            elif etype == "new_health_row":
                entry = event["entry"]
                bw = entry.get("Bodyweight (kg)", "")
                sleep = entry.get("Sleep (hrs)", "")
                food = entry.get("Food Quality (1-10)", "")
                hrv = entry.get("HRV (ms)", "")
                entry_date = entry.get("Date", today_str)

                parts = []
                if bw:
                    parts.append(f"BW {bw}kg")
                if sleep:
                    parts.append(f"sleep {sleep}h")
                if food:
                    parts.append(f"food {food}/10")
                if hrv:
                    parts.append(f"HRV {hrv}ms")

                if parts:
                    upsert_coach_state(
                        "HEALTH",
                        f"[sync {entry_date}] " + ", ".join(parts),
                        "HIGH",
                    )
                    updated_domains.append("HEALTH")
                    print(f"  [SheetSync] Updated HEALTH domain: {', '.join(parts)}")

            # ------------------------------------------------------------------
            elif etype == "new_lift_row":
                entry = event["entry"]
                exercise = entry.get("exercise_name") or entry.get("Exercise", "")
                actual = entry.get("actual") or entry.get("Actual", "")
                lift_date = entry.get("date") or entry.get("Date", today_str)

                # Match exercise name against tracked_lifts to find domain
                domain = None
                exercise_lower = exercise.lower()
                for pattern, dom in lift_domain_map.items():
                    if pattern in exercise_lower:
                        domain = dom
                        break

                if domain:
                    upsert_coach_state(
                        domain,
                        f"[sync {lift_date}] Last logged: {exercise} {actual}",
                        "HIGH",
                    )
                    updated_domains.append(domain)
                    print(f"  [SheetSync] Updated {domain} domain: {exercise} {actual}")

        return {
            "resolved_catchups": resolved_catchups,
            "updated_domains": list(set(updated_domains)),
        }

    # ---------------------------------------------------------------------------
    # Main entry point
    # ---------------------------------------------------------------------------

    def run_sync(
        self,
        week_num: int,
        current_week_days: list,
        health_log: list,
        lift_history: list,
        commands: list,
        dry_run: bool = False,
    ) -> dict:
        """
        Detect deltas and dispatch. Always non-fatal.
        Returns: {"resolved_catchups": N, "updated_domains": [...], "events": N}
        """
        try:
            events = self.detect_deltas(
                week_num, current_week_days, health_log, lift_history
            )

            if not events:
                return {"resolved_catchups": 0, "updated_domains": [], "events": 0}

            if dry_run:
                print(f"  [SheetSync DRY RUN] {len(events)} delta(s) — not dispatching:")
                for e in events:
                    label = e.get("label") or e.get("entry", {}).get("Date", "?")
                    print(f"    {e['type']}: {label}")
                return {"resolved_catchups": 0, "updated_domains": [], "events": len(events)}

            result = self.dispatch(events, commands)
            result["events"] = len(events)
            return result

        except Exception as e:
            print(f"  [SheetSync] run_sync failed (non-fatal): {e}")
            return {"resolved_catchups": 0, "updated_domains": [], "events": 0}
