"""
MockLLM — deterministic mock for the Anthropic Claude API.

Each fixture defines expected LLM call responses keyed by "{mode}_{call_index}".
Mode is passed via metadata={"mode": "..."} in each API call.
If an unexpected LLM call is made → raises ValueError immediately.
This catches any accidental real API calls during tests.
"""


class MockLLM:
    def __init__(self, responses: dict):
        """
        Args:
            responses: dict keyed by "{mode}_{call_index}" → response string
                       e.g. {"close_day_0": '{"date": "..."}', "weekly_eval_1": '...'}
                       Loaded from fixture JSON under "mock_llm_responses" key.
        """
        self.responses = responses
        self.call_log = []
        self.call_counts = {}  # mode -> count

    def complete(self, mode: str, system: str, messages: list, **kwargs) -> str:
        """
        Called by the mock anthropic client in place of messages.create().
        Records the call and returns the fixture-defined response.
        """
        count = self.call_counts.get(mode, 0)
        call_key = f"{mode}_{count}"
        self.call_counts[mode] = count + 1
        self.call_log.append({
            "key": call_key,
            "mode": mode,
            "system_preview": system[:150] if system else "",
            "last_user_message": messages[-1]["content"][:200] if messages else ""
        })
        if call_key not in self.responses:
            available = list(self.responses.keys())
            raise ValueError(
                f"Unexpected LLM call: key='{call_key}'.\n"
                f"Available keys: {available}\n"
                f"Add mock response to fixture's 'mock_llm_responses' dict.\n"
                f"System preview: {system[:100] if system else '(none)'}"
            )
        return self.responses[call_key]

    def get_call_log(self) -> list:
        return list(self.call_log)

    def was_called_with_mode(self, mode: str) -> bool:
        return any(c["mode"] == mode for c in self.call_log)

    def get_calls_for_mode(self, mode: str) -> list:
        return [c for c in self.call_log if c["mode"] == mode]

    def last_call(self) -> dict | None:
        return self.call_log[-1] if self.call_log else None


def build_mock_anthropic_client(llm: MockLLM):
    """
    Returns a mock Anthropic client whose messages.create() delegates to llm.complete().
    Used in runner.py via unittest.mock.patch("anthropic.Anthropic").
    """
    class _MockContent:
        def __init__(self, text):
            self.text = text

    class _MockResponse:
        def __init__(self, text):
            self.content = [_MockContent(text)]
            self.usage = type("Usage", (), {"input_tokens": 100, "output_tokens": 50})()

    class _MockMessages:
        def __init__(self, llm_instance):
            self._llm = llm_instance

        def create(self, **kwargs):
            mode = (kwargs.get("metadata") or {}).get("mode", "unknown")
            system = kwargs.get("system", "")
            messages = kwargs.get("messages", [])
            text = self._llm.complete(mode=mode, system=system, messages=messages)
            return _MockResponse(text)

    class _MockClient:
        def __init__(self, llm_instance):
            self.messages = _MockMessages(llm_instance)

    return _MockClient(llm)
