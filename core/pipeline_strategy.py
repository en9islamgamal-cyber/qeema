"""
core/pipeline_strategy.py — VALUE / QEEMA v22.5 — PipelineStrategy + StrategyFactory
=========================================================================
Strategy pattern for dynamic pipeline path selection.

[Why this exists]
v20 added several optimization paths (multi-task script, batched tafsir,
combined TTS, degradation modes), but the orchestrator never actually
USED them. This module wires those features into a single decision-maker
the orchestrator queries before each stage.

[How it works]
PipelineStrategy is computed ONCE at the start of an episode based on:
  - Current quota state (ElevenLabs chars, Leonardo tokens)
  - Quality mode (HIGH / BALANCED / ECONOMY / AUTO)
  - Episode number (first 4 episodes get higher quality)
  - Available engines (graceful degradation if optional engines missing)

The orchestrator then asks strategy.use_X() for each decision point.

[Decision matrix]
                          HIGH    BALANCED  ECONOMY
Multi-task script         yes     yes       yes
Batched tafsir            yes     yes       no (heuristic)
Combined per-scene TTS    yes     yes       yes
AI images (Leonardo)      7       5         3
Adaptive voice emotions   yes     yes       no (single voice)
Subtitles                 yes     yes       yes
Tafsir validation         gemini  gemini    gemini (always on if key)
Quality threshold         70      65        60
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class QualityMode(str, Enum):
    """Operating quality modes."""
    HIGH = "high"
    BALANCED = "balanced"
    ECONOMY = "economy"
    AUTO = "auto"


@dataclass(frozen=True)
class PipelineStrategy:
    """
    Immutable strategy snapshot for a single episode run.

    Computed once at episode start, queried throughout the pipeline.
    All boolean methods return deterministic decisions — no surprises mid-run.
    """
    mode: QualityMode

    # Script generation
    use_multi_task_script: bool

    # Voice
    use_combined_tts: bool           # 1 call per scene vs many
    use_adaptive_voice: bool         # per-emotion settings vs single

    # Images
    max_ai_images: int               # 0 = CSS only
    image_reuse_strategy: str        # "unique" | "reuse" | "minimal"

    # Subtitles
    enable_subtitles: bool

    # Quality
    quality_threshold: float         # script quality gate

    # Why this strategy was chosen (for logs/debugging)
    reasoning: str = ""

    # Quota snapshot at strategy creation time
    quota_snapshot: dict = field(default_factory=dict)

    # ─── Helper methods ────────────────────────────────────────
    def summary(self) -> str:
        """One-line summary for logs."""
        return (
            f"PipelineStrategy(mode={self.mode.value}, "
            f"multi_task={self.use_multi_task_script}, "
            f"images={self.max_ai_images}, "
            f"adaptive_voice={self.use_adaptive_voice}, "
            f"threshold={self.quality_threshold})"
        )

    def detailed_report(self) -> str:
        """Multi-line report for episode breakdown logs."""
        lines = [
            f"┌─── Pipeline Strategy ──────────",
            f"│ Mode:               {self.mode.value.upper()}",
            f"│ Reason:             {self.reasoning}",
            f"│ Multi-task script:  {self._yn(self.use_multi_task_script)}",
            f"│ Combined TTS:       {self._yn(self.use_combined_tts)}",
            f"│ Adaptive voice:     {self._yn(self.use_adaptive_voice)}",
            f"│ Max AI images:      {self.max_ai_images}",
            f"│ Image strategy:     {self.image_reuse_strategy}",
            f"│ Subtitles:          {self._yn(self.enable_subtitles)}",
            f"│ Quality threshold:  {self.quality_threshold}",
        ]
        if self.quota_snapshot:
            lines.append(f"│ Quota at start:     {self.quota_snapshot}")
        lines.append(f"└────────────────────────────────")
        return "\n".join(lines)

    @staticmethod
    def _yn(b: bool) -> str:
        return "YES ✓" if b else "no ✗"


# ════════════════════════════════════════════════════════════════
# Strategy factory
# ════════════════════════════════════════════════════════════════
class StrategyFactory:
    """Builds PipelineStrategy instances based on inputs."""

    # Quota thresholds for auto-selection
    HIGH_LEO_THRESHOLD = 35       # tokens
    HIGH_EL_THRESHOLD = 3500      # chars
    BALANCED_LEO_THRESHOLD = 15
    BALANCED_EL_THRESHOLD = 2500

    @classmethod
    def build(
        cls,
        *,
        requested_mode: QualityMode = QualityMode.AUTO,
        quota_manager: Any = None,                 # core.quota_manager.QuotaManager
        episode_number: int = 0,
        has_tafsir_validator: bool = False,
        has_leonardo_engine: bool = False,
        has_multi_task_engine: bool = False,
    ) -> PipelineStrategy:
        """
        Build a strategy honoring user request, quota constraints, and
        engine availability.

        Auto-selection logic:
            - First 4 episodes of month + plenty of quota → HIGH
            - Tight quota                                  → BALANCED
            - Critical quota or no quota_manager           → ECONOMY

        Manual override (requested_mode != AUTO):
            User's choice is respected, but capped if quota too low.
            E.g., user asks HIGH but Leonardo has 5 tokens left → BALANCED.
        """
        # 1) Snapshot quota state
        snapshot = cls._snapshot_quota(quota_manager)

        # 2) Determine the mode to actually use
        mode, reasoning = cls._select_mode(
            requested_mode, snapshot, episode_number,
        )

        # 3) Build feature flags from mode + engine availability
        return cls._build_for_mode(
            mode=mode,
            reasoning=reasoning,
            quota_snapshot=snapshot,
            has_tafsir_validator=has_tafsir_validator,
            has_leonardo_engine=has_leonardo_engine,
            has_multi_task_engine=has_multi_task_engine,
        )

    @staticmethod
    def _snapshot_quota(qm) -> dict:
        if qm is None:
            return {}
        try:
            return {
                "leonardo_remaining": qm.leonardo_remaining(),
                "elevenlabs_remaining": qm.elevenlabs_remaining(),
                "episodes_completed_month": getattr(
                    qm, "_state", None
                ).episodes_completed_this_month if hasattr(qm, "_state") else 0,
            }
        except Exception as e:
            logger.warning(f"⚠️ Could not snapshot quota: {e}")
            return {}

    @classmethod
    def _select_mode(
        cls,
        requested: QualityMode,
        snapshot: dict,
        episode_number: int,
    ) -> tuple:
        """Returns (chosen_mode, human_reason_string)."""
        leo = snapshot.get("leonardo_remaining", 999)
        el = snapshot.get("elevenlabs_remaining", 999999)

        # Auto-select path
        if requested == QualityMode.AUTO:
            if leo >= cls.HIGH_LEO_THRESHOLD and el >= cls.HIGH_EL_THRESHOLD:
                return QualityMode.HIGH, (
                    f"auto-selected HIGH: ample quota (Leo={leo}, EL={el:,})"
                )
            if leo >= cls.BALANCED_LEO_THRESHOLD and el >= cls.BALANCED_EL_THRESHOLD:
                return QualityMode.BALANCED, (
                    f"auto-selected BALANCED: tight quota (Leo={leo}, EL={el:,})"
                )
            return QualityMode.ECONOMY, (
                f"auto-selected ECONOMY: critical quota (Leo={leo}, EL={el:,})"
            )

        # User-requested path — honor but cap if quota insufficient
        if requested == QualityMode.HIGH:
            if leo < cls.HIGH_LEO_THRESHOLD or el < cls.HIGH_EL_THRESHOLD:
                if leo >= cls.BALANCED_LEO_THRESHOLD and el >= cls.BALANCED_EL_THRESHOLD:
                    return QualityMode.BALANCED, (
                        f"requested HIGH but capped to BALANCED (Leo={leo}, EL={el:,})"
                    )
                return QualityMode.ECONOMY, (
                    f"requested HIGH but capped to ECONOMY (Leo={leo}, EL={el:,})"
                )
            return QualityMode.HIGH, "user-requested HIGH"

        if requested == QualityMode.BALANCED:
            if leo < cls.BALANCED_LEO_THRESHOLD or el < cls.BALANCED_EL_THRESHOLD:
                return QualityMode.ECONOMY, (
                    f"requested BALANCED but capped to ECONOMY (Leo={leo}, EL={el:,})"
                )
            return QualityMode.BALANCED, "user-requested BALANCED"

        # ECONOMY is always allowed
        return QualityMode.ECONOMY, "user-requested ECONOMY"

    @classmethod
    def _build_for_mode(
        cls, *,
        mode: QualityMode,
        reasoning: str,
        quota_snapshot: dict,
        has_tafsir_validator: bool,
        has_leonardo_engine: bool,
        has_multi_task_engine: bool,
    ) -> PipelineStrategy:
        """Build strategy from chosen mode + engine availability."""

        # All modes use multi-task script if available — it's strictly better
        # than 6 separate calls regardless of quota.
        use_multi_task = has_multi_task_engine

        # Note: tafsir validation is now mandatory whenever the validator is
        # wired (no use_claude_tafsir gate in v22.5). The has_tafsir_validator
        # flag is recorded for diagnostics but no strategy field branches on it.
        _ = has_tafsir_validator

        # TTS: combined is always better, but adaptive voice only HIGH/BALANCED.
        use_combined_tts = True  # always — pure efficiency win
        use_adaptive_voice = mode != QualityMode.ECONOMY

        # Images
        if not has_leonardo_engine:
            max_images = 0
            image_strategy = "css_only"
        elif mode == QualityMode.HIGH:
            max_images = 7
            image_strategy = "unique"
        elif mode == QualityMode.BALANCED:
            max_images = 5
            image_strategy = "reuse"
        else:  # ECONOMY
            max_images = 3
            image_strategy = "minimal"

        # Subtitles always on (cheap value-add for accessibility)
        enable_subtitles = True

        # Quality threshold
        thresholds = {
            QualityMode.HIGH: 70.0,
            QualityMode.BALANCED: 65.0,
            QualityMode.ECONOMY: 60.0,
        }
        quality_threshold = thresholds[mode]

        return PipelineStrategy(
            mode=mode,
            use_multi_task_script=use_multi_task,
            use_combined_tts=use_combined_tts,
            use_adaptive_voice=use_adaptive_voice,
            max_ai_images=max_images,
            image_reuse_strategy=image_strategy,
            enable_subtitles=enable_subtitles,
            quality_threshold=quality_threshold,
            reasoning=reasoning,
            quota_snapshot=quota_snapshot,
        )


# ════════════════════════════════════════════════════════════════
# Convenience functions
# ════════════════════════════════════════════════════════════════
def parse_mode(value: Optional[str]) -> QualityMode:
    """Parse CLI/ENV mode string. Defaults to AUTO."""
    if not value:
        return QualityMode.AUTO
    try:
        return QualityMode(value.lower().strip())
    except ValueError:
        logger.warning(f"⚠️ Unknown mode '{value}' — using AUTO")
        return QualityMode.AUTO
