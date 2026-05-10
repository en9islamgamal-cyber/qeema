"""
core/phase_router.py — VALUE / QEEMA v22.5 (NEW)
=========================================================================
Multi-day phase-based orchestration.

[Why this exists]
The legacy Orchestrator.run() does everything in one go. For the 3-day
pipeline, we need to:
  - Day 1: planning only (script + tafsir + visual enrich + TTS direct)
  - Day 2: assets only (Leonardo + ElevenLabs)
  - Day 3: render + upload

Rather than rewriting the 1500-line orchestrator, this PhaseRouter wraps
it. It calls only the stages relevant to each phase and persists state
between phases via PhaseStateManager.

[How it integrates with the legacy orchestrator]
PhaseRouter does NOT replace orchestrator.run(). Instead:
  - Phase 1: calls script generation + validation directly (no rendering)
  - Phase 2: reads script, calls image + audio generation
  - Phase 3: reads everything, calls render + upload (this IS most of run())

For Phase 3, we delegate to a stripped-down orchestrator path that skips
the work done in Phases 1 & 2.

[Failure handling]
If any phase fails, the next-day workflow can retry that same phase.
After max_retries (default 2), the episode is marked FAILED_PERMANENT.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from core.phase_state import Phase, PhaseState, PhaseStateManager

logger = logging.getLogger(__name__)


@dataclass
class PhaseRunResult:
    """Result of running a single phase."""
    episode_number: int
    phase: int
    success: bool
    duration_sec: float = 0.0
    error: Optional[str] = None
    next_phase: Optional[int] = None  # which phase is next (None if done)
    state: Optional[PhaseState] = None


class PhaseRouter:
    """Routes work to the right pipeline stages based on phase number.

    Args:
        orchestrator: The legacy Orchestrator instance (for delegating
                     phase 3 render/upload work).
        state_manager: PhaseStateManager for persistence.
        max_retries_per_phase: After this many failed attempts on a phase,
                              the episode is marked permanently failed.
    """

    def __init__(
        self,
        orchestrator: Any,
        state_manager: PhaseStateManager,
        *,
        max_retries_per_phase: int = 2,
    ) -> None:
        self._orchestrator = orchestrator
        self._state_manager = state_manager
        self._max_retries = max_retries_per_phase

    # ─── Public API ──────────────────────────────────────────
    def run_phase(
        self,
        episode_number: int,
        phase: Optional[int] = None,
    ) -> PhaseRunResult:
        """Run a specific phase for an episode.

        Args:
            episode_number: Which episode to process.
            phase: Which phase to run (1, 2, or 3). If None, auto-detects
                  the next pending phase.

        Returns:
            PhaseRunResult with success/failure status.
        """
        import time
        start = time.monotonic()

        state = self._state_manager.load(episode_number)

        # Determine which phase to run
        if phase is None:
            phase = self._state_manager.determine_next_phase(state)
            if phase is None:
                logger.info(
                    f"ℹ️ Episode {episode_number} already completed — skipping"
                )
                return PhaseRunResult(
                    episode_number=episode_number,
                    phase=int(state.phase),
                    success=True,
                    next_phase=None,
                    state=state,
                )

        # Validate phase prerequisite
        if not self._can_run(state, phase):
            err = (
                f"Cannot run phase {phase}: prerequisite phases not complete "
                f"(current phase={state.phase})"
            )
            logger.error(f"❌ {err}")
            return PhaseRunResult(
                episode_number=episode_number,
                phase=phase,
                success=False,
                error=err,
                state=state,
            )

        # Check retry cap
        attempts = self._attempts_for_phase(state, phase)
        if attempts >= self._max_retries:
            err = f"Phase {phase} exhausted {attempts} retries — giving up"
            logger.error(f"❌ Episode {episode_number}: {err}")
            return PhaseRunResult(
                episode_number=episode_number,
                phase=phase,
                success=False,
                error=err,
                state=state,
            )

        # Dispatch to phase handler
        logger.info(
            f"🚀 Starting Phase {phase} for episode {episode_number} "
            f"(attempt #{attempts + 1}/{self._max_retries})"
        )
        try:
            if phase == Phase.PLANNING:
                outputs = self._run_phase_1(episode_number, state)
            elif phase == Phase.ASSETS:
                outputs = self._run_phase_2(episode_number, state)
            elif phase == Phase.RENDER:
                outputs = self._run_phase_3(episode_number, state)
            else:
                raise ValueError(f"Unknown phase: {phase}")

            # Mark complete
            state = self._state_manager.mark_phase_complete(
                state, phase=phase, outputs=outputs,
            )
            duration = time.monotonic() - start
            return PhaseRunResult(
                episode_number=episode_number,
                phase=phase,
                success=True,
                duration_sec=duration,
                next_phase=self._state_manager.determine_next_phase(state),
                state=state,
            )
        except Exception as e:
            duration = time.monotonic() - start
            logger.exception(
                f"❌ Phase {phase} failed for episode {episode_number}: {e}"
            )
            state = self._state_manager.mark_phase_failed(
                state, phase=phase, error=str(e),
            )
            return PhaseRunResult(
                episode_number=episode_number,
                phase=phase,
                success=False,
                duration_sec=duration,
                error=str(e),
                state=state,
            )

    # ─── Phase implementations ───────────────────────────────
    def _run_phase_1(
        self, episode_number: int, state: PhaseState,
    ) -> Dict[str, Any]:
        """Phase 1: Planning + Scripting.

        v22.5 FIX: Delegates to orchestrator.run(phase=PHASE_1) which has
        the correct method signatures and complete v22.5 logic baked in:
          - Multi-task script generation
          - Tafsir validation (Gemini-only, retried with backoff)
          - Deep visual prompts (3 chained Gemini calls × 7 scenes)
          - TTS Director (per-segment SSML direction)

        No Leonardo, no ElevenLabs — pure planning.
        """
        from core.models import EpisodePhase

        report = self._orchestrator.run(
            episode_number, phase=EpisodePhase.PHASE_1,
        )
        if not report.success:
            raise RuntimeError(
                f"Phase 1 orchestrator failed: "
                f"{report.error or 'unknown error'} "
                f"(status={report.final_status})"
            )

        # Reload the saved script JSON so we can store it in PhaseState
        script_data = self._load_script_json(episode_number)

        return {"script_data": script_data}

    def _run_phase_2(
        self, episode_number: int, state: PhaseState,
    ) -> Dict[str, Any]:
        """Phase 2: Asset generation (Leonardo + ElevenLabs).

        v22.5 FIX: Delegates to orchestrator.run(phase=PHASE_2). The
        orchestrator reads Phase 1's outputs from disk and generates assets.

        v22.6.3: also extracts the orchestrator's _deep_visuals and
        _tts_directions (written into temp/episodes/episode_NNN.json
        during Phase 2) into asset_paths so they propagate to the
        persistent phase state. Without this, Phase 3 running on a
        different runner has no way to access them.
        """
        from core.models import EpisodePhase

        if not state.script_data:
            raise RuntimeError(
                "Phase 2 requires Phase 1's script_data — none found in state"
            )

        report = self._orchestrator.run(
            episode_number, phase=EpisodePhase.PHASE_2,
        )
        if not report.success:
            raise RuntimeError(
                f"Phase 2 orchestrator failed: "
                f"{report.error or 'unknown error'} "
                f"(status={report.final_status})"
            )

        # Read asset paths from the orchestrator's phase state
        ep_dir = (
            self._orchestrator.paths.temp_episodes
            / f"episode_{episode_number:03d}"
        )
        orch_phase_state = self._orchestrator._load_phase_state(episode_number)

        # v22.6.3: pull deep_visuals + tts_directions from the orchestrator's
        # temp episode JSON. They're not in _phase_state.json — that file
        # only has audio_map + mastered_map. The visuals/TTS-directions
        # live in temp/episodes/episode_NNN.json (alongside the script).
        deep_visuals: list = []
        tts_directions: dict = {}
        ep_json_path = (
            self._orchestrator.paths.temp_episodes
            / f"episode_{episode_number:03d}.json"
        )
        if ep_json_path.exists():
            try:
                import json as _json
                with open(ep_json_path, encoding="utf-8") as f:
                    ep_data = _json.load(f)
                deep_visuals = ep_data.get("_deep_visuals") or []
                tts_directions = ep_data.get("_tts_directions") or {}
            except Exception as e:
                logger.warning(
                    f"⚠️ Could not read deep_visuals/tts_directions "
                    f"from episode JSON: {e}"
                )

        return {
            "asset_paths": {
                "ep_dir": str(ep_dir),
                "audio_map": orch_phase_state.get("audio_map", {}),
                "mastered_map": orch_phase_state.get("mastered_map", {}),
                # Image paths can be derived from ep_dir/ai_images/
                "ai_images_dir": str(ep_dir / "ai_images"),
                # v22.6.3: ensure these survive cross-run restarts.
                # Phase 3 may run on a fresh runner; without these in
                # persistent state, the rendered video would have no
                # cinematic visual prompts and no SSML pacing.
                "_deep_visuals": deep_visuals,
                "_tts_directions": tts_directions,
            }
        }

    def _run_phase_3(
        self, episode_number: int, state: PhaseState,
    ) -> Dict[str, Any]:
        """Phase 3: Render + upload.

        v22.5 FIX: Delegates to orchestrator.run(phase=PHASE_3) which
        reuses the Phase 1+2 outputs from disk and runs only the render
        and upload stages.
        """
        from core.models import EpisodePhase

        if not state.asset_paths:
            raise RuntimeError(
                "Phase 3 requires Phase 2's asset_paths — none found in state"
            )

        report = self._orchestrator.run(
            episode_number, phase=EpisodePhase.PHASE_3,
        )
        if not report.success:
            raise RuntimeError(
                f"Phase 3 orchestrator run failed: {report.error}"
            )

        return {
            "render_artifacts": {
                "video_url": report.video_url or "",
                "final_status": report.final_status,
                "duration_sec": report.total_duration_sec,
            }
        }

    def _load_script_json(self, episode_number: int) -> Dict[str, Any]:
        """Load the saved episode JSON (written by Phase 1) for state storage."""
        import json
        ep_path = (
            self._orchestrator.paths.temp_episodes
            / f"episode_{episode_number:03d}.json"
        )
        if not ep_path.exists():
            logger.warning(
                f"⚠️ Episode JSON not found at {ep_path} — Phase 1 may not have saved properly"
            )
            return {}
        try:
            with open(ep_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ Failed to load episode JSON: {e}")
            return {}

    # ─── Helpers ─────────────────────────────────────────────
    @staticmethod
    def _can_run(state: PhaseState, phase: int) -> bool:
        if phase == Phase.PLANNING:
            return state.can_run_phase_1
        elif phase == Phase.ASSETS:
            return state.can_run_phase_2
        elif phase == Phase.RENDER:
            return state.can_run_phase_3
        return False

    @staticmethod
    def _attempts_for_phase(state: PhaseState, phase: int) -> int:
        if phase == Phase.PLANNING:
            return state.phase_1_attempts
        elif phase == Phase.ASSETS:
            return state.phase_2_attempts
        elif phase == Phase.RENDER:
            return state.phase_3_attempts
        return 0

