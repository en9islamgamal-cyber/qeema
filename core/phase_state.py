"""
core/phase_state.py — VALUE / QEEMA v22.5 (NEW)
=========================================================================
Phase state persistence for the 3-day pipeline.

[Why this exists]
The 3-day pipeline runs as 3 separate GitHub Actions workflows on different
days. Each workflow gets a clean checkout — no in-memory state carries
over. We must persist the work product of each phase to disk so the next
day's workflow can pick up.

[Design]
For each episode, we maintain a phase state file:

    state/phases/episode_001.json

Containing:
    {
      "episode_number": 1,
      "phase": 1,                    # last completed phase
      "phase_1_completed_at": "...",
      "phase_2_completed_at": null,
      "phase_3_completed_at": null,
      "script_data": { ... },        # populated after Phase 1
      "asset_paths": { ... },        # populated after Phase 2
      "render_artifacts": { ... }    # populated after Phase 3
    }

[Cache strategy in pipeline.yml]
The state/phases/ dir is added to the GitHub Actions cache so it
survives across daily runs.

[Phase progression]
  None → 1 → 2 → 3 → completed
  1    → can re-run 1 (force_regenerate)
  2    → requires phase_1 done
  3    → requires phase_2 done

[Failure recovery]
If Phase 2 fails midway, the state still says phase=1. Next day's run
sees phase=1 and tries Phase 2 again. After max_retries (configurable),
the orchestrator marks the episode as FAILED.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class Phase(IntEnum):
    """Phase numbering — IntEnum so we can compare with < / >."""
    NONE = 0          # not started
    PLANNING = 1      # script + tafsir + visual enrich + TTS direct
    ASSETS = 2        # Leonardo images + ElevenLabs TTS
    RENDER = 3        # FFmpeg render + concat + subtitles + upload
    COMPLETED = 4     # all phases done


@dataclass
class PhaseState:
    """Persistent state for a single episode across the 3-phase pipeline."""
    episode_number: int
    phase: int = 0  # last *completed* phase (Phase enum value)

    # Timestamps
    phase_1_completed_at: Optional[str] = None
    phase_2_completed_at: Optional[str] = None
    phase_3_completed_at: Optional[str] = None

    # Failure tracking
    phase_1_attempts: int = 0
    phase_2_attempts: int = 0
    phase_3_attempts: int = 0
    last_error: Optional[str] = None
    last_error_phase: Optional[int] = None

    # Phase outputs (saved between days)
    script_data: Optional[Dict[str, Any]] = None     # Phase 1 output
    asset_paths: Dict[str, str] = field(default_factory=dict)  # Phase 2
    render_artifacts: Dict[str, str] = field(default_factory=dict)  # Phase 3

    # Manual review gate
    review_approved: bool = False
    review_requested: bool = False

    @property
    def can_run_phase_1(self) -> bool:
        """Phase 1 can always run — it's the start."""
        return True

    @property
    def can_run_phase_2(self) -> bool:
        """Phase 2 needs Phase 1 done + (optionally) review approved."""
        return self.phase >= Phase.PLANNING

    @property
    def can_run_phase_3(self) -> bool:
        """Phase 3 needs Phase 2 done."""
        return self.phase >= Phase.ASSETS

    @property
    def is_completed(self) -> bool:
        return self.phase >= Phase.COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PhaseState":
        # Filter unknown keys (forward-compat)
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        cleaned = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**cleaned)


