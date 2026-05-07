"""
core/quota_manager.py — VALUE / QEEMA v19.0 (NEW — CRITICAL)
=========================================================================
Hard quota enforcement for fixed-budget operation.

[Why this exists]
v18 had no quota awareness. If you blow through 150 Leonardo tokens
on episode 3, episodes 4-7 silently fall back to CSS (ugly).
If you blow through ElevenLabs credits, episodes fail mid-pipeline.

[Strategy]
- Track per-service usage in JSON state file (logs/quota_state.json)
- BEFORE expensive operations, estimate cost and check budget
- If exceeded: degrade gracefully OR refuse to start
- Persist across runs (state file survives restarts)

[Quotas configured for]
- 7 episodes/month plan
- ElevenLabs Starter (30k credits)
- Leonardo Free Trial (150 tokens) → upgrade path documented

[Failure modes]
- Insufficient budget for episode → return False from check_*()
- Caller raises QuotaExceededError → orchestrator skips episode gracefully
- Daily/monthly counters reset automatically
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from core.exceptions import PermanentError

logger = logging.getLogger(__name__)


class QuotaExceededError(PermanentError):
    """Raised when an operation would exceed configured quota."""
    def __init__(self, service: str, requested: int, available: int, **kw):
        super().__init__(
            f"Quota exceeded: {service} needs {requested}, only {available} available",
            **kw,
        )
        self.service = service
        self.requested = requested
        self.available = available


@dataclass
class QuotaConfig:
    """Configurable quotas. Defaults match Starter / Free Trial budgets."""
    # ElevenLabs Starter: 30,000 credits/month
    elevenlabs_monthly_credits: int = 30_000
    # Leonardo Free Trial: 150 tokens (one-time, no monthly reset)
    leonardo_total_tokens: int = 150
    # Anthropic — pay-as-you-go (we just track for cost reports)
    anthropic_monthly_budget_usd: float = 5.0
    # Number of episodes targeted per month
    episodes_target: int = 7
    # Buffer % to keep aside for retries/errors
    safety_buffer_percent: float = 0.10  # 10% buffer

    @property
    def elevenlabs_per_episode_budget(self) -> int:
        usable = int(self.elevenlabs_monthly_credits * (1 - self.safety_buffer_percent))
        return usable // max(self.episodes_target, 1)

    @property
    def leonardo_per_episode_budget(self) -> int:
        usable = int(self.leonardo_total_tokens * (1 - self.safety_buffer_percent))
        return usable // max(self.episodes_target, 1)


@dataclass
class QuotaState:
    """Persistent quota tracking state."""
    # Monthly counters (reset each calendar month)
    month_key: str = ""  # "YYYY-MM"
    elevenlabs_used_this_month: int = 0
    anthropic_spent_this_month_usd: float = 0.0
    episodes_completed_this_month: int = 0

    # Total counters (Leonardo free trial doesn't reset)
    leonardo_used_total: int = 0

    # Last-update tracking
    last_updated: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QuotaState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class QuotaManager:
    """
    Hard budget enforcement. Persistence + estimation + atomic counter updates.

    Usage:
        qm = QuotaManager(paths, config=QuotaConfig())

        # Before generating audio:
        if not qm.can_consume_elevenlabs(estimated_chars=3000):
            raise QuotaExceededError(...)

        # After successful audio generation:
        qm.consume_elevenlabs(actual_chars=2840)
    """

    def __init__(
        self,
        paths,  # PathsConfig
        config: Optional[QuotaConfig] = None,
    ) -> None:
        self._config = config or QuotaConfig()
        self._state_file = Path(paths.logs) / "quota_state.json"
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()
        self._maybe_reset_monthly()
        logger.info(
            f"💰 QuotaManager ready: target={self._config.episodes_target} eps/mo, "
            f"EL={self._state.elevenlabs_used_this_month}/{self._config.elevenlabs_monthly_credits}, "
            f"Leo={self._state.leonardo_used_total}/{self._config.leonardo_total_tokens}"
        )

    # ─── State persistence ───────────────────────────────────────
    def _load_state(self) -> QuotaState:
        if not self._state_file.exists():
            return QuotaState(month_key=self._current_month_key())
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return QuotaState.from_dict(data)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"⚠️ Could not load quota state ({e}) — starting fresh")
            return QuotaState(month_key=self._current_month_key())

    def _save_state(self) -> None:
        self._state.last_updated = time.time()
        try:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state.to_dict(), indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._state_file)
        except OSError as e:
            logger.error(f"❌ Could not persist quota state: {e}")

    @staticmethod
    def _current_month_key() -> str:
        return datetime.now().strftime("%Y-%m")

    def _maybe_reset_monthly(self) -> None:
        """Reset monthly counters if month changed."""
        current = self._current_month_key()
        if self._state.month_key != current:
            logger.info(
                f"🔄 New month {current} (was {self._state.month_key}) — "
                f"resetting monthly counters"
            )
            self._state.month_key = current
            self._state.elevenlabs_used_this_month = 0
            self._state.anthropic_spent_this_month_usd = 0.0
            self._state.episodes_completed_this_month = 0
            self._save_state()

    # ─── ElevenLabs ──────────────────────────────────────────────
    def elevenlabs_remaining(self) -> int:
        return max(0,
            self._config.elevenlabs_monthly_credits
            - self._state.elevenlabs_used_this_month
        )

    def can_consume_elevenlabs(self, estimated_chars: int) -> bool:
        """Check if we can afford this TTS call."""
        if estimated_chars <= 0:
            return True
        remaining = self.elevenlabs_remaining()
        # Reserve safety buffer
        usable = int(remaining * (1 - self._config.safety_buffer_percent))
        return estimated_chars <= usable

    def consume_elevenlabs(self, actual_chars: int) -> None:
        """Record actual ElevenLabs usage (after successful call)."""
        self._state.elevenlabs_used_this_month += max(0, actual_chars)
        self._save_state()

    # ─── Leonardo ────────────────────────────────────────────────
    def leonardo_remaining(self) -> int:
        return max(0,
            self._config.leonardo_total_tokens
            - self._state.leonardo_used_total
        )

    def can_consume_leonardo(self, estimated_tokens: int) -> bool:
        """Check if we can afford this image generation."""
        if estimated_tokens <= 0:
            return True
        return estimated_tokens <= self.leonardo_remaining()

    def consume_leonardo(self, actual_tokens: int) -> None:
        """Record Leonardo usage (after successful generation)."""
        self._state.leonardo_used_total += max(0, actual_tokens)
        self._save_state()

    # ─── Episode tracking ────────────────────────────────────────
    def episode_started(self) -> None:
        """Called when episode begins."""
        self._state.episodes_completed_this_month += 1
        self._save_state()

    def episodes_remaining_this_month(self) -> int:
        return max(0,
            self._config.episodes_target
            - self._state.episodes_completed_this_month
        )

    def can_start_episode(self) -> bool:
        """Pre-flight check: do we have enough quota to attempt an episode?"""
        # Hard requirement: ElevenLabs must have minimum budget
        # ~2000 chars is the absolute minimum for a usable episode
        el_min_required = 2000
        if self.elevenlabs_remaining() < el_min_required:
            logger.error(
                f"❌ Cannot start episode: ElevenLabs critically low "
                f"({self.elevenlabs_remaining()} < {el_min_required})"
            )
            return False

        # Soft requirement: warn if Leonardo low but allow episode
        # (CSS fallback will handle missing images)
        leo_per_ep = self._config.leonardo_per_episode_budget
        if leo_per_ep > 0 and self.leonardo_remaining() < leo_per_ep:
            logger.warning(
                f"⚠️ Leonardo quota low ({self.leonardo_remaining()} < {leo_per_ep}) "
                f"— episode will use CSS gradient fallback for some scenes"
            )
        return True

    # ─── Anthropic cost tracking ────────────────────────────────
    def consume_anthropic(self, cost_usd: float) -> None:
        self._state.anthropic_spent_this_month_usd += max(0.0, cost_usd)
        self._save_state()

    # ─── Reporting ───────────────────────────────────────────────
    def report(self) -> Dict[str, Any]:
        """Get current quota usage report."""
        return {
            "month": self._state.month_key,
            "episodes_completed": self._state.episodes_completed_this_month,
            "episodes_target": self._config.episodes_target,
            "episodes_remaining": self.episodes_remaining_this_month(),
            "elevenlabs": {
                "used": self._state.elevenlabs_used_this_month,
                "total": self._config.elevenlabs_monthly_credits,
                "remaining": self.elevenlabs_remaining(),
                "utilization_percent": round(
                    self._state.elevenlabs_used_this_month
                    / max(self._config.elevenlabs_monthly_credits, 1) * 100, 1
                ),
            },
            "leonardo": {
                "used": self._state.leonardo_used_total,
                "total": self._config.leonardo_total_tokens,
                "remaining": self.leonardo_remaining(),
                "utilization_percent": round(
                    self._state.leonardo_used_total
                    / max(self._config.leonardo_total_tokens, 1) * 100, 1
                ),
            },
            "anthropic_spent_usd": round(
                self._state.anthropic_spent_this_month_usd, 4
            ),
        }

    def print_report(self) -> None:
        """Print human-readable quota report."""
        r = self.report()
        print(f"\n═══ Quota Report ({r['month']}) ═══")
        print(f"Episodes: {r['episodes_completed']}/{r['episodes_target']} "
              f"(remaining: {r['episodes_remaining']})")
        el = r['elevenlabs']
        print(f"ElevenLabs: {el['used']:,}/{el['total']:,} chars "
              f"({el['utilization_percent']}% — {el['remaining']:,} remaining)")
        leo = r['leonardo']
        print(f"Leonardo:   {leo['used']}/{leo['total']} tokens "
              f"({leo['utilization_percent']}% — {leo['remaining']} remaining)")
        print(f"Anthropic:  ${r['anthropic_spent_usd']:.2f} spent\n")
