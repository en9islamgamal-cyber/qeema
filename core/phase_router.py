"""
core/phase_router.py — VALUE / QEEMA v22.7 (Persistence layer added)
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

[v22.7 — Persistence layer]
Phase 2 generates audio/image files into the runner's `temp/episodes/`
directory. GitHub Actions wipes this between runs, so Phase 3 on a fresh
runner used to crash with "Audio missing for render".

v22.7 introduces AssetStorage (Supabase Storage): Phase 2 uploads its
output directory after the orchestrator succeeds; Phase 3 downloads
everything back into the same local path before the orchestrator runs.
The upload manifest is stored inside `asset_paths["_storage_manifest"]`
and survives via the existing `state/phases/` GitHub Actions cache.

If `asset_storage` is None (e.g. --skip-supabase, or Supabase init
failed), Phase 2 logs a warning but still completes. Phase 3 will only
work on the same runner that ran Phase 2 in that case.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
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
        asset_storage: v22.7 — Optional AssetStorage for cross-runner asset
                       persistence. When wired:
                         • Phase 2 uploads its output dir to Supabase Storage
                           after orchestrator success, and embeds the manifest
                           in the returned asset_paths.
                         • Phase 3 downloads the manifest's files into the
                           local temp dir before the orchestrator runs.
                       When None (e.g. --skip-supabase), Phase 2 logs a
                       warning and Phase 3 assumes same-runner execution.
    """

    def __init__(
        self,
        orchestrator: Any,
        state_manager: PhaseStateManager,
        *,
        max_retries_per_phase: int = 2,
        asset_storage: Any = None,  # v22.7: infrastructure.asset_storage.AssetStorage
    ) -> None:
        self._orchestrator = orchestrator
        self._state_manager = state_manager
        self._max_retries = max_retries_per_phase
        self._asset_storage = asset_storage
        if asset_storage is None:
            logger.warning(
                "⚠️ PhaseRouter: AssetStorage not wired — Phase 2 outputs will "
                "NOT be persisted to Supabase Storage. Phase 3 will fail if it "
                "runs on a different GitHub Actions runner than Phase 2."
            )
        else:
            logger.info(
                "☁️  PhaseRouter: AssetStorage wired — Phase 2 will persist "
                "outputs and Phase 3 will rehydrate them automatically."
            )

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

        v22.5: Delegates to orchestrator.run(phase=PHASE_2). The
        orchestrator reads Phase 1's outputs from disk and generates assets.

        v22.6.3: Extracts the orchestrator's _deep_visuals and
        _tts_directions (written into temp/episodes/episode_NNN.json
        during Phase 2) into asset_paths so they propagate to the
        persistent phase state.

        v22.7: After the orchestrator succeeds, uploads the entire episode
        temp directory to Supabase Storage and embeds the manifest into
        asset_paths. This is the bridge that lets Phase 3 run on a fresh
        runner — without it, Phase 3 crashes with "Audio missing".
        Hard-fails if upload fails: better to retry Phase 2 today than
        let Phase 3 silently break on day three.
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
                with open(ep_json_path, encoding="utf-8") as f:
                    ep_data = json.load(f)
                deep_visuals = ep_data.get("_deep_visuals") or []
                tts_directions = ep_data.get("_tts_directions") or {}
            except Exception as e:
                logger.warning(
                    f"⚠️ Could not read deep_visuals/tts_directions "
                    f"from episode JSON: {e}"
                )

        asset_paths: Dict[str, Any] = {
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

        # ════════════════════════════════════════════════════════════════
        # v22.7: persist physical assets to Supabase Storage
        # ════════════════════════════════════════════════════════════════
        # The orchestrator's Phase 2 wrote audio/image files into ep_dir
        # on THIS runner's filesystem. Phase 3 will run on a DIFFERENT
        # runner (different day) where ep_dir is empty. We mirror the
        # whole directory to Supabase Storage now so Phase 3 can pull
        # it back down before rendering.
        #
        # Defensive getattr: some tests construct PhaseRouter via __new__
        # without going through __init__, so _asset_storage may not exist
        # on those instances. Treat missing attribute the same as None.
        asset_storage = getattr(self, "_asset_storage", None)
        if asset_storage is not None:
            if not ep_dir.is_dir():
                # Should never happen — the orchestrator just wrote to this
                # dir — but guard anyway so we get a clean error message.
                raise RuntimeError(
                    f"Phase 2: episode directory missing after orchestrator "
                    f"reported success: {ep_dir}. Cannot persist to Storage."
                )
            try:
                manifest = asset_storage.upload_episode_dir(
                    episode_number=episode_number,
                    local_dir=str(ep_dir),
                )
            except Exception as e:
                # Re-raise so the phase is marked failed and tomorrow's
                # workflow retries it. Silent success here would mean
                # Phase 3 crashes on day three with "Audio missing".
                raise RuntimeError(
                    f"Phase 2: AssetStorage upload failed — Phase 3 cannot "
                    f"run on a fresh runner without these files. "
                    f"Underlying error: {type(e).__name__}: {e}"
                ) from e

            asset_paths["_storage_manifest"] = manifest
            asset_paths["_storage_uploaded_at"] = (
                datetime.now(timezone.utc).isoformat()
            )
            logger.info(
                f"☁️ v22.7: Phase 2 assets persisted to Supabase Storage "
                f"({len(manifest)} files, prefix=episode_{episode_number:03d})"
            )
        else:
            # No storage wired — Phase 3 will only work if it runs on the
            # same runner as Phase 2 (e.g. local dev with --skip-supabase,
            # or both phases in one workflow job).
            logger.warning(
                "⚠️ v22.7: AssetStorage not wired — Phase 2 outputs NOT "
                "persisted. Phase 3 will fail if it runs on a different runner."
            )

        return {"asset_paths": asset_paths}

    def _run_phase_3(
        self, episode_number: int, state: PhaseState,
    ) -> Dict[str, Any]:
        """Phase 3: Render + upload.

        v22.5: Delegates to orchestrator.run(phase=PHASE_3) which
        reuses the Phase 1+2 outputs from disk.

        v22.7: BEFORE delegating to the orchestrator, downloads Phase 2's
        assets from Supabase Storage into the local temp directory. The
        manifest lives in state.asset_paths["_storage_manifest"]. After
        download, the orchestrator's existing logic (which reads paths from
        persistent state and finds the files in temp/) just works.
        """
        from core.models import EpisodePhase

        if not state.asset_paths:
            raise RuntimeError(
                "Phase 3 requires Phase 2's asset_paths — none found in state"
            )

        # ════════════════════════════════════════════════════════════════
        # v22.7: rehydrate physical assets from Supabase Storage
        # ════════════════════════════════════════════════════════════════
        # GitHub Actions gave us a fresh runner. temp/episodes/episode_NNN/
        # is empty. Pull every file from Phase 2's manifest before letting
        # the orchestrator's render stage start, so paths in mastered_map
        # actually resolve to files on disk.
        #
        # Defensive getattr: see _run_phase_2 for why.
        asset_storage = getattr(self, "_asset_storage", None)
        manifest = state.asset_paths.get("_storage_manifest")
        if manifest:
            if asset_storage is None:
                raise RuntimeError(
                    "Phase 3: state contains a _storage_manifest from Phase 2, "
                    "but AssetStorage is not wired in this run. Cannot fetch "
                    "Phase 2 assets. Re-enable Supabase (remove --skip-supabase) "
                    "and re-run Phase 3."
                )
            ep_dir = (
                self._orchestrator.paths.temp_episodes
                / f"episode_{episode_number:03d}"
            )
            ep_dir.mkdir(parents=True, exist_ok=True)
            try:
                downloaded = asset_storage.download_from_manifest(
                    episode_number=episode_number,
                    manifest=manifest,
                    local_dir=str(ep_dir),
                )
                logger.info(
                    f"☁️ v22.7: Phase 3 rehydrated {downloaded} assets into "
                    f"{ep_dir}"
                )
            except Exception as e:
                raise RuntimeError(
                    f"Phase 3: failed to download Phase 2 assets from Supabase "
                    f"Storage: {type(e).__name__}: {e}. Cannot render."
                ) from e
        else:
            # No manifest in state. Three possible reasons:
            #   (a) Phase 2 ran with AssetStorage disabled (--skip-supabase
            #       or no Supabase credentials). Files might exist locally
            #       if Phase 2 ran on the same runner.
            #   (b) Tests with mocked orchestrators — Phase 3 won't really
            #       render, so file existence doesn't matter.
            #   (c) State was created by pre-v22.7 Phase 2 on a previous
            #       runner, with AssetStorage NOW available in this run.
            #       Files DON'T exist locally — render WILL fail.
            #
            # Only case (c) is a real bug we can detect cleanly: we expected
            # to use Storage (asset_storage is wired) but the state pre-dates
            # the storage layer. We fail fast there with a clear recovery
            # message. Cases (a) and (b) fall through to the renderer, which
            # either succeeds (files do exist) or fails naturally with its
            # own error message.
            mastered_map = state.asset_paths.get("mastered_map", {}) or {}
            if asset_storage is not None and mastered_map:
                existing = [
                    p for p in mastered_map.values()
                    if p and Path(p).is_file()
                ]
                if not existing:
                    raise RuntimeError(
                        "Phase 3 cannot start: AssetStorage IS wired in this "
                        "run, but state has no _storage_manifest AND none of "
                        "the mastered audio files exist on this runner's "
                        "filesystem.\n\n"
                        "Likely cause: state was created by Phase 2 on a "
                        "previous run (pre-v22.7 code, or AssetStorage was "
                        "unwired then) on a different GitHub Actions runner "
                        "whose filesystem is now gone.\n\n"
                        "Recovery: trigger the workflow manually with "
                        "PHASE=2 to re-run Phase 2 on the current codebase. "
                        "This will regenerate the assets AND upload them to "
                        "Supabase Storage. Phase 3 (auto-detected next day) "
                        "will then download them and render successfully."
                    )
                logger.info(
                    f"ℹ️ v22.7: Phase 3 has no manifest, but "
                    f"{len(existing)}/{len(mastered_map)} mastered audio "
                    f"files exist locally — assuming same-runner execution."
                )
            else:
                logger.warning(
                    "⚠️ v22.7: Phase 3 has no _storage_manifest in state. "
                    "Assuming same-runner execution. If files are missing, "
                    "the renderer will fail with its own error."
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
        ep_path = (
            self._orchestrator.paths.temp_episodes
            / f"episode_{episode_number:03d}.json"
        )
        if not ep_path.exists():
            logger.warning(
                f"⚠️ Episode JSON not found at {ep_path} — "
                f"Phase 1 may not have saved properly"
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
