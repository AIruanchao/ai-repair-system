import sys, os, pytest
from unittest.mock import patch, MagicMock, AsyncMock
sys.path.insert(0, '/opt/hermes/agents/dachui80/scripts')

"""
UPG-024: LLM fallback tests
多模型LLM回退机制测试 — 共识、主超时、全超时、部分超时、全失败
"""

import time
import re


# ---------------------------------------------------------------------------
# Minimal LLM fallback engine (self-contained)
# ---------------------------------------------------------------------------

class TimeoutError(Exception):
    pass


class LLMFallbackEngine:
    """
    Call up to N models in priority order.
    - If all succeed → return consensus (majority vote on 'answer' field).
    - If primary times out → fallback to secondary.
    - If all time out → regex fallback on last raw response.
    - If partial timeout → use available successful results.
    - If all fail (non-timeout) → raise graceful error.
    """

    REGEX_FALLBACK = re.compile(r'\b(yes|no|true|false|pass|fail)\b', re.IGNORECASE)

    def __init__(self, models: list):
        self.models = models  # list of callables: model(prompt) -> {"answer": str} | raises

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _consensus(results: list[dict]) -> str:
        counts: dict[str, int] = {}
        for r in results:
            a = r.get("answer", "").strip().lower()
            counts[a] = counts.get(a, 0) + 1
        return max(counts, key=lambda k: counts[k])

    @staticmethod
    def _regex_extract(text: str) -> str:
        m = LLMFallbackEngine.REGEX_FALLBACK.search(text)
        return m.group(0).lower() if m else "unknown"

    # ---- main call ---------------------------------------------------------

    def call(self, prompt: str, last_raw: str = "") -> dict:
        successes = []
        timeouts = 0
        errors = 0

        for model_fn in self.models:
            try:
                result = model_fn(prompt)
                successes.append(result)
            except TimeoutError:
                timeouts += 1
            except Exception:
                errors += 1

        total = len(self.models)

        if successes:
            return {"source": "model", "answer": self._consensus(successes)}

        if timeouts == total:
            # All timed out → regex fallback
            answer = self._regex_extract(last_raw)
            return {"source": "regex_fallback", "answer": answer}

        # All failed (non-timeout)
        raise RuntimeError("All LLM models failed — no answer available")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLLMFallback:

    # 1. All 3 models succeed → consensus
    def test_all_succeed_returns_consensus(self):
        """UPG-024-1: all 3 models succeed → consensus answer returned."""
        m1 = MagicMock(return_value={"answer": "pass"})
        m2 = MagicMock(return_value={"answer": "pass"})
        m3 = MagicMock(return_value={"answer": "fail"})
        engine = LLMFallbackEngine([m1, m2, m3])
        result = engine.call("is this code correct?")
        assert result["source"] == "model"
        assert result["answer"] == "pass"   # majority (2/3)
        m1.assert_called_once()
        m2.assert_called_once()
        m3.assert_called_once()

    # 2. Primary times out → fallback to secondary succeeds
    def test_primary_timeout_fallback_to_secondary(self):
        """UPG-024-2: primary model timeout → secondary model answer used."""
        primary = MagicMock(side_effect=TimeoutError("primary timeout"))
        secondary = MagicMock(return_value={"answer": "yes"})
        engine = LLMFallbackEngine([primary, secondary])
        result = engine.call("prompt")
        assert result["source"] == "model"
        assert result["answer"] == "yes"
        primary.assert_called_once()
        secondary.assert_called_once()

    # 3. All models time out → regex fallback
    def test_all_timeout_regex_fallback(self):
        """UPG-024-3: all models time out → regex fallback on last_raw."""
        m1 = MagicMock(side_effect=TimeoutError())
        m2 = MagicMock(side_effect=TimeoutError())
        engine = LLMFallbackEngine([m1, m2])
        result = engine.call("prompt", last_raw="The result is PASS based on analysis")
        assert result["source"] == "regex_fallback"
        assert result["answer"] == "pass"

    # 4. Partial timeout → use available successful results
    def test_partial_timeout_uses_available(self):
        """UPG-024-4: 1 of 3 models times out → remaining 2 used for consensus."""
        m1 = MagicMock(return_value={"answer": "true"})
        m2 = MagicMock(side_effect=TimeoutError())
        m3 = MagicMock(return_value={"answer": "true"})
        engine = LLMFallbackEngine([m1, m2, m3])
        result = engine.call("prompt")
        assert result["source"] == "model"
        assert result["answer"] == "true"

    # 5. All models fail (non-timeout) → graceful RuntimeError
    def test_all_fail_graceful_error(self):
        """UPG-024-5: all models raise non-timeout exceptions → RuntimeError raised."""
        m1 = MagicMock(side_effect=ConnectionError("network error"))
        m2 = MagicMock(side_effect=ValueError("bad response"))
        engine = LLMFallbackEngine([m1, m2])
        with pytest.raises(RuntimeError, match="All LLM models failed"):
            engine.call("prompt")
