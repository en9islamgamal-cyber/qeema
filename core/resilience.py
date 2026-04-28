"""
core/resilience.py — VALUE / QEEMA v11.0 (Production)
=======================================================
Resilience patterns for distributed systems:
  - Smart retry with exponential backoff + jitter
  - Circuit breaker (prevent cascading failures)
  - Rate limiter (token bucket)
  - Bulkhead (isolation between providers)

Why:
- النظام بيتعامل مع 5+ APIs خارجية (Gemini, Groq, ElevenLabs, Quran CDNs, YouTube)
- أي API منهم ممكن يقع → لازم نعزل التأثير (Bulkhead)
- النداءات المتكررة الفاشلة بتاكل quota → Circuit Breaker
- بعض الـ APIs ليها rate limits صارمة → Rate Limiter
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from threading import Lock
from typing import Any, Callable, Optional, TypeVar

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
@dataclass
class RetryConfig:
    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True
    retry_on: tuple = (TransientError,)  # only retry these
    skip_on: tuple = (PermanentError,)   # never retry these


def retry_with_backoff(config: Optional[RetryConfig] = None):
    """
    Decorator: smart retry with exponential backoff + jitter.

    Why jitter? لو 100 client بيـ retry في نفس الوقت بعد فشل APIcommon
    بيعملوا "thundering herd". الـ jitter بيوزّع الـ retries.
    """
    cfg = config or RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Optional[Exception] = None
            for attempt in range(1, cfg.max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except cfg.skip_on as e:
                    logger.error(f"❌ {func.__name__}: permanent error, no retry: {e}")
                    raise
                except cfg.retry_on as e:
                    last_exc = e
                    if attempt == cfg.max_attempts:
                        break

                    # Honor server's Retry-After header if available
                    server_delay = getattr(e, "retry_after", None)
                    if server_delay is not None:
                        delay = min(server_delay, cfg.max_delay)
                    else:
                        delay = min(
                            cfg.initial_delay * (cfg.exponential_base ** (attempt - 1)),
                            cfg.max_delay,
                        )
                        if cfg.jitter:
                            delay *= 0.5 + random.random()  # 50%-150% of computed

                    logger.warning(
                        f"⚠️ {func.__name__}: attempt {attempt}/{cfg.max_attempts} failed "
                        f"({type(e).__name__}: {e}). Retry in {delay:.1f}s"
                    )
                    time.sleep(delay)
            assert last_exc is not None
            raise last_exc

        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            last_exc: Optional[Exception] = None
            for attempt in range(1, cfg.max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except cfg.skip_on as e:
                    logger.error(f"❌ {func.__name__}: permanent error, no retry: {e}")
                    raise
                except cfg.retry_on as e:
                    last_exc = e
                    if attempt == cfg.max_attempts:
                        break
                    server_delay = getattr(e, "retry_after", None)
                    delay = (
                        min(server_delay, cfg.max_delay)
                        if server_delay is not None
                        else min(
                            cfg.initial_delay * (cfg.exponential_base ** (attempt - 1)),
                            cfg.max_delay,
                        )
                    )
                    if cfg.jitter and server_delay is None:
                        delay *= 0.5 + random.random()
                    logger.warning(
                        f"⚠️ {func.__name__}: attempt {attempt}/{cfg.max_attempts} → wait {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
            assert last_exc is not None
            raise last_exc

        # Pick sync vs async based on coroutine
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return wrapper

    return decorator


# ════════════════════════════════════════════════════════════════
# 2. Circuit Breaker
# ════════════════════════════════════════════════════════════════
class CircuitState(Enum):
    CLOSED = "closed"        # طبيعي، النداءات بتمر
    OPEN = "open"            # كل النداءات مرفوضة (الخدمة مكسورة)
    HALF_OPEN = "half_open"  # محاولة استكشاف لو الخدمة رجعت


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5            # بعد كام فشل نقفل الدائرة
    recovery_timeout: float = 60.0        # كام ثانية ننتظر قبل الـ HALF_OPEN
    success_threshold: int = 2            # كام نجاح متتالي في HALF_OPEN لنرجع CLOSED


class CircuitBreaker:
    """
    Per-provider circuit breaker.

    Why: لو ElevenLabs وقع، مفيش لازمة نضيع 3 retries × 30 ثانية لكل request جديد.
    الـ breaker بيرفض النداء فوراً ويرجع fallback.
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.cfg = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: Optional[float] = None
        self._lock = Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            # Auto-transition OPEN → HALF_OPEN after timeout
            if (
                self._state == CircuitState.OPEN
                and self._opened_at is not None
                and time.time() - self._opened_at >= self.cfg.recovery_timeout
            ):
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.info(f"🔄 [{self.name}] Circuit: OPEN → HALF_OPEN (probing)")
            return self._state

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.cfg.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(f"✅ [{self.name}] Circuit: HALF_OPEN → CLOSED (recovered)")
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                logger.warning(f"🚨 [{self.name}] Circuit: HALF_OPEN → OPEN (probe failed)")
            elif (
                self._state == CircuitState.CLOSED
                and self._failure_count >= self.cfg.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                logger.error(
                    f"🚨 [{self.name}] Circuit: CLOSED → OPEN "
                    f"(failures={self._failure_count})"
                )

    def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        if self.state == CircuitState.OPEN:
            raise ProviderUnavailableError(
                self.name,
                f"Circuit breaker OPEN; will retry at {self._opened_at + self.cfg.recovery_timeout}",
            )
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            # Only count transient errors as circuit failures
            if isinstance(e, (TransientError, ConnectionError, TimeoutError)):
                self.record_failure()
            raise


# ════════════════════════════════════════════════════════════════
# 3. Rate Limiter (Token Bucket)
# ════════════════════════════════════════════════════════════════
class TokenBucketRateLimiter:
    """
    Token bucket: rate=N tokens/sec, burst=B.

    Why token bucket vs sliding window?
    - بيسمح بـ bursts مؤقتة (لو الـ APIs بطيئة في وقت تاني نقدر نلحقهم)
    - أبسط في الكود وأسرع
    """

    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = rate_per_sec
        self.capacity = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = Lock()

    def acquire(self, tokens: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                # Refill
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True

                # How long until enough tokens?
                wait_for = (tokens - self._tokens) / self.rate

            if time.monotonic() + wait_for > deadline:
                return False
            time.sleep(min(wait_for, 0.5))


# ════════════════════════════════════════════════════════════════
# 4. Provider Pool with health tracking (Bulkhead)
# ════════════════════════════════════════════════════════════════
@dataclass
class ProviderHealth:
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
        return (self.total_latency_sec / self.total_calls) * 1000


class ProviderPool:
    """
    Pool of equivalent providers (e.g., 3 Gemini keys).
    Routes traffic with awareness of health & rate limits.

    Strategies:
    - "round_robin": الافتراضي
    - "least_used": أقل استخدام
    - "fastest": أسرع response time
    """

    def __init__(self, name: str, strategy: str = "round_robin"):
        self.name = name
        self.strategy = strategy
        self._providers: list[ProviderHealth] = []
        self._next_idx = 0
        self._lock = Lock()

    def register(
        self,
        name: str,
        breaker_config: Optional[CircuitBreakerConfig] = None,
        rate_limit: Optional[tuple[float, int]] = None,  # (rate/sec, burst)
    ) -> None:
        breaker = CircuitBreaker(name, breaker_config)
        limiter = TokenBucketRateLimiter(*rate_limit) if rate_limit else None
        with self._lock:
            self._providers.append(ProviderHealth(name, breaker, limiter))
        logger.info(f"📡 [{self.name}] Registered provider: {name}")

    def _pick_provider(self) -> Optional[ProviderHealth]:
        with self._lock:
            available = [
                p for p in self._providers
                if p.breaker.state != CircuitState.OPEN
            ]
            if not available:
                return None

            if self.strategy == "least_used":
                return min(available, key=lambda p: p.total_calls)
            if self.strategy == "fastest":
                return min(available, key=lambda p: p.avg_latency_ms or float("inf"))

            # round_robin
            self._next_idx = (self._next_idx + 1) % len(available)
            return available[self._next_idx]

    def execute(
        self,
        func: Callable[[str], T],
        max_attempts: Optional[int] = None,
    ) -> T:
        """
        Execute func across pool with automatic failover.
        func receives provider name and returns result.
        """
        attempts = max_attempts or len(self._providers)
        last_exc: Optional[Exception] = None
        tried: set[str] = set()

        for _ in range(attempts):
            provider = self._pick_provider()
            if provider is None:
                raise ProviderUnavailableError(
                    self.name, "all providers unavailable (circuits open)"
                )
            if provider.name in tried:
                continue
            tried.add(provider.name)

            # Rate limit gate
            if provider.rate_limiter and not provider.rate_limiter.acquire(timeout=5.0):
                logger.warning(f"⏱️ [{provider.name}] rate-limited, trying next")
                continue

            start = time.monotonic()
            provider.total_calls += 1
            try:
                result = provider.breaker.call(func, provider.name)
                provider.total_latency_sec += time.monotonic() - start
                return result
            except RateLimitError as e:
                provider.total_failures += 1
                logger.warning(f"⏱️ [{provider.name}] rate-limited by server: {e}")
                last_exc = e
            except (TransientError, Exception) as e:
                provider.total_failures += 1
                last_exc = e
                logger.warning(f"⚠️ [{provider.name}] failed: {e}, trying next")

        if last_exc:
            raise last_exc
        raise ProviderUnavailableError(self.name, "no providers succeeded")

    def health_report(self) -> dict:
        with self._lock:
            return {
                "pool": self.name,
                "providers": [
                    {
                        "name": p.name,
                        "state": p.breaker.state.value,
                        "calls": p.total_calls,
                        "success_rate": round(p.success_rate, 3),
                        "avg_latency_ms": round(p.avg_latency_ms, 1),
                    }
                    for p in self._providers
                ],
            }
