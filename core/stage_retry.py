"""
core/stage_retry.py — VALUE / QEEMA v22 (NEW)
=========================================================================
Per-stage retry logic with exponential backoff.

[Why this exists]
v21's pipeline.yml has shell-level retry × 3, but that means re-running
the ENTIRE episode pipeline. If only the audio stage fails, we wastefully
re-run script + tafsir + everything else.

This module adds INNER retry — each stage retries individually with
exponential backoff before giving up.

[Design]
  - Pure decorator pattern, no orchestrator changes needed
  - Configurable per stage (some stages should never retry, e.g., upload)
  - Distinguishes transient vs permanent errors
  - Records metrics on retries

[Retry policy]
  Default: 3 attempts, exponential backoff with jitter
    Attempt 1: immediate
    Attempt 2: wait 2s + jitter
    Attempt 3: wait 4s + jitter

  Permanent errors (NEVER retry):
    - QualityGateError (re-running won't help)
    - PermanentError (explicitly marked)
    - ValueError, TypeError (programming errors)

  Transient errors (DO retry):
    - TransientError (explicitly marked)
    - TimeoutError, ConnectionError
    - Generic Exception (default to transient)
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple, Type

from core.exceptions import (
    PermanentError,
    QualityGateError,
    TransientError,
)

logger = logging.getLogger(__name__)


# Errors that should NEVER be retried.
# These indicate programming errors or quality issues that won't resolve.
NEVER_RETRY: Tuple[Type[Exception], ...] = (
    QualityGateError,
    PermanentError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry policy for a single stage."""
    max_attempts: int = 3
    base_delay_sec: float = 2.0
    max_delay_sec: float = 30.0
    jitter_pct: float = 0.2  # ±20% random jitter on delay

    def compute_delay(self, attempt: int) -> float:
        """Compute delay before attempt N (1-indexed). attempt=1 → 0."""
        if attempt <= 1:
            return 0.0
        # Exponential backoff: base * 2^(attempt-2)
        delay = self.base_delay_sec * (2 ** (attempt - 2))
        delay = min(delay, self.max_delay_sec)
        # Add jitter
        jitter = delay * self.jitter_pct * (random.random() * 2 - 1)
        return max(0.1, delay + jitter)


# Pre-defined policies
POLICY_DEFAULT = RetryPolicy(max_attempts=3, base_delay_sec=2.0)
POLICY_AGGRESSIVE = RetryPolicy(max_attempts=5, base_delay_sec=1.0)
POLICY_CONSERVATIVE = RetryPolicy(max_attempts=2, base_delay_sec=5.0)
POLICY_NO_RETRY = RetryPolicy(max_attempts=1)

# Per-stage policy mapping (sensible defaults)
STAGE_POLICIES = {
    "script":            POLICY_DEFAULT,        # LLM hiccups happen
    "tafsir_validation": POLICY_DEFAULT,        # Claude API can flake
    "ai_images":         POLICY_AGGRESSIVE,     # Leonardo polling can timeout
    "audio":             POLICY_DEFAULT,        # ElevenLabs rate limits
    "audio_master":      POLICY_CONSERVATIVE,   # local ffmpeg — usually deterministic
    "render_scenes":     POLICY_CONSERVATIVE,   # browser pool — flaky retries waste time
    "concat_raw":        POLICY_CONSERVATIVE,
    "bgm_mix":           POLICY_CONSERVATIVE,
    "subtitles":         POLICY_NO_RETRY,       # optional anyway
    "wrap_branded":      POLICY_CONSERVATIVE,
    "thumbnail":         POLICY_DEFAULT,
    "upload":            POLICY_DEFAULT,        # network — but uploader has own retries
}


def get_policy(stage_name: str) -> RetryPolicy:
    """Get retry policy for a named stage. Falls back to default."""
    return STAGE_POLICIES.get(stage_name, POLICY_DEFAULT)


def should_retry(exc: Exception) -> bool:
    """Determine if an exception is retryable."""
    # Explicit non-retryable types
    if isinstance(exc, NEVER_RETRY):
        return False
    # Explicit retryable types
    if isinstance(exc, TransientError):
        return True
    # Common transient network errors
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # Default: assume transient (most pipeline failures are transient)
    return True


def run_with_retry(
    fn: Callable[[], Any],
    *,
    stage_name: str = "unknown",
    policy: Optional[RetryPolicy] = None,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> Any:
    """Run a callable with retry and exponential backoff.

    Args:
        fn: Zero-arg callable to execute.
        stage_name: For logging and metrics.
        policy: RetryPolicy override. If None, uses STAGE_POLICIES[stage_name].
        on_retry: Optional callback invoked before each retry (attempt, exc).

    Returns:
        Whatever fn returns on first success.

    Raises:
        Last exception if all attempts fail, or original exception if it's
        in NEVER_RETRY (no retries attempted in that case).
    """
    if policy is None:
        policy = get_policy(stage_name)

    last_exc: Optional[Exception] = None

    for attempt in range(1, policy.max_attempts + 1):
        # Sleep before attempt 2+
        if attempt > 1:
            delay = policy.compute_delay(attempt)
            logger.info(
                f"⏳ Retrying '{stage_name}' attempt {attempt}/"
                f"{policy.max_attempts} after {delay:.1f}s"
            )
            time.sleep(delay)

        try:
            result = fn()
            if attempt > 1:
                logger.info(
                    f"✅ '{stage_name}' succeeded on attempt {attempt}"
                )
            return result

        except Exception as exc:
            last_exc = exc

            # Check if we should retry
            if not should_retry(exc):
                logger.warning(
                    f"⛔ '{stage_name}' attempt {attempt} failed "
                    f"with non-retryable {type(exc).__name__}: {exc}"
                )
                raise

            # Last attempt? Don't retry — propagate
            if attempt >= policy.max_attempts:
                logger.error(
                    f"❌ '{stage_name}' exhausted {policy.max_attempts} "
                    f"attempts. Final error: {type(exc).__name__}: {exc}"
                )
                raise

            # Retry
            logger.warning(
                f"⚠️ '{stage_name}' attempt {attempt} failed "
                f"with {type(exc).__name__}: {exc}"
            )
            if on_retry:
                try:
                    on_retry(attempt, exc)
                except Exception as cb_exc:
                    logger.warning(f"⚠️ on_retry callback failed: {cb_exc}")

    # Defensive — should never reach here
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"run_with_retry exited unexpectedly for '{stage_name}'")


def with_retry(stage_name: str, policy: Optional[RetryPolicy] = None):
    """Decorator factory for retry.

    Example:
        @with_retry("audio")
        def generate_audio():
            ...
    """
    def decorator(fn: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            return run_with_retry(
                lambda: fn(*args, **kwargs),
                stage_name=stage_name,
                policy=policy,
            )
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return decorator
