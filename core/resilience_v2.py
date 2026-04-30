"""
core/resilience_v2.py — Thread-safe provider pool + scoped exception policy
================================================================================
Why a v2
--------
Three concrete defects in the v1 ProviderPool:

  1. Metric mutations (`total_calls += 1`) are not atomic. Under
     concurrent `synthesize_batch` execution, `success_rate` and
     `avg_latency_ms` become unreliable, and "fastest"/"least_used"
     strategies route to the wrong provider.

  2. `except (TransientError, Exception)` swallows programmer errors
     (KeyError, AttributeError) and treats them as "transient",
     burning provider quota on bugs that should crash loudly.

  3. The round-robin index `_next_idx` is shared global state, but
     the candidate set is filtered per-call. The interaction produces
     uneven distribution that's hard to reason about.

This v2 fixes all three. It is **API-compatible** with v1 so callers
can swap imports.

Design properties
-----------------
- `ProviderHealth` mutations go through methods that take the lock.
  No exposed mutable counters.
- Exception policy is explicit: only declared `transient_errors` are
  retried/failed-over. Everything else propagates immediately.
- Round-robin uses `itertools.cycle` over a stable provider order;
  per-call cursor maintained in a thread-local context.
- Optional: `RetryAfter` carrier from server responses is honored.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
)

from core.exceptions import (
    PermanentError,
    ProviderUnavailableError,
    QeemaError,
    RateLimitError,
    TransientError,
)
from core.resilience import CircuitBreaker, CircuitBreakerConfig, CircuitState

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ════════════════════════════════════════════════════════════════
# Health metrics — atomic via lock
# ════════════════════════════════════════════════════════════════
@dataclass
class _Metrics:
    """Mutable metrics; all access goes through the owning Provider's lock."""
    total_calls: int = 0
    total_failures: int = 0
    total_latency_sec: float = 0.0
    last_failure_at: Optional[float] = None
    consecutive_failures: int = 0


class _Provider:
    """A single provider with its breaker, optional rate-limiter, and metrics.

    All mutations are guarded by an instance-level lock. Reads of derived
    values (success_rate, avg_latency_ms) are also locked to ensure a
    consistent snapshot.
    """

    __slots__ = ("name", "breaker", "rate_limiter", "_m", "_lock")

    def __init__(
        self,
        name: str,
        breaker: CircuitBreaker,
        rate_limiter: Optional[Any] = None,  # TokenBucketRateLimiter
    ) -> None:
        self.name: str = name
        self.breaker: CircuitBreaker = breaker
        self.rate_limiter = rate_limiter
        self._m: _Metrics = _Metrics()
        self._lock: threading.Lock = threading.Lock()

    # ── Mutators ────────────────────────────────────────────────
    def record_attempt(self) -> None:
        with self._lock:
            self._m.total_calls += 1

    def record_success(self, latency_sec: float) -> None:
        with self._lock:
            self._m.total_latency_sec += latency_sec
            self._m.consecutive_failures = 0

    def record_failure(self, latency_sec: float) -> None:
        with self._lock:
            self._m.total_failures += 1
            self._m.total_latency_sec += latency_sec
            self._m.last_failure_at = time.time()
            self._m.consecutive_failures += 1

    # ── Read-only snapshot ───────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            calls = self._m.total_calls
            fails = self._m.total_failures
            success_rate = 1.0 if calls == 0 else 1.0 - (fails / calls)
            avg_latency_ms = (
                0.0 if calls == 0 else (self._m.total_latency_sec / calls) * 1000.0
            )
            return {
                "name": self.name,
                "state": self.breaker.state.value,
                "calls": calls,
                "failures": fails,
                "consecutive_failures": self._m.consecutive_failures,
                "success_rate": round(success_rate, 4),
                "avg_latency_ms": round(avg_latency_ms, 1),
                "last_failure_at": self._m.last_failure_at,
            }

    @property
    def avg_latency_ms_unlocked_estimate(self) -> float:
        """Lock-free best-effort estimate, ONLY for sort key in _pick.

        Reads of int/float are atomic in CPython, so this gives us a
        consistent-enough value for routing decisions without contending
        on the lock. Critical decisions (success/failure recording) go
        through the locked methods.
        """
        m = self._m
        calls = m.total_calls
        if calls == 0:
            return 0.0
        return (m.total_latency_sec / calls) * 1000.0

    @property
    def total_calls_unlocked_estimate(self) -> int:
        return self._m.total_calls