# ════════════════════════════════════════════════════════════════
# PhaseStateManager — disk-backed persistence
# ════════════════════════════════════════════════════════════════
class PhaseStateManager:
    """Manages per-episode phase state on disk.

    Each episode gets its own JSON file in <state_dir>/episode_NNN.json.
    The directory is cached by GitHub Actions across runs.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, episode_number: int) -> Path:
        return self._state_dir / f"episode_{episode_number:03d}.json"

    def load(self, episode_number: int) -> PhaseState:
        """Load state for an episode. Returns empty state if not yet started."""
        path = self._path_for(episode_number)
        if not path.exists():
            return PhaseState(episode_number=episode_number)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return PhaseState.from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                f"⚠️ Failed to load phase state for ep {episode_number}: {e} — "
                f"starting fresh"
            )
            return PhaseState(episode_number=episode_number)

    def save(self, state: PhaseState) -> None:
        """Save state to disk. Atomic via temp file + rename."""
        path = self._path_for(state.episode_number)
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except OSError as e:
            logger.error(f"❌ Failed to save phase state: {e}")
            raise

    def mark_phase_complete(
        self,
        state: PhaseState,
        phase: int,
        outputs: Optional[Dict[str, Any]] = None,
    ) -> PhaseState:
        """Mark a phase as completed and persist.

        Args:
            state: Current state object
            phase: The phase number that was just completed (1, 2, or 3)
            outputs: Optional dict of phase-specific outputs to merge in

        Returns:
            Updated state (also saved to disk)
        """
        now = datetime.now(timezone.utc).isoformat()
        if phase == Phase.PLANNING:
            state.phase_1_completed_at = now
            if outputs and "script_data" in outputs:
                state.script_data = outputs["script_data"]
        elif phase == Phase.ASSETS:
            state.phase_2_completed_at = now
            if outputs and "asset_paths" in outputs:
                state.asset_paths = outputs["asset_paths"]
        elif phase == Phase.RENDER:
            state.phase_3_completed_at = now
            if outputs and "render_artifacts" in outputs:
                state.render_artifacts = outputs["render_artifacts"]

        # Update phase pointer to highest completed
        if phase > state.phase:
            state.phase = phase

        # If phase 3 completed, mark fully done
        if phase == Phase.RENDER:
            state.phase = Phase.COMPLETED

        # Clear last error since this phase succeeded
        state.last_error = None
        state.last_error_phase = None

        self.save(state)
        logger.info(
            f"✅ Episode {state.episode_number}: Phase {phase} marked complete"
        )
        return state

    def mark_phase_failed(
        self,
        state: PhaseState,
        phase: int,
        error: str,
    ) -> PhaseState:
        """Record a phase failure and persist."""
        if phase == Phase.PLANNING:
            state.phase_1_attempts += 1
        elif phase == Phase.ASSETS:
            state.phase_2_attempts += 1
        elif phase == Phase.RENDER:
            state.phase_3_attempts += 1

        state.last_error = error[:500]  # truncate huge errors
        state.last_error_phase = phase
        self.save(state)
        logger.warning(
            f"⚠️ Episode {state.episode_number}: Phase {phase} failed "
            f"(attempt #{[state.phase_1_attempts, state.phase_2_attempts, state.phase_3_attempts][phase-1]})"
        )
        return state

    def determine_next_phase(self, state: PhaseState) -> Optional[int]:
        """Decide which phase should run next for this episode.

        Returns:
            1, 2, or 3 — the phase to run
            None — episode is fully completed
        """
        if state.is_completed:
            return None

        if state.phase == Phase.NONE:
            return Phase.PLANNING
        if state.phase == Phase.PLANNING:
            return Phase.ASSETS
        if state.phase == Phase.ASSETS:
            return Phase.RENDER
        return None

    def list_states(self) -> list:
        """List all persisted states."""
        states = []
        for p in sorted(self._state_dir.glob("episode_*.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    states.append(PhaseState.from_dict(json.load(f)))
            except (json.JSONDecodeError, OSError):
                continue
        return states

    def cleanup_completed(self, keep_days: int = 30) -> int:
        """Remove completed-state files older than keep_days. Returns count removed."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        removed = 0
        for state in self.list_states():
            if not state.is_completed:
                continue
            if not state.phase_3_completed_at:
                continue
            try:
                completed = datetime.fromisoformat(state.phase_3_completed_at)
                if completed < cutoff:
                    self._path_for(state.episode_number).unlink()
                    removed += 1
            except (ValueError, OSError):
                continue
        return removed
