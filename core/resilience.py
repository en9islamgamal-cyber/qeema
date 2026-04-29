"""
core/resilience.py — VALUE / QEEMA v11.0 (Production)
=========================================================
Resilience patterns for distributed systems.

[Patterns Implemented]
1. RetryPolicy + retry_with_backoff — exponential + jitter
2. CircuitBreaker                   — three-state (CLOSED/OPEN/HALF_OPEN)
3. TokenBucketRateLimiter           — burstable rate limiting
4. ProviderPool                     — health-aware routing across providers

[Why These?]
- Pipeline depends on 5+ external services (LLM × N, TTS × 2, Quran CDN × 4,
  YouTube). Any can fail or rate-limit. We isolate failures (Bulkhead) and
  trip circuits to stop wasting quota on dead services.
- Round-robin alone is bad: if provider #1 is broken, we keep retrying it.
  Circuit breaker fixes this.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any, Callable, Iterator, List, Optional, Tuple, TypeVar

from core.exceptions import (
    PermanentError,
    ProviderUnavailableError,
    RateLimitError,
    TransientError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ════════════════════════════════════════════════════════════════
# 1. Retry with exponential backoff + jitter
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RetryPolicy:
    """
    Retry configuration.

    [Why jitter?] If 100 clients all retry at exactly t+1s after a shared
    failure, they create a thundering herd. Jitter spreads the retries.
    """
    max_attempts: int = 3
    initial_delay_sec: float = 1.0
    max_delay_sec: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True
    retry_on: Tuple[type, ...] = (TransientError,)
    skip_on: Tuple[type, ...] = (PermanentError,)


def _compute_delay(policy: RetryPolicy, attempt: int, server_hint: Optional[float]) -> float:
    """
    Calculate next retry delay.
    Honors server-provided Retry-After if present (most accurate).
    Otherwise: min(initial * base^(attempt-1), max) ± jitter.
    """
    if server_hint is not None and server_hint > 0:
        return min(server_hint, policy.max_delay_sec)

    delay = min(
        policy.initial_delay_sec * (policy.exponential_base ** (attempt - 1)),
        policy.max_delay_sec,
    )
    if policy.jitter:
        # Decorrelated jitter: 50%-150% of computed delay
        delay *= 0.5 + random.random()
    return delay


def retry_with_backoff(policy: Optional[RetryPolicy] = None) -> Callable:
    """
    Decorator: smart retry with exponential backoff + jitter.

    Works on both sync and async functions.

    Example:
        @retry_with_backoff(RetryPolicy(max_attempts=5))
        def fetch_data():
            ...
    """
    p = policy or RetryPolicy()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        if asyncio.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> T:
                last_exc: Optional[BaseException] = None
                for attempt in range(1, p.max_attempts + 1):
                    try:
                        return await func(*args, **kwargs)
                    except p.skip_on as e:
                        logger.error(
                            f"❌ {func.__name__}: permanent error (no retry): {e}"
                        )
                        raise
                    except p.retry_on as e:
                        last_exc = e
                        if attempt == p.max_attempts:
                            break
                        hint = getattr(e, "retry_after", None)
                        delay = _compute_delay(p, attempt, hint)
                        logger.warning(
                            f"⚠️ {func.__name__}: attempt {attempt}/{p.max_attempts} "
                            f"failed ({type(e).__name__}); retry in {delay:.2f}s"
                        )
                        await asyncio.sleep(delay)
                assert last_exc is not None
                raise last_exc
            return async_wrapper  # type: ignore[return-value]

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: Optional[BaseException] = None
            for attempt in range(1, p.max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except p.skip_on as e:
                    logger.error(
                        f"❌ {func.__name__}: permanent error (no retry): {e}"
                    )
                    raise
                except p.retry_on as e:
                    last_exc = e
                    if attempt == p.max_attempts:
                        break
                    hint = getattr(e, "retry_after", None)
                    delay = _compute_delay(p, attempt, hint)
                    logger.warning(
                        f"⚠️ {func.__name__}: attempt {attempt}/{p.max_attempts} "
                        f"failed ({type(e).__name__}); retry in {delay:.2f}s"
                    )
                    time.sleep(delay)
            assert last_exc is not None
            raise last_exc

        return sync_wrapper

    return decorator


# ════════════════════════════════════════════════════════════════
# 2. Circuit Breaker (three-state)
# ════════════════════════════════════════════════════════════════
class CircuitState(str, Enum):
    CLOSED = "closed"        # normal operation
    OPEN = "open"            # all calls rejected
    HALF_OPEN = "half_open"  # probing recovery


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout_sec: float = 60.0
    success_threshold: int = 2  # consecutive successes in HALF_OPEN to close


class CircuitBreaker:
    """
    Per-provider circuit breaker.

    [State machine]
        CLOSED -- failures >= threshold --> OPEN
        OPEN   -- recovery_timeout elapsed --> HALF_OPEN
        HALF_OPEN -- success_threshold met --> CLOSED
        HALF_OPEN -- any failure --> OPEN

    [Why?] When ElevenLabs goes down, we don't want each request waiting 90s
    for timeout × 3 retries. Breaker opens after 5 failures, rejects instantly
    for 60s, then probes once.
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> None:
        self.name: str = name
        self._cfg: CircuitBreakerConfig = config or CircuitBreakerConfig()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0
        self._opened_at: Optional[float] = None
        self._lock: threading.Lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """Current state. Auto-transitions OPEN → HALF_OPEN on access."""
        with self._lock:
            if (
                self._state == CircuitState.OPEN
                and self._opened_at is not None
                and (time.time() - self._opened_at) >= self._cfg.recovery_timeout_sec
            ):
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.info(
                    f"🔄 [{self.name}] Circuit: OPEN → HALF_OPEN (probing recovery)"
                )
            return self._state

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._cfg.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._opened_at = None
                    logger.info(
                        f"✅ [{self.name}] Circuit: HALF_OPEN → CLOSED (recovered)"
                    )
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed, reopen immediately
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                logger.warning(
                    f"🚨 [{self.name}] Circuit: HALF_OPEN → OPEN (probe failed)"
                )
            elif (
                self._state == CircuitState.CLOSED
                and self._failure_count >= self._cfg.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                logger.error(
                    f"🚨 [{self.name}] Circuit: CLOSED → OPEN "
                    f"(consecutive failures={self._failure_count})"
                )

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """
        Execute func through breaker.
        Raises ProviderUnavailableError if state == OPEN.
        """
        if self.state == CircuitState.OPEN:
            assert self._opened_at is not None
            time_left = (self._opened_at + self._cfg.recovery_timeout_sec) - time.time()
            raise ProviderUnavailableError(
                self.name,
                f"circuit OPEN (try again in {max(0.0, time_left):.0f}s)",
            )
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except (TransientError, ConnectionError, OSError) as e:
            self.record_failure()
            raise
        except Exception:
            # Permanent errors don't trip the breaker (they need manual fix)
            raise


# ════════════════════════════════════════════════════════════════
# 3. Token Bucket Rate Limiter
# ════════════════════════════════════════════════════════════════
class TokenBucketRateLimiter:
    """
    Token bucket: refills at `rate` tokens/sec, max capacity `burst`.

    [Why token bucket?]
    - Allows bursts up to `burst` tokens (handles micro-spikes)
    - Smooth average rate enforcement
    - Simple, lock-fast implementation
    """

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        if rate_per_sec <= 0 or burst <= 0:
            raise ValueError("rate_per_sec and burst must be positive")
        self._rate: float = rate_per_sec
        self._capacity: int = burst
        self._tokens: float = float(burst)
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    def acquire(self, tokens: int = 1, timeout_sec: float = 30.0) -> bool:
        """
        Acquire `tokens`. Blocks up to `timeout_sec`.
        Returns False if timed out before tokens available.
        """
        if tokens > self._capacity:
            raise ValueError(
                f"Requested {tokens} tokens but capacity is only {self._capacity}"
            )
        deadline = time.monotonic() + timeout_sec
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self._capacity), self._tokens + elapsed * self._rate
                )
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True

                wait_for = (tokens - self._tokens) / self._rate

            if time.monotonic() + wait_for > deadline:
                return False
            time.sleep(min(wait_for, 0.5))


