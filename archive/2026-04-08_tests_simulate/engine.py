"""
CoachSimulator — executes cascade events against mock memory and LLM.

Design:
- Each step() processes one event (close_day, telegram_message, weekly_eval, etc.)
- Before/after state snapshots are recorded for every step
- assert_state() and assert_no_change() accumulate flaws without raising
- report() returns verdict + full flaw list

Injection strategy: monkeypatching (see runner.py).
Production code is NOT modified — mocks are injected via unittest.mock.patch at test time.
"""
import sys
import os
from typing import Any

# Make src/ importable
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")
sys.path.insert(0, os.path.abspath(SRC_DIR))


class CoachSimulator:
    def __init__(self, fixture: dict, memory: "MockMemory", llm: "MockLLM"):
        self.fixture = fixture
        self.memory = memory
        self.llm = llm
        self.trace = []
        self.flaws = []
        self._current_time = "12:00"
        self._current_date = "2026-03-23"  # Simulation date — avoid real date.today() in guards
        self._last_route = None  # set by _run_telegram_message for routing assertions

    # -------------------------------------------------------------------------
    # Event dispatch
    # -------------------------------------------------------------------------

    def step(self, event: dict):
        """
        Process one event. Records before/after state diff.

        Supported event types:
          {"type": "morning_brief"}
          {"type": "telegram_message", "text": "...", "time": "HH:MM"}
          {"type": "close_session", "lifts": [...], "flags": {...}}
          {"type": "close_day"}
          {"type": "weekly_eval"}
          {"type": "monthly_eval"}
          {"type": "annual_eval"}
          {"type": "time_advance", "to": "HH:MM"}
        """
        before = self.memory.snapshot()
        self.memory.reset_mutation_log()

        result = self._dispatch(event)

        after = self.memory.snapshot()
        mutations = list(self.memory.mutation_log)

        self.trace.append({
            "event": event,
            "state_before": before,
            "state_after": after,
            "mutations": mutations,
            "result": result,
            "llm_calls_at_step": list(self.llm.get_call_log()),
            "time": self._current_time
        })
        return result

    def _dispatch(self, event: dict):
        t = event.get("type")
        if t == "morning_brief":
            return self._run_morning_brief()
        elif t == "telegram_message":
            if "time" in event:
                self._current_time = event["time"]
            return self._run_telegram_message(event["text"], event.get("time", self._current_time))
        elif t == "close_session":
            return self._run_close_session(event.get("lifts", []), event.get("flags", {}))
        elif t == "close_day":
            return self._run_close_day()
        elif t == "weekly_eval":
            return self._run_weekly_eval()
        elif t == "monthly_eval":
            return self._run_monthly_eval()
        elif t == "annual_eval":
            return self._run_annual_eval()
        elif t == "time_advance":
            self._current_time = event.get("to", self._current_time)
            return {"advanced_to": self._current_time}
        else:
            raise ValueError(f"Unknown event type: '{t}'. Valid types: morning_brief, telegram_message, "
                             "close_session, close_day, weekly_eval, monthly_eval, annual_eval, time_advance")

    # -------------------------------------------------------------------------
    # Event implementations
    # Each calls into the real production function via the injected patch context.
    # The actual patching is set up in runner.py before sim.step() is called.
    # -------------------------------------------------------------------------

    def _run_morning_brief(self):
        """Run run_brief() from run_coach.py with mock context."""
        try:
            import run_coach
            result = run_coach.run_brief(dry_run=True)
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _run_close_day(self):
        """Run cascade_levels.close_day() with mock memory."""
        try:
            import cascade_levels
            result = cascade_levels.close_day()
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _run_weekly_eval(self):
        """Run cascade_levels.weekly_eval() with mock memory."""
        try:
            import cascade_levels
            result = cascade_levels.weekly_eval()
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _run_monthly_eval(self):
        """Run cascade_levels.monthly_eval() with mock memory."""
        try:
            import cascade_levels
            result = cascade_levels.monthly_eval()
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _run_annual_eval(self):
        """Run cascade_levels.annual_eval() with mock memory."""
        try:
            import cascade_levels
            result = cascade_levels.annual_eval()
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _run_telegram_message(self, text: str, time: str):
        """
        Simulate a Telegram message arriving at the given time.
        Invokes the intent classification + routing path.
        For CURRENT_FLOW intercepts, invokes the appropriate handler.
        """
        # Record the inbound message
        self.memory.append_telegram_log("IN", text)

        # Check CURRENT_FLOW to determine routing
        coach_state = self.memory.read_coach_state()
        cf_raw = (coach_state.get("CURRENT_FLOW", {}) or {})
        if isinstance(cf_raw, str):
            cf = cf_raw
        elif isinstance(cf_raw, dict):
            cf = cf_raw.get("Summary", "") or cf_raw.get("summary", "") or ""
        else:
            cf = ""

        routed_to = self._classify_and_route(text, cf, coach_state)
        # Store last routing result so assertions can check it via domain "__last_route__"
        self._last_route = routed_to
        return {
            "status": "ok",
            "text": text,
            "time": time,
            "current_flow": cf,
            "routed_to": routed_to
        }

    def _classify_and_route(self, text: str, current_flow: str, coach_state: dict) -> str:
        """
        Simplified routing for simulation purposes.
        Mirrors the real telegram_bot.py routing logic without async/Telegram.
        """
        import json as _json_r
        text_lower = text.lower()
        _today_r = self._current_date  # use simulation date, not real date.today()

        # CURRENT_FLOW intercepts (check first)

        # Dict-format CURRENT_FLOW — golden rule 2x confirm flow
        if current_flow.strip().startswith("{"):
            try:
                _cf_data_r = _json_r.loads(current_flow)
                if (_cf_data_r.get("flow") == "cascade_awaiting_confirm" and
                        _cf_data_r.get("pending_golden_rule_confirm", False)):
                    return self._handle_golden_rule_confirm(text, _cf_data_r)
            except Exception:
                pass

        # String-format CURRENT_FLOW intercepts
        if current_flow.startswith("weekly_planning"):
            return "weekly_planning_handler"
        if current_flow.startswith("daily_planning"):
            return "daily_planning_handler"
        if current_flow.startswith("monthly_planning"):
            return "monthly_planning_handler"
        if current_flow.startswith("annual_planning"):
            return "annual_planning_handler"
        if current_flow.startswith("cascade_awaiting_confirm"):
            return "cascade_confirm_handler"

        # daily_catchup intercept — response to the DAILY_FOCUS guard catch-up prompt
        if current_flow == "daily_catchup":
            _yes_r = {"sí", "si", "yes", "yep", "yeah", "claro", "voy"}
            _no_r = {"no", "nope", "descanso", "rest", "skip", "hoy no"}
            if any(w in text_lower for w in _yes_r) or text_lower.strip() in _yes_r:
                # Transition to daily_planning flow
                self.memory.upsert_coach_state(
                    "CURRENT_FLOW",
                    f"daily_planning | {_today_r} | catchup"
                )
                return "daily_catchup_yes"
            elif any(w in text_lower for w in _no_r) or text_lower.strip() in _no_r:
                # Rest day — log and close
                self.memory.upsert_coach_state(
                    "DAILY_FOCUS",
                    _json_r.dumps({"date": _today_r, "session": "rest", "rest_day": True})
                )
                self.memory.upsert_coach_state("CURRENT_FLOW", "")
                return "daily_catchup_no"
            else:
                return "daily_catchup_unclear"

        # DAILY_FOCUS guard — Phase 2 fix for BUG-02
        # When no CURRENT_FLOW is active and time >= 10:00, check if today's plan is confirmed.
        if not current_flow:
            _hour_r = int(self._current_time.split(":")[0])
            if _hour_r >= 10:
                _df_envelope_r = coach_state.get("DAILY_FOCUS") or {}
                _df_raw_r = (_df_envelope_r.get("summary", "") or _df_envelope_r.get("Summary", "")
                             if isinstance(_df_envelope_r, dict) else "")
                _df_current_r = False
                if _df_raw_r and _df_raw_r.strip().startswith("{"):
                    try:
                        _df_r = _json_r.loads(_df_raw_r)
                        _df_current_r = (_df_r.get("date") == _today_r)
                    except Exception:
                        pass
                elif isinstance(_df_envelope_r, str) and _df_envelope_r.strip().startswith("{"):
                    try:
                        _df_r = _json_r.loads(_df_envelope_r)
                        _df_current_r = (_df_r.get("date") == _today_r)
                    except Exception:
                        pass
                if not _df_current_r:
                    self.memory.upsert_coach_state("CURRENT_FLOW", "daily_catchup")
                    return "catch_up_handler"

        # Simple keyword classification (mirrors Haiku classifier output for simulation)
        program_keywords = ("programa", "cardio", "nuevo programa", "dejar", "correr",
                            "deload", "bloque", "diseña")
        # QUESTION: specific data lookups — checked before workout to avoid WORKOUT mis-routing
        question_keywords = ("cuánto levanté", "cuánto hice", "cuánto pesé", "qué peso usé",
                             "cuál fue mi", "semana pasada", "la semana pasada", "cuándo fue",
                             "mi último", "mis últimos", "cuánto deadlift", "cuánto squat",
                             "cuánto bench", "último entreno")
        workout_keywords = ("squat", "bench", "deadlift", "sets", "entreno", "workout",
                            "levanté", "levante", "cuánto")
        health_keywords = ("codo", "dolor", "fatiga", "sueño", "sleep", "elbow", "pain",
                           "lesión", "lesion")
        skip_keywords = ("no puedo entrenar", "reunión", "reunion", "skip", "salto",
                         "no puedo ir")

        if any(kw in text_lower for kw in skip_keywords):
            self._handle_session_skip(text, coach_state)
            return "session_skip_escalation"

        if any(kw in text_lower for kw in program_keywords):
            # Simulate program_agent LLM call and detect golden rule conflict
            try:
                response = self.llm.complete(
                    mode="program_agent",
                    system="program_agent",
                    messages=[{"role": "user", "content": text}]
                )
                _gr_signals = ("golden_rule_conflict", "golden_rule_override",
                               "golden rule", "constitutional")
                if any(sig in response.lower() for sig in _gr_signals):
                    _gr_flow = _json_r.dumps({
                        "flow": "cascade_awaiting_confirm",
                        "level": "PROGRAM",
                        "pending_golden_rule_confirm": True,
                        "confirm_count": 0,
                        "proposal_summary": text[:100]
                    })
                    self.memory.upsert_coach_state("CURRENT_FLOW", _gr_flow)
                    return "program_agent_golden_rule_conflict"
            except Exception:
                pass
            return "program_agent"

        if any(kw in text_lower for kw in question_keywords):
            try:
                self.llm.complete(
                    mode="question_lookup",
                    system="question_lookup",
                    messages=[{"role": "user", "content": text}]
                )
            except Exception:
                pass
            return "question_lookup"

        if any(kw in text_lower for kw in health_keywords):
            return "health_agent"

        if any(kw in text_lower for kw in workout_keywords):
            return "workout_agent"

        return "general_response"

    def _handle_golden_rule_confirm(self, text: str, cf_data: dict) -> str:
        """
        Handle the 2x confirmation flow for golden rule overrides.
        - First non-denial → confirm_count 0→1, update CURRENT_FLOW, return "golden_rule_first_confirm"
        - Second non-denial → write override_log, clear CURRENT_FLOW, return "golden_rule_override_applied"
        - Any explicit denial → clear CURRENT_FLOW, return "golden_rule_override_cancelled"
        """
        import json as _json_gr
        text_lower = text.lower().strip()
        _no_words = {"no", "cancel", "cancela", "cancelar"}

        # Explicit denial
        if any(w == text_lower or w in text_lower.split() for w in _no_words):
            self.memory.upsert_coach_state("CURRENT_FLOW", "")
            return "golden_rule_override_cancelled"

        confirm_count = cf_data.get("confirm_count", 0)
        if confirm_count < 1:
            # First confirmation — increment and push back
            cf_data["confirm_count"] = 1
            self.memory.upsert_coach_state("CURRENT_FLOW", _json_gr.dumps(cf_data))
            return "golden_rule_first_confirm"
        else:
            # Second confirmation — write override_log and clear flow
            # get_domain() returns the already-parsed dict (MockMemory parses JSON on read)
            gr_val = self.memory.get_domain("GOLDEN_RULES")
            if isinstance(gr_val, dict):
                gr_data = gr_val
            elif isinstance(gr_val, str):
                try:
                    gr_data = _json_gr.loads(gr_val)
                except Exception:
                    gr_data = {"rules": [], "override_log": []}
            else:
                gr_data = {"rules": [], "override_log": []}
            gr_data.setdefault("override_log", [])
            gr_data["override_log"].append({
                "rule_id": cf_data.get("proposal_summary", "unknown")[:50],
                "date": self._current_date,
                "reason": "2x athlete-confirmed override"
            })
            self.memory.upsert_coach_state("GOLDEN_RULES", _json_gr.dumps(gr_data))
            self.memory.upsert_coach_state("CURRENT_FLOW", "")
            return "golden_rule_override_applied"

    def _handle_session_skip(self, text: str, coach_state: dict = None):  # noqa: ARG002
        """
        Simulate the escalation path for a session skip message.
        Sets CASCADE_STATE.WEEKLY to AWAITING_USER, DAILY to LOCKED.
        Does NOT modify WEEKLY_INTENT (that requires user confirmation).
        """
        import cascade_state
        try:
            cascade_state.set_level_state(
                "WEEKLY",
                "AWAITING_USER",
                context={"skip_message": text, "pending_reschedule": True}
            )
        except Exception:
            # If cascade_state is patched, it goes through memory mock
            # Set the state manually in mock memory
            import json as _json_skip
            current_cs = self.memory.get_domain("CASCADE_STATE") or {}
            if not isinstance(current_cs, dict):
                current_cs = {}
            current_cs["WEEKLY"] = {"state": "AWAITING_USER", "context": {"skip_message": text}}
            current_cs["DAILY"] = {"state": "LOCKED", "locked_by": "WEEKLY"}
            self.memory.upsert_coach_state("CASCADE_STATE", _json_skip.dumps(current_cs))

    def _run_close_session(self, lifts: list, flags: dict):
        """
        Simulate session close: records lifts, handles injury flags.
        """
        # Record lifts to lift history
        if lifts:
            self.memory.append_lift_history(lifts)

        # Handle injury flag
        elbow_pain = flags.get("elbow_pain", 0)
        result = {"lifts_logged": len(lifts), "flags": flags}

        if elbow_pain and elbow_pain > 3:
            # Mark for close_day escalation
            self.memory.upsert_coach_state(
                "SESSION_FLAGS",
                {"elbow_pain": elbow_pain, "requires_escalation": True}
            )
            result["injury_flag"] = f"elbow_pain={elbow_pain} > 3, flagged for close_day escalation"

        return result

    # -------------------------------------------------------------------------
    # Assertions
    # -------------------------------------------------------------------------

    def assert_state(self, domain: str, check: Any, msg: str = ""):
        """
        Assert that a domain's current value satisfies check.

        check can be:
          - dict: all specified keys must match
          - str: value (as string) must contain this string (case-insensitive)
          - callable: check(actual) must return True
          - "non_empty": value must be non-null and non-empty
          - "non_null": value must not be None
        """
        # Support nested domain paths like "CASCADE_STATE.DAILY.state"
        actual = self._get_nested(domain)
        if not self._evaluate_check(actual, check):
            self.flaws.append({
                "type": "assertion_failure",
                "domain": domain,
                "check": str(check),
                "actual": actual,
                "msg": msg or f"assert_state failed for '{domain}'"
            })

    def assert_no_change(self, domains: list, msg: str = ""):
        """
        Assert that none of the listed domains were mutated in the last step.
        Uses mutation_log from the most recent reset.
        """
        for domain in domains:
            if self.memory.was_mutated(domain):
                mutations = self.memory.get_mutations_for(domain)
                self.flaws.append({
                    "type": "unexpected_mutation",
                    "domain": domain,
                    "mutations": mutations,
                    "msg": msg or f"Domain '{domain}' was mutated but should not have been"
                })

    def assert_llm_call_system_not_contains(self, call_key: str, substring: str, msg: str = ""):
        """
        Assert that a specific LLM call's system prompt does NOT contain a substring.
        Used for fixture_04 to verify morning brief doesn't leak pending requests.
        """
        for call in self.llm.get_call_log():
            if call["key"] == call_key:
                if substring.lower() in call["system_preview"].lower():
                    self.flaws.append({
                        "type": "context_leak",
                        "call_key": call_key,
                        "forbidden_substring": substring,
                        "system_preview": call["system_preview"],
                        "msg": msg or f"LLM call '{call_key}' system prompt contains forbidden string '{substring}'"
                    })
                return
        # Call not found — not necessarily a flaw (call may not have been made)

    def assert_llm_call_message_contains(self, call_key: str, substring: str, msg: str = ""):
        """
        Assert that a specific LLM call's system prompt OR user message content contains substring.
        Used to verify that code-generated signals (skip_patterns, golden_rules) reach the LLM.
        """
        for call in self.llm.get_call_log():
            if call["key"] == call_key:
                full_system = call.get("system", "")
                last_msg = call.get("last_user_message", "")
                if (substring.lower() in full_system.lower() or
                        substring.lower() in last_msg.lower()):
                    return
                # Found the call but substring not in it
                self.flaws.append({
                    "type": "llm_call_content_mismatch",
                    "call_key": call_key,
                    "expected_substring": substring,
                    "system_preview": full_system[:300],
                    "msg": msg or f"LLM call '{call_key}' does not contain expected string '{substring}'"
                })
                return
        # Call was never made — also a flaw
        self.flaws.append({
            "type": "llm_call_not_found",
            "call_key": call_key,
            "expected_substring": substring,
            "msg": msg or f"LLM call '{call_key}' was never made"
        })

    def _get_nested(self, domain_path: str) -> Any:
        """Support dot-notation for nested access: 'CASCADE_STATE.DAILY.state'
        Special domains: '__last_route__' returns the last Telegram routing decision."""
        parts = domain_path.split(".")
        if parts[0] == "__last_route__":
            return self._last_route
        value = self.memory.get_domain(parts[0])
        for part in parts[1:]:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    def _evaluate_check(self, actual: Any, check: Any) -> bool:
        if check == "non_empty":
            return bool(actual)
        if check == "non_null":
            return actual is not None
        if callable(check):
            try:
                return bool(check(actual))
            except Exception:
                return False
        if isinstance(check, str):
            return isinstance(actual, str) and check.lower() in actual.lower()
        if isinstance(check, dict):
            if not isinstance(actual, dict):
                return False
            return all(actual.get(k) == v for k, v in check.items())
        return actual == check

    # -------------------------------------------------------------------------
    # Reporting
    # -------------------------------------------------------------------------

    def report(self) -> dict:
        return {
            "scenario": self.fixture.get("scenario_name", "unknown"),
            "verdict": "PASS" if not self.flaws else "FAIL",
            "flaws": self.flaws,
            "trace_length": len(self.trace),
            "llm_calls_total": len(self.llm.get_call_log()),
            "mutations_by_domain": self._count_mutations()
        }

    def _count_mutations(self) -> dict:
        counts = {}
        for entry in self.trace:
            for m in entry.get("mutations", []):
                domain = m["domain"]
                counts[domain] = counts.get(domain, 0) + 1
        return counts
