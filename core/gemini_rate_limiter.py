"""
core/gemini_rate_limiter.py — VALUE / QEEMA v22.5 — per-key Gemini rate limiter
================================================================================

[Why this exists]
Gemini's free tier limits each API key to **5 requests per minute (RPM) per
project**. When multiple components in our pipeline call Gemini using the same
key — e.g. ScriptEngine + TafsirValidator both using key #1 in Phase 1 — they
can each hit Gemini at ~12s intervals (under their own limits) and STILL exceed
5 RPM combined.

This module solves that with a single global rate-limiter map: one
`KeyRateLimiter` per API key, shared by every consumer of that key.

[Strategy]
Token bucket with refill rate = 4 tokens/min and burst = 1.
- 4 RPM (not 5) leaves 20% safety margin for retries.
- burst=1 forbids parallel calls on the same key.
- `acquire()` blocks until a token is available, then consumes one.

[Usage]
    from core.gemini_rate_limiter import limiter_for_key
    limiter = limiter_for_key(my_api_key)
    limiter.acquire()  # blocks if needed; up to ~15s typical wait
    response = client.models.generate_content(...)

ScriptEngine, TafsirValidator's GeminiReviewer, the Phase 2 adapter, and
DeepVisualPromptGenerator all MUST go through this limiter when calling Gemini.

[Thread safety]
All operations are protected by a per-instance lock. Multiple threads calling
`acquire()` on the same limiter will be serialized correctly.

[Why a sliding window instead of token bucket]
A token bucket can permit a brief 2-call burst at minute boundaries. We use a
strict sliding window: track timestamps of recent calls, and block if there
have been ≥4 calls in the last 60 seconds.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque, Dict

logger = logging.getLogger(__name__)


# Conservative limit: 4 RPM (Gemini free tier ceiling = 5 RPM, we leave safety margin)
DEFAULT_MAX_REQUESTS_PER_MINUTE = 4
DEFAULT_WINDOW_SECONDS = 60.0


class KeyRateLimiter:
    """Sliding-window rate limiter for a single Gemini API key.

    Tracks the timestamps of the most recent N requests. If a new request would
    push the rate above the limit, blocks until the oldest in-window request is
    >60 seconds old, then proceeds.
    """

    def __init__(
        self,
        key_label: str,
        max_per_minute: int = DEFAULT_MAX_REQUESTS_PER_MINUTE,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self._label = key_label
        self._max = max_per_minute
        self._window = window_seconds
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, *, max_wait_seconds: float = 120.0) -> float:
        """Block until a request slot is available, then record this request.

        Returns the wait time (in seconds) actually slept. 0.0 means no wait.
        Raises TimeoutError if the wait would exceed max_wait_seconds.
        """
        total_waited = 0.0

        while True:
            with self._lock:
                now = time.monotonic()
                # Drop timestamps older than the window
                cutoff = now - self._window
                while self._timestamps and self._timestamps[0] < cutoff:
                    self._timestamps.popleft()

                if len(self._timestamps) < self._max:
                    # Slot available — record and proceed
                    self._timestamps.append(now)
                    if total_waited > 0:
                        logger.info(
                            f"⏳ Rate limiter [{self._label}]: waited "
                            f"{total_waited:.1f}s, now proceeding "
                            f"({len(self._timestamps)}/{self._max} in window)"
                        )
                    return total_waited

                # Compute how long until the oldest timestamp falls out of window
                oldest = self._timestamps[0]
                wait_for = max(0.0, (oldest + self._window) - now) + 0.5  # small buffer

            # Outside the lock — sleep
            if total_waited + wait_for > max_wait_seconds:
                raise TimeoutError(
                    f"Rate limiter [{self._label}] would wait > "
                    f"{max_wait_seconds}s; aborting"
                )
            logger.info(
                f"⏳ Rate limiter [{self._label}]: at limit "
                f"({self._max}/min), waiting {wait_for:.1f}s..."
            )
            time.sleep(wait_for)
            total_waited += wait_for

    def current_usage(self) -> int:
        """How many requests are in the current sliding window."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            return len(self._timestamps)


# ────────────────────────────────────────────────────────────────────────────
# Global registry — one limiter per API key
# ────────────────────────────────────────────────────────────────────────────
_LIMITERS: Dict[str, KeyRateLimiter] = {}
_REGISTRY_LOCK = threading.Lock()


def limiter_for_key(
    api_key: str,
    *,
    max_per_minute: int = DEFAULT_MAX_REQUESTS_PER_MINUTE,
    label_hint: str = "",
) -> KeyRateLimiter:
    """Get the singleton rate limiter for this API key.

    Multiple callers passing the same `api_key` get the same limiter instance,
    so a script_engine and a tafsir_validator using the same key automatically
    share the rate window.

    Args:
        api_key: The Gemini API key string. Used as the cache key.
        max_per_minute: 4 (default). Reduce only if you hit 429s in production.
        label_hint: Human-readable hint for logs (e.g. "phase1-script").
                   Only used the first time a key is registered.

    Returns:
        KeyRateLimiter shared across all callers using this key.
    """
    if not api_key:
        raise ValueError("limiter_for_key requires non-empty api_key")

    with _REGISTRY_LOCK:
        existing = _LIMITERS.get(api_key)
        if existing is not None:
            return existing

        # Build a label that doesn't leak the key. Use last 4 chars + hint.
        suffix = api_key[-4:] if len(api_key) >= 4 else "????"
        label = f"key-{suffix}" if not label_hint else f"{label_hint}-{suffix}"
        limiter = KeyRateLimiter(label, max_per_minute=max_per_minute)
        _LIMITERS[api_key] = limiter
        logger.info(
            f"📊 Rate limiter created [{label}]: "
            f"{max_per_minute} req/min sliding window"
        )
        return limiter


def reset_all_limiters() -> None:
    """Clear all registered limiters. ONLY for tests — never in production."""
    with _REGISTRY_LOCK:
        _LIMITERS.clear()
