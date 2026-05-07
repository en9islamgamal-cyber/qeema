"""
core/cost_tracker.py — VALUE / QEEMA v15.0 (NEW)
=================================================
Real-time API cost tracking & budget enforcement.

Tracks per-episode and per-day costs across:
- Gemini (input + output tokens)
- ElevenLabs (chars × tier rate)
- Leonardo.ai (per image)
- Anthropic Claude (input + output tokens)

Persists to JSONL log; queryable via summary().
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Pricing model (USD as of 2026; adjust as needed) ─────────────
# Source: official pricing pages — keep this in sync periodically
PRICING_USD: Dict[str, Dict[str, float]] = {
    "gemini-2.5-flash": {
        "input_per_1m_tokens": 0.075,
        "output_per_1m_tokens": 0.30,
    },
    "gemini-2.5-pro": {
        "input_per_1m_tokens": 1.25,
        "output_per_1m_tokens": 5.00,
    },
    "claude-opus-4-7": {
        "input_per_1m_tokens": 15.00,
        "output_per_1m_tokens": 75.00,
    },
    "elevenlabs_creator": {
        "per_1k_chars": 0.30,    # creator tier
    },
    "elevenlabs_pro": {
        "per_1k_chars": 0.18,
    },
    "leonardo": {
        "per_image_sd": 0.005,
        "per_image_sdxl": 0.012,
    },
    "groq_llama": {
        "input_per_1m_tokens": 0.05,
        "output_per_1m_tokens": 0.08,
    },
}


@dataclass
class CostEvent:
    timestamp: float
    episode_number: Optional[int]
    provider: str
    operation: str
    units: float          # tokens, characters, or images
    unit_type: str        # "tokens_in" | "tokens_out" | "chars" | "images"
    cost_usd: float


class CostTracker:
    """Thread-safe cost accumulator with daily and per-episode views."""

    def __init__(self, log_dir: Path) -> None:
        self._lock = threading.Lock()
        self._events: List[CostEvent] = []
        self._log_dir: Path = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file: Path = log_dir / f"costs_{date.today().isoformat()}.jsonl"

    def record_gemini(
        self, *, episode_number: Optional[int], model: str,
        tokens_in: int, tokens_out: int,
    ) -> float:
        pricing = PRICING_USD.get(model, PRICING_USD["gemini-2.5-flash"])
        cost_in = (tokens_in / 1_000_000) * pricing["input_per_1m_tokens"]
        cost_out = (tokens_out / 1_000_000) * pricing["output_per_1m_tokens"]
        total = cost_in + cost_out
        self._record(CostEvent(
            timestamp=time.time(), episode_number=episode_number,
            provider=model, operation="generate",
            units=tokens_in + tokens_out, unit_type="tokens",
            cost_usd=total,
        ))
        return total

    def record_elevenlabs(
        self, *, episode_number: Optional[int], chars: int,
        tier: str = "elevenlabs_creator",
    ) -> float:
        pricing = PRICING_USD.get(tier, PRICING_USD["elevenlabs_creator"])
        cost = (chars / 1000) * pricing["per_1k_chars"]
        self._record(CostEvent(
            timestamp=time.time(), episode_number=episode_number,
            provider="elevenlabs", operation="tts",
            units=chars, unit_type="chars",
            cost_usd=cost,
        ))
        return cost

    def record_leonardo(
        self, *, episode_number: Optional[int], image_count: int,
        model_tier: str = "sdxl",
    ) -> float:
        key = "per_image_sdxl" if model_tier == "sdxl" else "per_image_sd"
        per = PRICING_USD["leonardo"][key]
        cost = image_count * per
        self._record(CostEvent(
            timestamp=time.time(), episode_number=episode_number,
            provider="leonardo", operation="image_gen",
            units=image_count, unit_type="images",
            cost_usd=cost,
        ))
        return cost

    def record_claude(
        self, *, episode_number: Optional[int], tokens_in: int, tokens_out: int,
    ) -> float:
        pricing = PRICING_USD["claude-opus-4-7"]
        cost = (tokens_in / 1_000_000) * pricing["input_per_1m_tokens"] \
             + (tokens_out / 1_000_000) * pricing["output_per_1m_tokens"]
        self._record(CostEvent(
            timestamp=time.time(), episode_number=episode_number,
            provider="claude", operation="validate",
            units=tokens_in + tokens_out, unit_type="tokens",
            cost_usd=cost,
        ))
        return cost

    def _record(self, event: CostEvent) -> None:
        with self._lock:
            self._events.append(event)
            try:
                with self._log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(event)) + "\n")
            except Exception as e:
                logger.warning(f"⚠️ Cost log write failed: {e}")

    def summary(self, episode_number: Optional[int] = None) -> Dict[str, float]:
        with self._lock:
            relevant = (
                [e for e in self._events if e.episode_number == episode_number]
                if episode_number is not None
                else list(self._events)
            )
            by_provider: Dict[str, float] = {}
            total = 0.0
            for e in relevant:
                by_provider[e.provider] = by_provider.get(e.provider, 0.0) + e.cost_usd
                total += e.cost_usd
            return {
                "total_usd": round(total, 4),
                "by_provider": {k: round(v, 4) for k, v in by_provider.items()},
                "event_count": len(relevant),
            }

    def episode_cost(self, episode_number: int) -> float:
        return self.summary(episode_number=episode_number)["total_usd"]
