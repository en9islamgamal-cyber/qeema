"""
core/resilience.py — VALUE / QEEMA v12.0 (High-Performance Production)
======================================================================
Advanced Resilience Patterns:
  ✅ Async-Aware Smart Retry (Exponential Backoff + Jitter)
  ✅ State-Machine Circuit Breaker (OPEN, CLOSED, HALF_OPEN)
  ✅ Non-Blocking Token Bucket Rate Limiter
  ✅ Async Provider Pool (Failover & Load Balancing)
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, List, Set

from core.exceptions import (
    PermanentError,
    ProviderUnavailableError,
    RateLimitError,
    TransientError,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")

# ════════════════════════════════════════════════════════════════
# 1. High-Performance Async Retry Decorator
# ════════════════════════════════════════════════════════════════
@dataclass
class RetryConfig:
    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True
    retry_on: tuple = (TransientError, ConnectionError, asyncio.TimeoutError)
    skip_on: tuple = (PermanentError,)

def retry_with_backoff(config: Optional[RetryConfig] = None):
    """
    مُزخرف (Decorator) ذكي يدعم العمليات التزامنية وغير التزامنية.
    يستخدم خوارزمية Full Jitter لمنع ظاهرة 'Thundering Herd'.
    """
    cfg = config or RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            last_exc: Optional[Exception] = None
            for attempt in range(1, cfg.max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except cfg.skip_on as e:
                    logger.error(f"❌ {func.__name__}: Permanent failure: {e}")
                    raise
                except cfg.retry_on as e:
                    last_exc = e
                    if attempt == cfg.max_attempts: break
                    
                    # حساب التأخير باستخدام Exponential Backoff + Jitter
                    delay = min(cfg.max_delay, cfg.initial_delay * (cfg.exponential_base ** (attempt - 1)))
                    if cfg.jitter:
                        delay = random.uniform(cfg.initial_delay, delay)
                    
                    logger.warning(f"⚠️ {func.__name__}: Attempt {attempt} failed. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay) # NON-BLOCKING SLEEP
            
            raise last_exc

        return async_wrapper # نركز هنا على الـ Async كمعيار للمشروع
    return decorator

# ════════════════════════════════════════════════════════════════
# 2. State-Machine Circuit Breaker
# ════════════════════════════════════════════════════════════════
class CircuitState(Enum):
    CLOSED = "closed"      # يعمل طبيعياً
    OPEN = "open"          # معطل (لحماية النظام)
    HALF_OPEN = "half_open" # وضع الاختبار



class CircuitBreaker:
    """
    قاطع الدائرة البرمجي: يمنع استنزاف الموارد عند تعطل API معين.
    """
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._last_failure_time = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time > self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    logger.info(f"🔄 [{self.name}] Circuit HALF-OPEN: Testing service...")
                else:
                    raise ProviderUnavailableError(self.name, "Circuit is OPEN")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        async with self._lock:
            if exc_type is None: # نجاح
                self._failures = 0
                self._state = CircuitState.CLOSED
            elif issubclass(exc_type, (TransientError, ConnectionError)):
                self._failures += 1
                self._last_failure_time = time.monotonic()
                if self._failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.critical(f"🚨 [{self.name}] Circuit OPENED! Too many failures.")

# ════════════════════════════════════════════════════════════════
# 3. Async Token Bucket (The Governor)
# ════════════════════════════════════════════════════════════════
class TokenBucketRateLimiter:
    """
    خوارزمية إدارة الكوتة (Token Bucket).
    تسمح بـ Bursts مؤقتة مع الحفاظ على معدل ثابت (Steady Rate).
    """
    def __init__(self, rate_per_min: float, capacity: int):
        self.rate = rate_per_min / 60.0
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: float = 1.0):
        async with self._lock:
            now = time.monotonic()
            # Refill tokens
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens < amount:
                wait_time = (amount - self.tokens) / self.rate
                logger.debug(f"⏳ Throttling: waiting {wait_time:.2f}s for tokens")
                await asyncio.sleep(wait_time)
                # إعادة الملء بعد الانتظار
                self.tokens = amount 
            
            self.tokens -= amount
            return True



# ════════════════════════════════════════════════════════════════
# 4. Enterprise Provider Pool (The Bulkhead)
# ════════════════════════════════════════════════════════════════
class ProviderPool:
    """
    مجمع المزودين: يدير استراتيجيات الـ Failover والـ Load Balancing.
    لو سقط Gemini 1، ينتقل تلقائياً لـ Gemini 2 أو Groq.
    """
    def __init__(self, name: str):
        self.name = name
        self.providers: List[Dict] = []
        self._current_idx = 0

    def add_provider(self, name: str, adapter: Any, rpm: int = 30):
        self.providers.append({
            "name": name,
            "adapter": adapter,
            "breaker": CircuitBreaker(name),
            "limiter": TokenBucketRateLimiter(rpm, capacity=int(rpm*0.2 + 1))
        })

    async def execute(self, task_fn: Callable, *args, **kwargs):
        """
        خوارزمية التنفيذ التكيفي:
        تجرب كافة المزودين المتاحين قبل إعلان الفشل النهائي.
        """
        last_error = None
        # نحاول مع كل المزودين (Round Robin Failover)
        for _ in range(len(self.providers)):
            p = self.providers[self._current_idx]
            self._current_idx = (self._current_idx + 1) % len(self.providers)

            try:
                async with p["breaker"]:
                    await p["limiter"].acquire()
                    return await task_fn(p["adapter"], *args, **kwargs)
            except (ProviderUnavailableError, RateLimitError) as e:
                last_error = e
                continue # جرب المزود التالي فوراً
            except Exception as e:
                last_error = e
                logger.error(f"❌ Provider [{p['name']}] failed: {e}")
                continue

        raise ProviderUnavailableError(self.name, f"All providers exhausted. Last error: {last_error}")