# ════════════════════════════════════════════════════════════════
# 4. Provider Pool (health-aware routing)
# ════════════════════════════════════════════════════════════════
@dataclass
class ProviderHealth:
    """Tracks runtime health metrics for a single provider."""
    name: str
    breaker: CircuitBreaker
    rate_limiter: Optional[TokenBucketRateLimiter] = None
    total_calls: int = 0
    total_failures: int = 0
    total_latency_sec: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return 1.0 - (self.total_failures / self.total_calls)

    @property
    def avg_latency_ms(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return (self.total_latency_sec / self.total_calls) * 1000.0


class ProviderPool:
    """
    Pool of equivalent providers (e.g., 3 Gemini API keys, or
    [ElevenLabs, Google TTS]).

    [Strategies]
    - "round_robin": classic, fair distribution
    - "least_used":  send next call to least-busy provider
    - "fastest":     send next call to lowest-latency provider

    [Behavior]
    - Skips providers whose breaker is OPEN
    - Honors per-provider rate limits
    - Returns first successful result, or raises last error
    """

    _STRATEGIES = ("round_robin", "least_used", "fastest")

    def __init__(self, name: str, strategy: str = "round_robin") -> None:
        if strategy not in self._STRATEGIES:
            raise ValueError(
                f"Unknown strategy '{strategy}'. Use: {self._STRATEGIES}"
            )
        self.name: str = name
        self.strategy: str = strategy
        self._providers: List[ProviderHealth] = []
        self._next_idx: int = 0
        self._lock: threading.Lock = threading.Lock()

    def register(
        self,
        name: str,
        *,
        breaker_config: Optional[CircuitBreakerConfig] = None,
        rate_limit: Optional[Tuple[float, int]] = None,
    ) -> None:
        """Register a provider. rate_limit=(rate/sec, burst)."""
        breaker = CircuitBreaker(name, breaker_config)
        limiter = (
            TokenBucketRateLimiter(*rate_limit) if rate_limit else None
        )
        with self._lock:
            self._providers.append(ProviderHealth(name, breaker, limiter))
        logger.info(f"📡 [{self.name}] registered provider: {name}")

    def _available(self) -> List[ProviderHealth]:
        """Providers whose circuit is not fully open."""
        return [
            p for p in self._providers
            if p.breaker.state != CircuitState.OPEN
        ]

    def _pick(self, exclude: Iterator[str]) -> Optional[ProviderHealth]:
        """Pick a provider not in `exclude` based on strategy."""
        excluded = set(exclude)
        with self._lock:
            candidates = [
                p for p in self._available() if p.name not in excluded
            ]
            if not candidates:
                return None

            if self.strategy == "least_used":
                return min(candidates, key=lambda p: p.total_calls)
            if self.strategy == "fastest":
                return min(
                    candidates,
                    key=lambda p: p.avg_latency_ms or float("inf"),
                )
            # round_robin
            self._next_idx = (self._next_idx + 1) % max(len(candidates), 1)
            return candidates[self._next_idx % len(candidates)]

    def execute(
        self,
        func: Callable[[str], T],
        *,
        max_attempts: Optional[int] = None,
    ) -> T:
        """
        Execute func across pool with automatic failover.
        `func` receives the provider name and must return a result.
        """
        attempts = max_attempts or len(self._providers)
        last_exc: Optional[BaseException] = None
        tried: set[str] = set()

        for _ in range(attempts):
            provider = self._pick(iter(tried))
            if provider is None:
                # All providers tried or all circuits open
                if last_exc is not None:
                    raise last_exc
                raise ProviderUnavailableError(
                    self.name, "all providers unavailable (circuits open)"
                )
            tried.add(provider.name)

            # Rate limit gate
            if provider.rate_limiter and not provider.rate_limiter.acquire(timeout_sec=5.0):
                logger.warning(
                    f"⏱️ [{provider.name}] local rate limit exceeded; trying next"
                )
                continue

            start = time.monotonic()
            provider.total_calls += 1
            try:
                result = provider.breaker.call(func, provider.name)
                provider.total_latency_sec += time.monotonic() - start
                return result
            except RateLimitError as e:
                provider.total_failures += 1
                last_exc = e
                logger.warning(
                    f"⏱️ [{provider.name}] rate-limited by server: {e}"
                )
            except ProviderUnavailableError as e:
                provider.total_failures += 1
                last_exc = e
                logger.warning(f"🚧 [{provider.name}] {e}")
            except (TransientError, Exception) as e:
                provider.total_failures += 1
                last_exc = e
                logger.warning(
                    f"⚠️ [{provider.name}] failed ({type(e).__name__}): {e}; "
                    f"trying next"
                )

        if last_exc is not None:
            raise last_exc
        raise ProviderUnavailableError(self.name, "no providers succeeded")

    def health_report(self) -> dict:
        """Snapshot of current pool health (for monitoring)."""
        with self._lock:
            return {
                "pool": self.name,
                "strategy": self.strategy,
                "providers": [
                    {
                        "name": p.name,
                        "state": p.breaker.state.value,
                        "calls": p.total_calls,
                        "failures": p.total_failures,
                        "success_rate": round(p.success_rate, 3),
                        "avg_latency_ms": round(p.avg_latency_ms, 1),
                    }
                    for p in self._providers
                ],
            }