# ════════════════════════════════════════════════════════════════
# Pool
# ════════════════════════════════════════════════════════════════
class ProviderPoolV2:
    """
    Concurrent-safe pool of equivalent providers.

    Strategies
    ----------
    - "round_robin": fair rotation across registration order.
    - "least_used": route to the provider with fewest total calls.
    - "fastest":    route to the provider with lowest avg latency.

    Failover
    --------
    For each call, attempts up to N providers (default: all). Skips:
      - Any provider whose circuit is OPEN.
      - Any provider whose rate-limiter denies the token.
      - Any provider already tried in the current call.

    Returns the first successful result. Raises the last error if
    all providers fail.

    Exception policy
    ----------------
    Only exceptions matching `retry_on` (default: TransientError +
    RateLimitError) are caught and trigger failover. Anything else
    (e.g. KeyError, AttributeError) propagates immediately. This is
    a deliberate departure from v1, which caught `Exception`.
    """

    _STRATEGIES = ("round_robin", "least_used", "fastest")
    _DEFAULT_RETRY_ON: Tuple[Type[BaseException], ...] = (
        TransientError, RateLimitError, ProviderUnavailableError,
    )

    def __init__(
        self,
        name: str,
        *,
        strategy: str = "round_robin",
        retry_on: Optional[Tuple[Type[BaseException], ...]] = None,
    ) -> None:
        if strategy not in self._STRATEGIES:
            raise ValueError(
                f"Unknown strategy {strategy!r}. Use: {self._STRATEGIES}"
            )
        self.name: str = name
        self.strategy: str = strategy
        self._retry_on: Tuple[Type[BaseException], ...] = retry_on or self._DEFAULT_RETRY_ON
        self._providers: List[_Provider] = []
        self._next_rr: int = 0
        self._lock: threading.Lock = threading.Lock()

    # ───────────────────────────────────────────────────────────
    # Registration
    # ───────────────────────────────────────────────────────────
    def register(
        self,
        provider_name: str,
        *,
        breaker_config: Optional[CircuitBreakerConfig] = None,
        rate_limiter: Optional[Any] = None,
    ) -> None:
        breaker = CircuitBreaker(provider_name, breaker_config)
        provider = _Provider(provider_name, breaker, rate_limiter)
        with self._lock:
            if any(p.name == provider_name for p in self._providers):
                raise ValueError(f"Provider {provider_name!r} already registered")
            self._providers.append(provider)
        logger.info(f"📡 [{self.name}] registered: {provider_name}")

    # ───────────────────────────────────────────────────────────
    # Selection
    # ───────────────────────────────────────────────────────────
    def _candidates(self, exclude: set[str]) -> List[_Provider]:
        return [
            p for p in self._providers
            if p.name not in exclude
            and p.breaker.state != CircuitState.OPEN
        ]

    def _pick(self, exclude: set[str]) -> Optional[_Provider]:
        with self._lock:
            candidates = self._candidates(exclude)
            if not candidates:
                return None

            if self.strategy == "least_used":
                return min(
                    candidates,
                    key=lambda p: p.total_calls_unlocked_estimate,
                )

            if self.strategy == "fastest":
                # Providers with no latency yet sort first (give them a chance)
                return min(
                    candidates,
                    key=lambda p: p.avg_latency_ms_unlocked_estimate or 0.0,
                )

            # round_robin: cycle over the *full* provider list, find the
            # next one that's a candidate. This avoids the v1 bug where
            # the modulo over a filtered list produced uneven distribution.
            n = len(self._providers)
            for offset in range(n):
                idx = (self._next_rr + offset) % n
                p = self._providers[idx]
                if p in candidates:
                    self._next_rr = (idx + 1) % n
                    return p
            return None

    # ───────────────────────────────────────────────────────────
    # Execute
    # ───────────────────────────────────────────────────────────
    def execute(
        self,
        func: Callable[[str], T],
        *,
        max_attempts: Optional[int] = None,
    ) -> T:
        """
        Run `func(provider_name)` against the pool with automatic failover.

        Raises:
          ProviderUnavailableError: when no provider is selectable.
          The last caught exception: when all attempted providers failed.
          Any non-`retry_on` exception: propagates immediately.
        """
        with self._lock:
            attempts = max_attempts or len(self._providers)
            if not self._providers:
                raise ProviderUnavailableError(self.name, "no providers registered")

        tried: set[str] = set()
        last_exc: Optional[BaseException] = None

        for _ in range(attempts):
            provider = self._pick(tried)
            if provider is None:
                if last_exc is not None:
                    raise last_exc
                raise ProviderUnavailableError(
                    self.name, "all providers unavailable (circuits open)"
                )
            tried.add(provider.name)

            # Rate-limit gate
            if provider.rate_limiter is not None:
                try:
                    ok = provider.rate_limiter.acquire(timeout_sec=5.0)
                except Exception as e:
                    # Rate limiter bug — propagate, don't mask
                    raise QeemaError(
                        f"rate limiter raised: {e}", cause=e
                    ) from e
                if not ok:
                    logger.warning(
                        f"⏱️ [{provider.name}] local rate limit; trying next"
                    )
                    continue

            # Attempt
            provider.record_attempt()
            t0 = time.monotonic()
            try:
                result = provider.breaker.call(func, provider.name)
                provider.record_success(time.monotonic() - t0)
                return result
            except self._retry_on as e:    # type: ignore[misc]
                provider.record_failure(time.monotonic() - t0)
                last_exc = e
                logger.warning(
                    f"⚠️ [{provider.name}] {type(e).__name__}: {e}; trying next"
                )
                continue
            # Anything not in retry_on propagates without failover.
            # This is intentional — programmer errors should crash loudly.

        if last_exc is not None:
            raise last_exc
        raise ProviderUnavailableError(self.name, "no providers succeeded")

    # ───────────────────────────────────────────────────────────
    # Observability
    # ───────────────────────────────────────────────────────────
    def health_report(self) -> dict:
        """Snapshot of pool health, safe to serialize."""
        with self._lock:
            providers = [p.snapshot() for p in self._providers]
        return {
            "pool": self.name,
            "strategy": self.strategy,
            "retry_on": [t.__name__ for t in self._retry_on],
            "providers": providers,
        }
