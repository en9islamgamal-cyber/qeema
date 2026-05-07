"""
core/degradation_modes.py — VALUE / QEEMA v22.5 — quota-aware quality modes
=========================================================================
Three operating modes that scale quality based on remaining quota.

[Why this exists]
v19 was binary: "pass quota check or fall back fully to CSS". v20 added
graceful degradation tiers so episodes complete with the best quality
the remaining quota can buy. v22.5 removes Claude/Heuristic branching —
tafsir validation is always Gemini-based (mandatory whenever a key is set).

[The three modes]

HIGH (default — episodes 1-4 of the month):
  - 7 images per episode (Lightning XL)
  - Adaptive voice (5 emotions)
  - 1080p HD output
  - Multi-task script (1 LLM call)

BALANCED (episodes 5-6 — quota tightening):
  - 5 images per episode (skip duplicate analogy/explain visuals)
  - Adaptive voice (5 emotions)
  - 1080p
  - Multi-task script

ECONOMY (episode 7 — last episode of month):
  - 3 images per episode (intro + 1 hero ayah + outro)
  - Single voice setting (no emotion overrides — saves chars)
  - 1080p
  - Multi-task script

[Mode selection]
Auto-selected based on remaining quota at episode start.
Can be overridden by CLI flag --mode high|balanced|economy.

[Tafsir validation]
Always Gemini-based. NOT a budget knob anymore — it's a hard requirement
whenever GEMINI_API_KEY is set. The orchestrator skips with a warning if
no key is configured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class QualityMode(str, Enum):
    HIGH = "high"
    BALANCED = "balanced"
    ECONOMY = "economy"


@dataclass(frozen=True)
class ModeBudget:
    """Cost budget for a quality mode."""
    mode: QualityMode
    # Image generation
    max_images_per_episode: int
    use_unique_image_per_scene: bool  # False → reuse intro image for ayahs
    # Voice
    use_adaptive_voice: bool
    # Description
    description: str


MODE_BUDGETS = {
    QualityMode.HIGH: ModeBudget(
        mode=QualityMode.HIGH,
        max_images_per_episode=7,
        use_unique_image_per_scene=True,
        use_adaptive_voice=True,
        description="Full quality — 7 unique images, adaptive voice",
    ),
    QualityMode.BALANCED: ModeBudget(
        mode=QualityMode.BALANCED,
        max_images_per_episode=5,
        use_unique_image_per_scene=False,  # reuse images across similar scenes
        use_adaptive_voice=True,
        description="Balanced — 5 images, adaptive voice",
    ),
    QualityMode.ECONOMY: ModeBudget(
        mode=QualityMode.ECONOMY,
        max_images_per_episode=3,
        use_unique_image_per_scene=False,
        use_adaptive_voice=False,  # single voice setting saves no chars but reduces complexity
        description="Economy — 3 images, single voice",
    ),
}


def auto_select_mode(quota_manager) -> QualityMode:
    """
    Pick the highest-quality mode that fits remaining quota.

    Decision logic:
    - Sufficient Leonardo (≥35 tokens) AND ElevenLabs (≥3500 chars) → HIGH
    - Marginal Leonardo (15-34 tokens) OR low ElevenLabs (<3500) → BALANCED
    - Critical Leonardo (<15 tokens) AND/OR very low ElevenLabs → ECONOMY
    """
    if quota_manager is None:
        return QualityMode.HIGH

    leo = quota_manager.leonardo_remaining()
    el = quota_manager.elevenlabs_remaining()

    # Conservative thresholds — leave headroom for retries
    if leo >= 35 and el >= 3500:
        chosen = QualityMode.HIGH
    elif leo >= 15 and el >= 2500:
        chosen = QualityMode.BALANCED
    else:
        chosen = QualityMode.ECONOMY

    budget = MODE_BUDGETS[chosen]
    logger.info(
        f"📊 Auto-selected mode: {chosen.value.upper()} "
        f"(Leo={leo}, EL={el:,}) — {budget.description}"
    )
    return chosen


def parse_mode(value: Optional[str]) -> Optional[QualityMode]:
    """Parse CLI/ENV mode string to QualityMode enum."""
    if not value:
        return None
    try:
        return QualityMode(value.lower().strip())
    except ValueError:
        logger.warning(f"⚠️ Unknown mode '{value}' — using auto-select")
        return None


def get_budget(mode: QualityMode) -> ModeBudget:
    """Get the budget for a given mode."""
    return MODE_BUDGETS[mode]
