"""
tests/test_resilience_v2.py
=============================
Verifies the concurrency, exception-policy, and routing fixes in v2.

Key invariants we test:
  1. Metrics are atomic — 1000 concurrent calls produce exactly
     1000 in total_calls, no lost increments.
  2. Programmer errors (KeyError, AttributeError) propagate without
     failover, unlike v1 which caught Exception.
  3. Round-robin distribution is fair across providers.
"""
from __future__ import annotations

import threading
from typing import List

import pytest

from core.exceptions import (
    PermanentError,
    ProviderUnavailableError,
    RateLimitError,
    TransientError,
)
from core.resilience import CircuitBreakerConfig
from core.resilience_v2 import ProviderPoolV2


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════
def make_pool(n: int = 3, strategy: str = "round_robin") -> ProviderPoolV2:
    pool = ProviderPoolV2("test_pool", strategy=strategy)
    for i in range(n):
        pool.register(
            f"p{i}",
            breaker_config=CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout_sec=1.0,
            ),
        )
    return pool


# ════════════════════════════════════════════════════════════════
# Atomic metrics
# ════════════════════════════════════════════════════════════════
class TestAtomicMetrics:
    def test_concurrent_increments_no_loss(self) -> None:
        """1000 concurrent successes → exactly 1000 in total_calls."""
        pool = make_pool(n=1)

        def call_pool() -> None:
            pool.execute(lambda name: 42)

        threads = [threading.Thread(target=call_pool) for _ in range(1000)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        report = pool.health_report()
        provider = report["providers"][0]
        assert provider["calls"] == 1000
        assert provider["failures"] == 0
        assert provider["success_rate"] == 1.0

    def test_failures_counted_correctly(self) -> None:
        pool = ProviderPoolV2("test", strategy="round_robin")
        pool.register(
            "p0",
            breaker_config=CircuitBreakerConfig(
                failure_threshold=10000,  # high so circuit doesn't trip
                recovery_timeout_sec=1.0,
            ),
        )

        def fail(name: str) -> None:
            raise TransientError("simulated")

        for _ in range(50):
            with pytest.raises(TransientError):
                pool.execute(fail)

        report = pool.health_report()
        p0 = report["providers"][0]
        assert p0["calls"] == 50
        assert p0["failures"] == 50
        assert p0["success_rate"] == 0.0


# ════════════════════════════════════════════════════════════════
# Exception policy — the v1 → v2 contract change
# ════════════════════════════════════════════════════════════════
class TestExceptionPolicy:
    def test_transient_triggers_failover(self) -> None:
        """A TransientError on p0 must cause p1 to be tried."""
        pool = make_pool(n=2)
        attempts: List[str] = []

        def func(name: str) -> str:
            attempts.append(name)
            if name == "p0":
                raise TransientError("p0 down")
            return f"hello from {name}"

        result = pool.execute(func)
        assert result == "hello from p1"
        assert "p0" in attempts
        assert "p1" in attempts

    def test_rate_limit_triggers_failover(self) -> None:
        pool = make_pool(n=2)
        attempts: List[str] = []

        def func(name: str) -> str:
            attempts.append(name)
            if name == "p0":
                raise RateLimitError("p0 limited")
            return "ok"

        assert pool.execute(func) == "ok"
        assert len(attempts) == 2

    def test_keyerror_propagates_immediately(self) -> None:
        """
        A KeyError (programmer bug) must NOT be caught and retried.
        This is the v1 → v2 behavior change.
        """
        pool = make_pool(n=3)
        attempts: List[str] = []

        def func(name: str) -> str:
            attempts.append(name)
            raise KeyError("forgot to handle this case")

        with pytest.raises(KeyError):
            pool.execute(func)

        # CRITICAL: only one provider should have been tried.
        # In v1 this would have tried all 3 because Exception was caught.
        assert len(attempts) == 1

    def test_attributeerror_propagates_immediately(self) -> None:
        pool = make_pool(n=3)
        attempts: List[str] = []

        def func(name: str) -> str:
            attempts.append(name)
            raise AttributeError("typo: req.txet instead of req.text")

        with pytest.raises(AttributeError):
            pool.execute(func)
        assert len(attempts) == 1

    def test_value_error_propagates(self) -> None:
        """ValueError = caller passed garbage. Don't retry."""
        pool = make_pool(n=3)
        attempts: List[str] = []

        def func(name: str) -> str:
            attempts.append(name)
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            pool.execute(func)
        assert len(attempts) == 1

    def test_permanent_error_propagates(self) -> None:
        """PermanentError must not be retried (auth, config, etc.)."""
        pool = make_pool(n=3)

        def func(name: str) -> str:
            raise PermanentError("unauthorized")

        with pytest.raises(PermanentError):
            pool.execute(func)


# ════════════════════════════════════════════════════════════════
# Routing
# ════════════════════════════════════════════════════════════════
class TestRouting:
    def test_round_robin_distribution(self) -> None:
        """RR over 3 providers, 30 calls → 10 each (give or take)."""
        pool = make_pool(n=3, strategy="round_robin")
        counts: dict[str, int] = {}

        def func(name: str) -> str:
            counts[name] = counts.get(name, 0) + 1
            return name

        for _ in range(30):
            pool.execute(func)

        # Each provider should get exactly 10 calls
        assert counts == {"p0": 10, "p1": 10, "p2": 10}

    def test_round_robin_skips_open_circuit(self) -> None:
        """An open circuit is excluded from rotation."""
        pool = make_pool(n=3, strategy="round_robin")
        counts: dict[str, int] = {}

        def func(name: str) -> str:
            counts[name] = counts.get(name, 0) + 1
            if name == "p1":
                raise TransientError("p1 always fails")
            return name

        # Trip p1's circuit (need 5 failures by default)
        for _ in range(20):
            try:
                pool.execute(func)
            except TransientError:
                pass

        # After p1 trips, only p0 and p2 should be tried
        counts.clear()
        for _ in range(20):
            try:
                pool.execute(func)
            except TransientError:
                pass

        # p1 should have been called 0 times (or very few — only when
        # circuit transitions to half-open)
        assert counts.get("p1", 0) <= 2
        assert counts.get("p0", 0) + counts.get("p2", 0) >= 18

    def test_unknown_strategy_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProviderPoolV2("test", strategy="random")  # type: ignore[arg-type]

    def test_duplicate_registration_rejected(self) -> None:
        pool = ProviderPoolV2("test")
        pool.register("p0")
        with pytest.raises(ValueError):
            pool.register("p0")


# ════════════════════════════════════════════════════════════════
# Failover exhaustion
# ════════════════════════════════════════════════════════════════
class TestFailoverExhaustion:
    def test_all_providers_fail_raises_last_error(self) -> None:
        pool = make_pool(n=3)

        def all_fail(name: str) -> str:
            raise TransientError(f"{name} dead")

        with pytest.raises(TransientError) as exc_info:
            pool.execute(all_fail)

        # The error message should reference whichever provider failed
        # last — the actual provider name is implementation-specific
        # but the error type should be the transient one
        assert "dead" in str(exc_info.value)

    def test_no_providers_registered_raises(self) -> None:
        pool = ProviderPoolV2("empty")

        with pytest.raises(ProviderUnavailableError):
            pool.execute(lambda name: "x")


# ════════════════════════════════════════════════════════════════
# Health report
# ════════════════════════════════════════════════════════════════
class TestHealthReport:
    def test_report_structure(self) -> None:
        pool = make_pool(n=2)
        pool.execute(lambda name: 1)

        report = pool.health_report()
        assert report["pool"] == "test_pool"
        assert report["strategy"] == "round_robin"
        assert "retry_on" in report
        assert isinstance(report["providers"], list)
        assert len(report["providers"]) == 2

        for p in report["providers"]:
            assert "name" in p
            assert "state" in p
            assert "calls" in p
            assert "failures" in p
            assert "success_rate" in p
            assert "avg_latency_ms" in p
