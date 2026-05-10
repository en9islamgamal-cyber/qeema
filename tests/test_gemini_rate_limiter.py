"""Tests for v22.5 shared per-key Gemini rate limiter.

These tests lock in the critical v22.5 invariant:
    ALL Gemini-using components on the same key share the same sliding window.

Without these tests passing, Phase 1 can exceed 5 RPM on key #1 by combining
ScriptEngine traffic with TafsirValidator traffic, which would burn the key
mid-episode in production.
"""
import sys
import time
import types as tp
import pytest

from core.gemini_rate_limiter import (
    KeyRateLimiter, limiter_for_key, reset_all_limiters,
)


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts with a fresh limiter registry."""
    reset_all_limiters()
    yield
    reset_all_limiters()


# ════════════════════════════════════════════════════════════════
# Core limiter behavior
# ════════════════════════════════════════════════════════════════
class TestKeyRateLimiter:
    def test_first_n_calls_no_wait(self):
        l = KeyRateLimiter("test", max_per_minute=4)
        for _ in range(4):
            assert l.acquire() == 0.0

    def test_n_plus_1_call_blocks_or_raises(self):
        """5th call within 60s must wait or raise TimeoutError."""
        l = KeyRateLimiter("test", max_per_minute=4)
        for _ in range(4):
            l.acquire()
        # 5th call: would have to wait ~60s. With max_wait=1, should raise.
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            l.acquire(max_wait_seconds=1.0)
        elapsed = time.monotonic() - t0
        # Should fail fast (preflight check), not actually wait
        assert elapsed < 0.5, f"Preflight detection should be fast, took {elapsed:.2f}s"

    def test_window_slides(self):
        """Old timestamps fall out of the window."""
        l = KeyRateLimiter("test", max_per_minute=2, window_seconds=0.5)
        l.acquire()
        l.acquire()
        # After window expires, slots free up
        time.sleep(0.6)
        # Should not block
        t0 = time.monotonic()
        l.acquire()
        assert (time.monotonic() - t0) < 0.1

    def test_current_usage_reports_window_count(self):
        l = KeyRateLimiter("test", max_per_minute=4)
        assert l.current_usage() == 0
        l.acquire()
        l.acquire()
        assert l.current_usage() == 2


# ════════════════════════════════════════════════════════════════
# Shared limiter registry — THE critical v22.5 invariant
# ════════════════════════════════════════════════════════════════
class TestSharedLimiterRegistry:
    def test_same_key_returns_same_limiter(self):
        """If two callers ask for the same key, they get the SAME limiter
        instance — that's how their rate windows merge."""
        l1 = limiter_for_key("test-key")
        l2 = limiter_for_key("test-key")
        assert l1 is l2

    def test_different_keys_independent(self):
        l_a = limiter_for_key("key-A")
        l_b = limiter_for_key("key-B")
        assert l_a is not l_b
        # Saturate A
        for _ in range(4):
            l_a.acquire()
        # B unaffected
        assert l_b.current_usage() == 0
        l_b.acquire()
        assert l_b.current_usage() == 1

    def test_label_hint_only_used_first_time(self):
        l1 = limiter_for_key("test-key", label_hint="first")
        l2 = limiter_for_key("test-key", label_hint="second")
        # Second call returns existing — label_hint ignored
        assert l1 is l2

    def test_empty_key_rejected(self):
        with pytest.raises(ValueError):
            limiter_for_key("")


# ════════════════════════════════════════════════════════════════
# Integration: GeminiJsonAdapter + GeminiReviewer share the limiter
# This is the v22.5 architectural invariant for Phase 1 safety.
# ════════════════════════════════════════════════════════════════
@pytest.fixture
def fake_genai(monkeypatch):
    """Inject a no-op fake google.genai so adapters can be instantiated."""
    fake_genai_mod = tp.ModuleType("google.genai")

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        class models:
            @staticmethod
            def generate_content(**kw):
                r = tp.SimpleNamespace()
                r.text = '{"ok": true}'
                return r

    fake_genai_mod.Client = FakeClient

    fake_types = tp.ModuleType("google.genai.types")

    class FakeConfig:
        def __init__(self, **kw):
            pass

    fake_types.GenerateContentConfig = FakeConfig

    fake_google = tp.ModuleType("google")
    fake_google.genai = fake_genai_mod

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    yield


class TestPhase1RateLimitInvariant:
    """The critical v22.5 invariant: ScriptEngine and TafsirValidator
    on the same Phase 1 key MUST share the rate window."""

    def test_two_adapters_same_key_share_limiter(self, fake_genai):
        """Two GeminiJsonAdapter instances with the same key must share."""
        from infrastructure.llm_adapters import GeminiJsonAdapter
        a1 = GeminiJsonAdapter("phase1-key", instance_name="script")
        a2 = GeminiJsonAdapter("phase1-key", instance_name="phase2-deep")
        assert a1._rate_limiter is a2._rate_limiter

    def test_reviewer_and_adapter_same_key_share_limiter(self, fake_genai):
        """GeminiReviewer (in TafsirValidator) and GeminiJsonAdapter (in
        ScriptEngine) on the same Phase 1 key MUST share the limiter.

        Without this, Phase 1 can exceed 5 RPM by combining their traffic,
        burning key #1 mid-episode."""
        from engines.tafsir_validator import GeminiReviewer
        from infrastructure.llm_adapters import GeminiJsonAdapter
        reviewer = GeminiReviewer("phase1-key")
        adapter = GeminiJsonAdapter("phase1-key", instance_name="script")
        assert reviewer._rate_limiter is not None
        assert adapter._rate_limiter is not None
        assert reviewer._rate_limiter is adapter._rate_limiter, (
            "GeminiReviewer + GeminiJsonAdapter on same key MUST share limiter"
        )

    def test_combined_traffic_respects_4_rpm(self, fake_genai):
        """Verify that 4 script calls + 1 tafsir call on the same key hit
        the limit on the 5th total call (regardless of which type)."""
        from engines.tafsir_validator import GeminiReviewer
        from infrastructure.llm_adapters import GeminiJsonAdapter

        adapter = GeminiJsonAdapter("phase1-key", instance_name="script")
        reviewer = GeminiReviewer("phase1-key")

        # 4 calls via adapter
        for _ in range(4):
            adapter.generate_json("test")

        # 5th call — through reviewer's shared limiter — must be blocked
        with pytest.raises(TimeoutError):
            reviewer._rate_limiter.acquire(max_wait_seconds=1.0)

    def test_different_keys_no_interference(self, fake_genai):
        """Phase 1 (key 1) and Phase 2 (key 2) traffic are independent."""
        from infrastructure.llm_adapters import GeminiJsonAdapter
        phase1 = GeminiJsonAdapter("key-1", instance_name="phase1")
        phase2 = GeminiJsonAdapter("key-2", instance_name="phase2")
        assert phase1._rate_limiter is not phase2._rate_limiter

        # Saturate phase1 — phase2 unaffected
        for _ in range(4):
            phase1.generate_json("x")
        # phase2 should still be free
        phase2.generate_json("y")  # MUST NOT raise
        assert phase2._rate_limiter.current_usage() == 1
