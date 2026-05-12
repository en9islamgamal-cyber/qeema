"""
orchestrator.py — VALUE / QEEMA v22.7.6 — orchestrator (3-phase pipeline coordinator)
==========================================================
[v22.5 — Phase-aware Strategy-driven Orchestration]
Every decision point queries PipelineStrategy. No dead code paths.
Phases run on different days using independent Gemini API keys.

[v22.7.6 — three critical bug fixes]
  1. MERGE BUG (CRITICAL): _run_phase2_deep_visuals now writes the rich
     visual prompt directly into ayah_scenes[i].visual_prompt BEFORE
     saving the JSON. Previously it only wrote a top-level _deep_visuals
     key, which Pydantic's extra="ignore" silently dropped on reload —
     meaning every scene.visual_prompt stayed empty, Leonardo was skipped,
     and ALL scenes fell back to CSS. The "rebuild in-memory script after
     deep visuals" workaround in _run_pipeline never actually worked
     because the reload re-ran Pydantic validation on the SAME stale data.
     This fix puts the prompt where Pydantic looks for it.

  2. CLEANUP FILENAME BUG: _safe_cleanup wrote to
        videos/episode_branded_episode.mp4
     for EVERY episode (used branded.stem which is always
     "branded_episode"). Each new episode overwrote the previous local
     backup. Now uses episode_NNN.mp4 with the actual episode_number.

  3. ARTIFACT-FRIENDLY CLEANUP: _safe_cleanup used to MOVE branded out
     of temp/episodes/episode_NNN/. The v22.7 workflow uploads
     temp/episodes/*/branded_episode.mp4 as a GitHub Actions artifact,
     but it ran AFTER cleanup — so the file was gone. Now we COPY
     instead of move, keeping the original in place for the artifact
     step. The local archive at videos/episode_NNN.mp4 is a second copy.

[Pipeline Stages — driven by strategy + phase]

PHASE 1 — Day 1 (key #1): Script + Religious validation
  1. script             → Multi-task (1 Gemini call) OR legacy 6-call
  2. tafsir_validation  → Per-ayah Gemini, sequential, rate-limited
                          → Saves Phase 1 state to disk

PHASE 2 — Day 2 (keys #2, #3): Visual + Audio assets
  3. deep_visuals       → Batch (1 call, Key 3) OR legacy chained
                          → MERGED INTO scene.visual_prompt (v22.7.6 fix)
  4. tts_director       → Batch (1 call, Key 2) OR legacy
  5. ai_images          → Leonardo (now actually runs — was failing
                          silently due to empty visual_prompt before fix #1)
  6. audio              → ElevenLabs TTS (no Gemini quota)
  7. audio_master       → VoiceEngine.master_episode (FFmpeg)

PHASE 3 — Day 3 (no Gemini): Render + Publish
  8.  render_scenes     → 6-segment cinematic structure
  9.  concat_raw        → BGMMixer with crossfades
  10. bgm_mix           → Background music
  11. subtitles         → ASS subtitles burned in (if enabled)
  12. wrap_branded      → Intro + outro + CTA
  13. thumbnail         → 3 variants for A/B testing
  14. review_gate       → Bypassed when QEEMA_AUTO_APPROVE=true (v22.7)
  15. upload            → YouTube
  16. dashboard         → Per-episode + monthly markdown
  17. cleanup           → COPY (not move) so artifact upload sees the file
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.config import PathsConfig, VideoConfig
from core.exceptions import (
    PermanentError,
    PipelineError,
    QualityGateError,
    TransientError,
)
from core.idempotency import CheckpointStore, IdempotencyKey
from core.interfaces import (
    EpisodeRepository,
    IntroOutroBuilder,
    QualityValidator,
    SceneRenderRequest,
    ThumbnailBuilder,
    UploadRequest,
    VideoAssembler,
    VideoUploader,
    VisualRenderer,
)
from core.logging_setup import with_context
from core.models import EpisodeScript, EpisodeStatus
from core.observability import SpanEmitter, get_emitter, get_registry
from engines.script_engine import ScriptEngine
from engines.subtitle_engine import SubtitleEngine
from engines.voice_engine import VoiceEngine
from infrastructure.bgm_mixer import BGMMixer

if TYPE_CHECKING:
    from core.pipeline_strategy import PipelineStrategy, QualityMode, StrategyFactory

logger = logging.getLogger(__name__)

PIPELINE_VERSION: str = "22.7.6"


# ════════════════════════════════════════════════════════════════
# Result reporting
# ════════════════════════════════════════════════════════════════
@dataclass
class StageResult:
    name: str
    success: bool
    duration_sec: float
    detail: str = ""


@dataclass
class EpisodeRunReport:
    episode_number: int
    success: bool
    final_status: str
    total_duration_sec: float
    stages: List[StageResult] = field(default_factory=list)
    video_url: Optional[str] = None
    error: Optional[str] = None
    tafsir_validation: List[Dict[str, Any]] = field(default_factory=list)
    quality_score: Optional[float] = None
    cost_usd: Optional[float] = None
    strategy_summary: Optional[str] = None
    phase_run: Optional[str] = None
    next_phase: Optional[str] = None

    def summary(self) -> str:
        emoji = "✅" if self.success else "❌"
        lines = [
            f"{emoji} Episode {self.episode_number} — {self.final_status} "
            f"({self.total_duration_sec:.1f}s)"
        ]
        if self.strategy_summary:
            lines.append(f"  📋 {self.strategy_summary}")
        for s in self.stages:
            mark = "✓" if s.success else "✗"
            lines.append(
                f"  {mark} {s.name:<20} {s.duration_sec:>6.1f}s {s.detail}"
            )
        if self.video_url:
            lines.append(f"  📺 {self.video_url}")
        if self.error:
            lines.append(f"  ⚠️  {self.error}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Orchestrator v22.7.6
# ════════════════════════════════════════════════════════════════
class Orchestrator:
    """Production orchestrator with strategy-driven decision making."""

    def __init__(
        self,
        *,
        script_engine: ScriptEngine,
        voice_engine: VoiceEngine,
        visual_renderer: VisualRenderer,
        assembler: VideoAssembler,
        repository: EpisodeRepository,
        uploader: Optional[VideoUploader],
        intro_outro: IntroOutroBuilder,
        thumbnail_builder: ThumbnailBuilder,
        quality_validator: QualityValidator,
        paths: PathsConfig,
        video_cfg: VideoConfig,
        bgm_mixer: Optional[BGMMixer] = None,
        subtitle_engine: Optional[SubtitleEngine] = None,
        image_engine: Any = None,
        tafsir_validator: Any = None,
        hook_optimizer: Any = None,
        review_gate: Any = None,
        cost_tracker: Any = None,
        quota_manager: Any = None,
        cost_dashboard: Any = None,
        strategy_factory: Any = None,
        requested_mode: Any = None,
        has_multi_task_engine: bool = False,
        color_grades_by_emotion: Optional[Dict[str, str]] = None,
        approval_explicit: bool = False,
        dry_run: bool = False,
        enable_subtitles: bool = True,
        enable_color_grade: bool = True,
        enable_crossfades: bool = True,
    ) -> None:
        self.script_engine = script_engine
        self.voice_engine = voice_engine
        self.visual_renderer = visual_renderer
        self.assembler = assembler
        self.repository = repository
        self.uploader = uploader
        self.intro_outro = intro_outro
        self.thumbnail_builder = thumbnail_builder
        self.quality_validator = quality_validator
        self.paths = paths
        self.video_cfg = video_cfg
        self.dry_run = dry_run

        self.bgm_mixer = bgm_mixer or BGMMixer(paths=paths)
        self.subtitle_engine = subtitle_engine
        self.image_engine = image_engine
        self.tafsir_validator = tafsir_validator
        self.hook_optimizer = hook_optimizer
        self.review_gate = review_gate
        self.cost_tracker = cost_tracker
        self.quota_manager = quota_manager
        self.cost_dashboard = cost_dashboard
        self.strategy_factory = strategy_factory
        self.requested_mode = requested_mode
        self.has_multi_task_engine = has_multi_task_engine

        self.color_grades_by_emotion = color_grades_by_emotion or {}
        self.approval_explicit = approval_explicit

        self.enable_subtitles = enable_subtitles
        self.enable_color_grade = enable_color_grade
        self.enable_crossfades = enable_crossfades
        env_bgm = os.getenv("ENABLE_BGM")
        self.enable_bgm = (
            env_bgm.lower() == "true" if env_bgm is not None else True
        )

        if self.enable_subtitles and self.subtitle_engine is None:
            try:
                self.subtitle_engine = SubtitleEngine(paths=paths)
            except Exception as e:
                logger.warning(f"⚠️ Subtitle engine init failed: {e}")
                self.enable_subtitles = False

        self._shutdown_requested: bool = False
        self._current_strategy: Optional[Any] = None

        checkpoints_root = paths.root / "state" / "checkpoints"
        self._checkpoints = CheckpointStore(checkpoints_root)
        self._emitter: SpanEmitter = get_emitter()

    def warmup(self) -> None:
        logger.info("🔥 Warming up v22.7.6 orchestrator")
        self.visual_renderer.warmup()
        self.intro_outro.build_intro()
        self.intro_outro.build_outro()
        logger.info("✅ Orchestrator v22.7.6 warm")

    def shutdown(self) -> None:
        logger.info("🧹 Shutting down orchestrator")
        try:
            self.visual_renderer.shutdown()
        except Exception as e:
            logger.warning(f"⚠️ visual_renderer shutdown error: {e}")

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        logger.warning("⚠️ Shutdown requested")

    def _compute_strategy(self, episode_number: int) -> Any:
        if self.strategy_factory is None:
            from core.pipeline_strategy import (
                StrategyFactory as _SF, QualityMode as _QM,
            )
            factory = _SF
            requested = self.requested_mode or _QM.AUTO
        else:
            factory = self.strategy_factory
            requested = self.requested_mode

        strategy = factory.build(
            requested_mode=requested,
            quota_manager=self.quota_manager,
            episode_number=episode_number,
            has_tafsir_validator=self.tafsir_validator is not None,
            has_leonardo_engine=self.image_engine is not None,
            has_multi_task_engine=self.has_multi_task_engine,
        )
        logger.info(strategy.detailed_report())
        return strategy

    def run_next(self) -> Optional[EpisodeRunReport]:
        record = self.repository.get_pending()
        if not record:
            logger.info("📭 No pending episodes")
            return None
        return self.run(record["episode_number"])

    def run(
        self,
        episode_number: int,
        *,
        phase: Any = None,
    ) -> EpisodeRunReport:
        from core.models import EpisodePhase
        if phase is None:
            resolved_phase = EpisodePhase.ALL
        elif isinstance(phase, EpisodePhase):
            resolved_phase = phase
        elif isinstance(phase, str):
            try:
                resolved_phase = EpisodePhase(phase.lower())
            except ValueError:
                logger.warning(f"⚠️ Unknown phase '{phase}', defaulting to ALL")
                resolved_phase = EpisodePhase.ALL
        else:
            resolved_phase = EpisodePhase.ALL

        if resolved_phase == EpisodePhase.AUTO:
            try:
                record = self.repository.get_or_create(episode_number)
                current_status = EpisodeStatus(
                    record.get("status", EpisodeStatus.PENDING.value)
                )
                resolved_phase = EpisodePhase.next_phase_for_status(current_status)
                logger.info(
                    f"🎯 AUTO phase resolved: episode {episode_number} status="
                    f"'{current_status.value}' → running {resolved_phase.value}"
                )
            except Exception as e:
                logger.warning(f"⚠️ AUTO phase resolution failed: {e}, defaulting to PHASE_1")
                resolved_phase = EpisodePhase.PHASE_1

        report = EpisodeRunReport(
            episode_number=episode_number,
            success=False,
            final_status="UNKNOWN",
            total_duration_sec=0.0,
        )
        start = time.monotonic()
        log = with_context(
            logger, episode_number=episode_number, stage="orchestrator",
        )

        try:
            self._current_strategy = self._compute_strategy(episode_number)
            report.strategy_summary = self._current_strategy.summary()
        except Exception as e:
            log.error(f"❌ Strategy computation failed: {e}")
            report.error = f"Strategy error: {e}"
            return report

        idem_key = IdempotencyKey.derive(
            episode_number=episode_number,
            pipeline_version=PIPELINE_VERSION,
            inputs={
                "voice_id": self.voice_engine._primary_voice_id(),
                "video_resolution": (
                    f"{self.video_cfg.width}x{self.video_cfg.height}"
                ),
                "dry_run": self.dry_run,
                "mode": self._current_strategy.mode.value,
            },
        )
        self._checkpoints.initialize(idem_key, episode_number=episode_number)

        report.phase_run = resolved_phase.value
        with self._emitter.span(
            "episode.run",
            episode_number=episode_number,
            pipeline_version=PIPELINE_VERSION,
            dry_run=self.dry_run,
            mode=self._current_strategy.mode.value,
            phase=resolved_phase.value,
        ):
            return self._run_pipeline(
                episode_number=episode_number,
                report=report,
                start=start,
                log=log,
                idem_key=idem_key,
                phase=resolved_phase,
            )

    def _phase_state_path(self, episode_number: int) -> Path:
        ep_dir = self.paths.temp_episodes / f"episode_{episode_number:03d}"
        ep_dir.mkdir(parents=True, exist_ok=True)
        return ep_dir / "_phase_state.json"

    def _save_phase_state(self, episode_number: int, **kwargs: Any) -> None:
        state_path = self._phase_state_path(episode_number)
        existing: Dict[str, Any] = {}
        if state_path.exists():
            try:
                with open(state_path, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass

        def _serialize(obj: Any) -> Any:
            if isinstance(obj, Path):
                return str(obj)
            if isinstance(obj, dict):
                return {k: _serialize(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_serialize(x) for x in obj]
            return obj

        existing.update({k: _serialize(v) for k, v in kwargs.items()})

        tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        tmp_path.replace(state_path)

    def _load_phase_state(self, episode_number: int) -> Dict[str, Any]:
        state_path = self._phase_state_path(episode_number)
        if not state_path.exists():
            return {}
        try:
            with open(state_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ Failed to load phase state: {e}")
            return {}

    def _reload_episode_script(
        self, episode_number: int,
    ) -> Optional[EpisodeScript]:
        ep_json_path = (
            self.paths.temp_episodes
            / f"episode_{episode_number:03d}.json"
        )

        try:
            from core.phase_state import PhaseStateManager
            psm = PhaseStateManager(Path("state/phases"))
            persistent_state = psm.load(episode_number)
            persistent_script = getattr(persistent_state, "script_data", None)
            if persistent_script and not ep_json_path.exists():
                ep_json_path.parent.mkdir(parents=True, exist_ok=True)
                with open(ep_json_path, "w", encoding="utf-8") as f:
                    json.dump(persistent_script, f, ensure_ascii=False, indent=2)
                scene_count = len(persistent_script.get("ayah_scenes", []))
                logger.info(
                    f"♻️ Restored episode JSON from persistent phase state "
                    f"({scene_count} scenes) — Phase 2/3 will not regenerate"
                )
        except Exception as e:
            logger.warning(f"⚠️ Persistent state restore failed: {e}")

        if hasattr(self.script_engine, "load_from_disk"):
            try:
                cached = self.script_engine.load_from_disk(episode_number)
                if cached is not None:
                    return cached
            except Exception as e:
                logger.warning(f"⚠️ load_from_disk failed: {e}")

        try:
            from engines.script_engine_unified import UnifiedScriptEngine
            unified = UnifiedScriptEngine(
                legacy_engine=self.script_engine,
                hook_optimizer=self.hook_optimizer,
            )
            return unified.generate(
                episode_number, strategy=self._current_strategy,
            )
        except Exception as e:
            logger.error(f"❌ Could not reload script for episode {episode_number}: {e}")
            return None

    def _phase2_gemini_adapter(self) -> Optional[Any]:
        return self._phase2_tts_gemini_adapter()

    def _phase2_tts_gemini_adapter(self) -> Optional[Any]:
        return self._build_phase2_adapter(
            primary_env="GEMINI_API_KEY_2",
            instance_name="phase2-gemini-tts",
            purpose="TTS",
        )

    def _phase2_visual_gemini_adapter(self) -> Optional[Any]:
        return self._build_phase2_adapter(
            primary_env="GEMINI_API_KEY_3",
            instance_name="phase2-gemini-visual",
            purpose="visual prompts",
        )

    @staticmethod
    def _build_phase2_adapter(
        *, primary_env: str, instance_name: str, purpose: str,
    ) -> Optional[Any]:
        try:
            key = (
                os.getenv(primary_env)
                or os.getenv("GEMINI_API_KEY_2")
                or os.getenv("GEMINI_API_KEY")
                or ""
            )
            if not key:
                logger.warning(
                    f"⚠️ No Phase 2 Gemini key for {purpose} — "
                    f"set {primary_env} (or GEMINI_API_KEY_2 as fallback)"
                )
                return None
            from infrastructure.llm_adapters import GeminiJsonAdapter
            return GeminiJsonAdapter(
                key, model="gemini-2.5-flash",
                instance_name=instance_name,
            )
        except Exception as e:
            logger.warning(
                f"⚠️ Phase 2 Gemini adapter init failed for {purpose}: {e}"
            )
            return None

    # ════════════════════════════════════════════════════════════════
    # v22.7.6 CRITICAL FIX: deep visuals now merge into scene.visual_prompt
    # ════════════════════════════════════════════════════════════════
    def _run_phase2_deep_visuals(
        self, episode_number: int, script: Any,
    ) -> None:
        """v22.7.6: Generate deep visual prompts AND merge them into the
        per-scene visual_prompt field.

        Previously this method only wrote a top-level `_deep_visuals` key.
        Pydantic's extra="ignore" silently dropped it on EpisodeScript
        reload, leaving every scene.visual_prompt empty → Leonardo got
        empty prompts → all 7 scenes fell back to CSS rendering.

        Now we ALSO update each ayah_scenes[i]["visual_prompt"] with a
        rich string built from the 14-field deep visual dict. When the
        script is reloaded after this method, the Pydantic-validated
        AyahScene has its visual_prompt field populated and Leonardo
        actually has something to work with.
        """
        ep_path = (
            self.paths.temp_episodes / f"episode_{episode_number:03d}.json"
        )
        if not ep_path.exists():
            logger.warning(
                f"⚠️ Phase 2 deep visuals: episode JSON not found at {ep_path}"
            )
            return

        try:
            with open(ep_path, encoding="utf-8") as f:
                episode_data = json.load(f)
        except Exception as e:
            logger.warning(
                f"⚠️ Phase 2 deep visuals: cannot read episode JSON: {e}"
            )
            return

        ayah_scenes = episode_data.get("ayah_scenes", []) or []
        if not ayah_scenes:
            logger.warning(
                "⚠️ Phase 2 deep visuals: no ayah_scenes in episode JSON"
            )
            return

        # Path 1: BatchVisualPromptEngine on Key 3 (1 call)
        deep_visuals_payload = self._try_batch_visual_prompts(ayah_scenes)

        # Path 2: legacy chained DeepVisualPromptGenerator on Key 2
        if deep_visuals_payload is None:
            logger.info(
                "📉 Phase 2 deep visuals: batch path unavailable/failed — "
                "falling back to legacy chained generator"
            )
            deep_visuals_payload = self._legacy_deep_visuals(ayah_scenes)

        if deep_visuals_payload is None:
            logger.warning(
                "⚠️ Phase 2 deep visuals: both batch and legacy paths failed — "
                "Leonardo will use shallow visual_subject/action from script"
            )
            return

        # Persist top-level _deep_visuals (legacy callers expect it here)
        episode_data["_deep_visuals"] = deep_visuals_payload

        # ════════════════════════════════════════════════════════════════
        # v22.7.6 CRITICAL FIX: merge rich prompts into scene.visual_prompt
        # ════════════════════════════════════════════════════════════════
        # Without this loop, the Pydantic-validated EpisodeScript reload
        # silently drops _deep_visuals and every scene.visual_prompt stays
        # empty. By writing into ayah_scenes[i]["visual_prompt"] directly,
        # we put the data where Pydantic's AyahScene.visual_prompt field
        # actually looks for it.
        try:
            from engines.visual_prompt_engineer import VisualPromptEngineer
            _builder: Any = VisualPromptEngineer
        except ImportError:
            _builder = None

        merged_count = 0
        for i, scene_dict in enumerate(ayah_scenes):
            if i >= len(deep_visuals_payload):
                break
            dv = deep_visuals_payload[i]
            if not dv.get("is_usable"):
                continue
            try:
                # Preferred path: use VisualPromptEngineer's builder so the
                # locked style template (positive + negative prompts) is
                # applied consistently with the standalone-call code path.
                prompt: str = ""
                if (
                    _builder is not None
                    and hasattr(_builder, "build_from_deep_result")
                ):
                    try:
                        prompt = _builder.build_from_deep_result(dv) or ""
                    except Exception as e:
                        logger.debug(
                            f"build_from_deep_result failed for ayah index "
                            f"{i}: {e} — using simple join fallback"
                        )
                        prompt = ""
                # Defensive fallback: simple comma-joined concatenation.
                # Loses the locked style template but produces a usable
                # Leonardo prompt instead of empty string.
                if not prompt:
                    parts = [
                        dv.get("subject", ""),
                        dv.get("action", ""),
                        dv.get("environment", ""),
                        dv.get("time_of_day", ""),
                        dv.get("mood", ""),
                        dv.get("color_palette", ""),
                        dv.get("lighting_direction", ""),
                        dv.get("camera_angle", ""),
                        dv.get("depth_of_field", ""),
                        dv.get("foreground", ""),
                        dv.get("midground", ""),
                        dv.get("background", ""),
                        dv.get("focal_point", ""),
                    ]
                    prompt = ", ".join(p for p in parts if p and p.strip())
                if prompt:
                    scene_dict["visual_prompt"] = prompt
                    merged_count += 1
            except Exception as e:
                logger.warning(
                    f"⚠️ Could not merge visual_prompt for ayah index {i}: {e}"
                )

        try:
            with open(ep_path, "w", encoding="utf-8") as f:
                json.dump(episode_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(
                f"⚠️ Phase 2 deep visuals: write-back failed: {e}"
            )
            return

        usable = sum(1 for d in deep_visuals_payload if d.get("is_usable"))
        logger.info(
            f"🎨 Phase 2 deep visuals: {usable}/{len(deep_visuals_payload)} "
            f"usable; merged {merged_count} prompts into ayah_scenes "
            f"(v22.7.6 fix — Leonardo will now receive non-empty prompts)"
        )

    def _try_batch_visual_prompts(
        self, ayah_scenes: List[Dict[str, Any]],
    ) -> Optional[List[Dict[str, Any]]]:
        adapter = self._phase2_visual_gemini_adapter()
        if adapter is None or getattr(adapter, "_client", None) is None:
            return None

        ayah_scripts: List[Dict[str, Any]] = []
        for i, scene in enumerate(ayah_scenes, start=1):
            ayah_obj = scene.get("ayah") or {}
            num = (
                ayah_obj.get("number") if isinstance(ayah_obj, dict)
                else getattr(ayah_obj, "number", i)
            ) or i
            ayah_scripts.append({
                "number": num,
                "explain": scene.get("explain_text", ""),
                "story": scene.get("story_text") or scene.get("analogy_text", ""),
                "emotion": scene.get("scene_emotion", "warm"),
            })

        try:
            from engines.batch_engines import BatchVisualPromptEngine
            engine = BatchVisualPromptEngine(adapter._client)
            logger.info(
                f"🎨 Phase 2 visuals (BATCH v22.6 — 1 Gemini call on Key 3, "
                f"{len(ayah_scripts)} ayahs)"
            )
            result = engine.generate_visuals(ayah_scripts)
            if result is None:
                return None
            return BatchVisualPromptEngine.to_legacy_dicts(result)
        except Exception as e:
            logger.warning(
                f"⚠️ Batch visual prompts failed ({type(e).__name__}: {e})"
            )
            return None

    def _legacy_deep_visuals(
        self, ayah_scenes: List[Dict[str, Any]],
    ) -> Optional[List[Dict[str, Any]]]:
        adapter = self._phase2_tts_gemini_adapter()
        if adapter is None:
            return None

        try:
            from engines.visual_prompt_deep import DeepVisualPromptGenerator
            deep_gen = DeepVisualPromptGenerator(adapter)
            results = deep_gen.generate_for_episode(
                ayah_scenes, max_workers=1,
            )
            return [
                {
                    "subject": r.subject, "action": r.action,
                    "environment": r.environment, "time_of_day": r.time_of_day,
                    "mood": r.mood, "color_palette": r.color_palette,
                    "lighting_direction": r.lighting_direction,
                    "atmospheric_elements": r.atmospheric_elements,
                    "camera_angle": r.camera_angle,
                    "depth_of_field": r.depth_of_field,
                    "foreground": r.foreground, "midground": r.midground,
                    "background": r.background, "focal_point": r.focal_point,
                    "layers_completed": r.layers_completed,
                    "is_usable": r.is_usable,
                }
                for r in results
            ]
        except Exception as e:
            logger.warning(
                f"⚠️ Legacy deep visuals failed ({type(e).__name__}: {e})"
            )
            return None

    def _run_phase2_tts_director(
        self, episode_number: int, script: Any,
    ) -> None:
        ep_path = (
            self.paths.temp_episodes / f"episode_{episode_number:03d}.json"
        )
        if not ep_path.exists():
            logger.warning(
                f"⚠️ Phase 2 TTS director: episode JSON not found at {ep_path}"
            )
            return

        try:
            with open(ep_path, encoding="utf-8") as f:
                episode_data = json.load(f)
        except Exception as e:
            logger.warning(
                f"⚠️ Phase 2 TTS director: cannot read episode JSON: {e}"
            )
            return

        directions_dict = self._try_batch_tts(episode_data)
        if directions_dict is None:
            logger.info(
                "📉 Phase 2 TTS: batch path unavailable/failed — "
                "falling back to legacy TTSDirector"
            )
            directions_dict = self._legacy_tts(episode_data)

        if not directions_dict:
            logger.info(
                "ℹ️ Phase 2 TTS director: no directions produced — "
                "audio will use base voice settings"
            )
            return

        episode_data["_tts_directions"] = directions_dict
        try:
            with open(ep_path, "w", encoding="utf-8") as f:
                json.dump(episode_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(
                f"⚠️ Phase 2 TTS director: write-back failed: {e}"
            )
            return

        logger.info(
            f"🎙️ Phase 2 TTS director: {len(directions_dict)} segments "
            f"directed with SSML"
        )

    def _try_batch_tts(
        self, episode_data: Dict[str, Any],
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        adapter = self._phase2_tts_gemini_adapter()
        if adapter is None or getattr(adapter, "_client", None) is None:
            return None

        try:
            from engines.batch_engines import BatchTTSDirector
            engine = BatchTTSDirector(adapter._client)
            logger.info(
                "🎙️ Phase 2 TTS (BATCH v22.6 — 1 Gemini call on Key 2)"
            )
            result = engine.direct_episode(episode_data)
            if result is None:
                return None
            return BatchTTSDirector.to_legacy_dict(result)
        except Exception as e:
            logger.warning(
                f"⚠️ Batch TTS director failed ({type(e).__name__}: {e})"
            )
            return None

    def _legacy_tts(
        self, episode_data: Dict[str, Any],
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        adapter = self._phase2_tts_gemini_adapter()
        if adapter is None:
            return None

        try:
            from engines.tts_director import TTSDirector
            director = TTSDirector(adapter)
            episode_direction = director.direct_episode(
                episode_data, max_retries=1,
            )
            segments = getattr(episode_direction, "segments", {}) or {}
            if not segments:
                return None

            if getattr(episode_direction, "fallback_used", False):
                logger.warning(
                    "⚠️ Legacy TTS produced fallback segments only "
                    "(Gemini JSON parse failed); no SSML applied"
                )
                return None

            return {
                sd.segment_id: {
                    "directed_text": sd.directed_text,
                    "pace": sd.pace,
                    "pronunciation_notes": list(sd.pronunciation_notes),
                }
                for sd in segments.values()
            }
        except Exception as e:
            logger.warning(
                f"⚠️ Legacy TTS director failed ({type(e).__name__}: {e})"
            )
            return None

    # ─── Main pipeline ───────────────────────────────────────────
    def _run_pipeline(
        self, *,
        episode_number: int,
        report: EpisodeRunReport,
        start: float,
        log: logging.Logger,
        idem_key: Any,
        phase: Any = None,
    ) -> EpisodeRunReport:
        strategy = self._current_strategy
        episode_id: Optional[str] = None

        from core.models import EpisodePhase
        if phase is None:
            phase = EpisodePhase.ALL
        run_phase1 = phase in (EpisodePhase.ALL, EpisodePhase.PHASE_1)
        run_phase2 = phase in (EpisodePhase.ALL, EpisodePhase.PHASE_2)
        run_phase3 = phase in (EpisodePhase.ALL, EpisodePhase.PHASE_3)
        log.info(
            f"🎬 Phase plan: phase1={run_phase1} phase2={run_phase2} phase3={run_phase3}"
        )

        try:
            record = self.repository.get_or_create(episode_number)
            episode_id = record["id"]
            self.repository.update_status(
                episode_id, EpisodeStatus.PROCESSING.value,
            )
        except Exception as e:
            report.final_status = "REPO_FAILED"
            report.error = f"Repository error: {e}"
            log.error(f"❌ {report.error}")
            return report

        ep_dir = self.paths.temp_episodes / f"episode_{episode_number:03d}"
        ep_dir.mkdir(parents=True, exist_ok=True)

        try:
            script: Optional[EpisodeScript] = None

            # ════════════════════════════════════════════════════════
            # PHASE 1 — Day 1 (key #1): Script + Tafsir validation
            # ════════════════════════════════════════════════════════
            if run_phase1:
                script_call = self._make_script_call(episode_number, strategy)
                script = self._run_stage(
                    "script",
                    script_call,
                    report,
                )
                script.episode_id = episode_id
                self._check_shutdown()

                if self.tafsir_validator is not None:
                    tafsir_results = self._run_stage(
                        "tafsir_validation",
                        lambda: self._validate_tafsir(script, strategy),
                        report,
                    )
                    report.tafsir_validation = tafsir_results
                    rejected = [
                        r for r in tafsir_results if not r.get("passed", False)
                    ]
                    if rejected:
                        concerns = [
                            f"Ayah {r.get('ayah', '?')}: {c}"
                            for r in rejected
                            for c in r.get("concerns", [])
                        ]
                        raise QualityGateError(
                            f"Tafsir validation FAILED for "
                            f"{len(rejected)} ayah(s)",
                            critiques=concerns,
                            episode_number=episode_number,
                            stage="tafsir_validation",
                        )
                    method = (
                        tafsir_results[0].get("method")
                        if tafsir_results else "n/a"
                    )
                    logger.info(
                        f"✅ Tafsir validation passed: "
                        f"{len(tafsir_results)} ayahs (method={method})"
                    )
                else:
                    logger.warning(
                        "⚠️ Tafsir validation skipped — no validator wired "
                        "(GEMINI_API_KEY missing). NOT recommended for production."
                    )

                self._save_phase_state(
                    episode_number,
                    phase1_completed_at=time.time(),
                )
                if phase == EpisodePhase.PHASE_1:
                    try:
                        self.repository.update_status(
                            episode_id, EpisodeStatus.SCRIPT_READY.value,
                        )
                        log.info(
                            f"📝 Phase 1 done — episode {episode_number} "
                            f"marked as SCRIPT_READY (awaiting Phase 2)"
                        )
                    except Exception as e:
                        log.warning(f"⚠️ Status update failed: {e}")

                    report.success = True
                    report.final_status = EpisodeStatus.SCRIPT_READY.value
                    report.next_phase = EpisodePhase.PHASE_2.value
                    report.total_duration_sec = time.monotonic() - start
                    log.info(
                        f"✅ Phase 1 complete in {report.total_duration_sec:.1f}s"
                    )
                    return report

            # ════════════════════════════════════════════════════════
            # PHASE 2 — Asset generation (Leonardo + ElevenLabs)
            # ════════════════════════════════════════════════════════
            if run_phase2:
                if script is None:
                    log.info(
                        f"📖 Phase 2 standalone — reloading episode {episode_number} script"
                    )
                    script = self._reload_episode_script(episode_number)
                    if script is None:
                        raise PipelineError(
                            f"Could not reload script for Phase 2 "
                            f"(episode {episode_number} — was Phase 1 run?)",
                            stage="phase2_reload",
                        )
                    script.episode_id = episode_id

                # v22.7.6: deep visuals NOW merge into scene.visual_prompt
                self._run_phase2_deep_visuals(episode_number, script)

                # Refresh script from disk so its visual_prompt fields reflect
                # the just-merged deep visuals. With the v22.7.6 fix, this
                # reload actually picks up the prompts (previously it was a
                # no-op because the data was only at top-level _deep_visuals).
                refreshed = self._reload_episode_script(episode_number)
                if refreshed is not None:
                    refreshed.episode_id = script.episode_id
                    script = refreshed
                    # Sanity-check: log how many scenes now have a prompt
                    populated = sum(
                        1 for s in script.ayah_scenes
                        if getattr(s, "visual_prompt", None)
                    )
                    log.info(
                        f"🎨 After deep-visual merge: {populated}/"
                        f"{len(script.ayah_scenes)} ayah scenes have a "
                        f"populated visual_prompt"
                    )
                else:
                    log.warning(
                        "⚠️ Could not refresh script after deep visuals — "
                        "AI images may use stale (empty) visual_prompt"
                    )

                self._run_phase2_tts_director(episode_number, script)

                self._run_stage(
                    "ai_images",
                    lambda: self._generate_ai_images(
                        script, ep_dir, strategy,
                    ),
                    report,
                )

                audio_map = self._run_stage(
                    "audio",
                    lambda: self._generate_audio(script, ep_dir, strategy),
                    report,
                )

                mastered = self._run_stage(
                    "audio_master",
                    lambda: self.voice_engine.master_episode(audio_map, ep_dir),
                    report,
                )
                self._check_shutdown()

                self._save_phase_state(
                    episode_number,
                    audio_map=audio_map,
                    mastered_map=mastered,
                    phase2_completed_at=time.time(),
                )
                if phase == EpisodePhase.PHASE_2:
                    try:
                        self.repository.update_status(
                            episode_id, EpisodeStatus.ASSETS_READY.value,
                        )
                        log.info(
                            f"🎨 Phase 2 done — episode {episode_number} "
                            f"marked as ASSETS_READY (awaiting Phase 3)"
                        )
                    except Exception as e:
                        log.warning(f"⚠️ Status update failed: {e}")

                    report.success = True
                    report.final_status = EpisodeStatus.ASSETS_READY.value
                    report.next_phase = EpisodePhase.PHASE_3.value
                    report.total_duration_sec = time.monotonic() - start
                    log.info(
                        f"✅ Phase 2 complete in {report.total_duration_sec:.1f}s"
                    )
                    return report

            # ════════════════════════════════════════════════════════
            # PHASE 3 — Render + Publish
            # ════════════════════════════════════════════════════════
            if run_phase3:
                if script is None:
                    log.info(
                        f"📖 Phase 3 standalone — reloading episode {episode_number}"
                    )

                    try:
                        from core.phase_state import PhaseStateManager
                        psm = PhaseStateManager(Path("state/phases"))
                        persistent = psm.load(episode_number)
                        ep_json_path = (
                            self.paths.temp_episodes
                            / f"episode_{episode_number:03d}.json"
                        )
                        if persistent.script_data and not ep_json_path.exists():
                            ep_json_path.parent.mkdir(parents=True, exist_ok=True)
                            payload = dict(persistent.script_data)
                            asset_paths = persistent.asset_paths or {}
                            if asset_paths.get("_deep_visuals"):
                                payload["_deep_visuals"] = (
                                    asset_paths["_deep_visuals"]
                                )
                            if asset_paths.get("_tts_directions"):
                                payload["_tts_directions"] = (
                                    asset_paths["_tts_directions"]
                                )
                            with open(ep_json_path, "w", encoding="utf-8") as f:
                                json.dump(
                                    payload, f, ensure_ascii=False, indent=2,
                                )
                            log.info(
                                f"♻️ Phase 3 hydrated temp JSON from "
                                f"persistent state "
                                f"(deep_visuals={len(asset_paths.get('_deep_visuals', []))}, "
                                f"tts_dirs={len(asset_paths.get('_tts_directions', {}))})"
                            )
                    except Exception as e:
                        log.warning(
                            f"⚠️ Phase 3 hydration from persistent state failed: {e}"
                        )

                    script = self._reload_episode_script(episode_number)
                    if script is None:
                        raise PipelineError(
                            f"Could not reload script for Phase 3 "
                            f"(episode {episode_number})",
                            stage="phase3_reload",
                        )
                    script.episode_id = episode_id

                    try:
                        from core.phase_state import PhaseStateManager
                        psm = PhaseStateManager(Path("state/phases"))
                        persistent = psm.load(episode_number)
                        asset_paths = persistent.asset_paths or {}
                        audio_map = asset_paths.get("audio_map", {})
                        mastered = asset_paths.get("mastered_map", {})
                    except Exception:
                        legacy_state = self._load_phase_state(episode_number)
                        audio_map = legacy_state.get("audio_map", {})
                        mastered = legacy_state.get("mastered_map", {})

                    if not mastered:
                        raise PipelineError(
                            f"Phase 3 needs mastered audio from Phase 2 — "
                            f"none found for episode {episode_number}",
                            stage="phase3_reload",
                        )

                scene_segments = self._run_stage(
                    "render_scenes",
                    lambda: self._render_all_scenes(script, mastered, ep_dir),
                    report,
                )
                self._check_shutdown()

                raw_video = ep_dir / "raw_episode.mp4"
                self._run_stage(
                    "concat_raw",
                    lambda: self._concat_scenes(scene_segments, str(raw_video), script=script),
                    report,
                )

                bgm_video = ep_dir / "bgm_episode.mp4"
                self._run_stage(
                    "bgm_mix",
                    lambda: self._apply_bgm_smart(
                        str(raw_video), str(bgm_video), script, scene_segments,
                    ),
                    report,
                )
                bgm_result = (
                    str(bgm_video) if bgm_video.exists() else str(raw_video)
                )

                post_subs = bgm_result
                if strategy.enable_subtitles and self.subtitle_engine is not None:
                    subs_video = ep_dir / "subs_episode.mp4"
                    try:
                        timing_map = (
                            self.subtitle_engine.build_timing_map_from_audio(
                                mastered,
                            )
                        )
                        ass_path = self.subtitle_engine.generate(
                            script, timing_map, ep_dir / "subs",
                        )

                        try:
                            from engines.subtitle_typography import validate_ass_file
                            typo_report = validate_ass_file(ass_path)
                            if not typo_report.is_valid:
                                logger.warning(
                                    f"⚠️ Subtitle typography: {typo_report.summary()}"
                                )
                                for issue in typo_report.issues[:3]:
                                    logger.warning(f"  {issue}")
                            elif typo_report.has_warnings:
                                logger.info(typo_report.summary())
                        except (ImportError, Exception) as e:
                            logger.debug(f"Subtitle typography check skipped: {e}")

                        post_subs_result = self.bgm_mixer.burn_subtitles(
                            bgm_result, ass_path, str(subs_video),
                        )
                        post_subs = post_subs_result
                        report.stages.append(StageResult(
                            "subtitles", True, 0.0, ass_path,
                        ))
                    except Exception as e:
                        logger.warning(
                            f"⚠️ Subtitles failed (continuing without): {e}"
                        )
                        report.stages.append(StageResult(
                            "subtitles", False, 0.0, str(e)[:100],
                        ))

                cta_audio = mastered.get("cta")
                branded = ep_dir / "branded_episode.mp4"
                self._run_stage(
                    "wrap_branded",
                    lambda: self.intro_outro.wrap_episode(
                        post_subs, str(branded), cta_audio_path=cta_audio,
                    ),
                    report,
                )

                thumbs: List[str] = []
                if hasattr(self.thumbnail_builder, 'create_variants'):
                    thumbs = self._run_stage(
                        "thumbnail_variants",
                        lambda: self.thumbnail_builder.create_variants(
                            script, episode_number,
                        ),
                        report,
                    )
                    thumb = thumbs[0] if thumbs else None
                else:
                    thumb = self._run_stage(
                        "thumbnail",
                        lambda: self.thumbnail_builder.create(
                            script, episode_number,
                        ),
                        report,
                    )
                    thumbs = [thumb] if thumb else []

                if self.review_gate is not None:
                    validation_summary = {
                        "quality_score": report.quality_score,
                        "tafsir_validation": report.tafsir_validation,
                        "stages": [
                            {
                                "name": s.name,
                                "passed": s.success,
                                "duration_sec": s.duration_sec,
                            }
                            for s in report.stages
                        ],
                        "strategy": report.strategy_summary,
                    }
                    verdict = self.review_gate.check(
                        episode_number=episode_number,
                        script=script,
                        validation_summary=validation_summary,
                        video_path=str(branded),
                        approval_explicit=self.approval_explicit,
                    )
                    if not verdict.approved:
                        log.warning(
                            f"⏸ Review gate BLOCKED publication: "
                            f"{verdict.reason}"
                        )
                        log.warning(
                            f"📋 Review summary: {verdict.review_file}"
                        )
                        self.repository.update_status(
                            episode_id, "awaiting_review",
                            youtube_url=None,
                        )
                        report.video_url = f"file://{branded}"
                        report.success = True
                        report.final_status = "awaiting_review"
                        self._write_dashboards(report)
                        return report

                if self.dry_run:
                    log.info("🧪 DRY RUN: skipping upload")
                    report.video_url = "dry-run-no-upload"
                elif self.uploader is None:
                    raise RuntimeError("No uploader configured")
                else:
                    def _do_upload() -> dict:
                        result = self.uploader.upload(UploadRequest(
                            video_path=str(branded),
                            title=script.youtube_title,
                            description=script.youtube_description,
                            tags=list(script.youtube_tags),
                            thumbnail_path=thumb,
                        ))
                        return {
                            "video_id": result.video_id,
                            "video_url": result.video_url,
                        }

                    upload_result = self._run_stage(
                        "upload", _do_upload, report, idem_key=idem_key,
                    )
                    report.video_url = upload_result["video_url"]

                    if (
                        len(thumbs) > 1
                        and hasattr(self.uploader, "upload_thumbnail_variant")
                    ):
                        for i, t in enumerate(thumbs[1:], start=2):
                            try:
                                self.uploader.upload_thumbnail_variant(
                                    upload_result["video_id"], t, slot=i,
                                )
                                log.info(
                                    f"✅ Uploaded thumbnail variant {i}"
                                )
                            except Exception as e:
                                log.warning(
                                    f"⚠️ Thumbnail variant {i} "
                                    f"upload failed: {e}"
                                )

                self.repository.update_status(
                    episode_id, EpisodeStatus.COMPLETED.value,
                    youtube_url=report.video_url,
                )

                if self.quota_manager is not None:
                    try:
                        self.quota_manager.episode_started()
                    except Exception as e:
                        log.warning(f"⚠️ Quota update failed: {e}")

                # v22.7.6: pass episode_number so cleanup uses correct
                # filename. Also note: cleanup now COPIES branded to the
                # archive (instead of moving) so the workflow artifact
                # upload step can still find it in temp/episodes/.
                self._safe_cleanup(
                    ep_dir, branded, Path(bgm_result), scene_segments,
                    episode_number=episode_number,
                )
                report.success = True
                report.final_status = EpisodeStatus.COMPLETED.value

        except QualityGateError as e:
            self._mark_failure(
                report, episode_id,
                EpisodeStatus.FAILED_QUALITY.value, str(e),
            )
        except PermanentError as e:
            self._mark_failure(
                report, episode_id,
                EpisodeStatus.FAILED_PERMANENT.value, str(e),
            )
        except (TransientError, PipelineError) as e:
            self._mark_failure(
                report, episode_id,
                EpisodeStatus.FAILED.value,
                f"{type(e).__name__}: {e}",
            )
        except Exception as e:
            self._mark_failure(
                report, episode_id,
                EpisodeStatus.FAILED.value,
                f"{type(e).__name__}: {e}",
            )
            traceback_str = self._get_traceback()
            log.error(f"❌ Unexpected error:\n{traceback_str}")

        report.total_duration_sec = time.monotonic() - start
        self._write_dashboards(report)
        log.info("\n" + report.summary())
        return report

    def _make_script_call(self, episode_number: int, strategy: Any) -> Any:
        try:
            from engines.script_engine_unified import UnifiedScriptEngine
            unified = UnifiedScriptEngine(
                legacy_engine=self.script_engine,
                hook_optimizer=self.hook_optimizer,
            )
            return lambda: unified.generate(episode_number, strategy=strategy)
        except ImportError:
            pass
        return lambda: self.script_engine.generate(episode_number)

    def _validate_tafsir(
        self, script: EpisodeScript, strategy: Any,
    ) -> List[Dict[str, Any]]:
        if self.tafsir_validator is None:
            return []

        surah_name = "Unknown"
        surah_num = 0
        if script.ayah_scenes:
            first = script.ayah_scenes[0]
            ayah_obj = getattr(first, 'ayah', None)
            if ayah_obj is not None:
                surah_num = getattr(ayah_obj, 'surah', 0)
                surah_name = getattr(ayah_obj, 'surah_name', '') or (
                    f"سورة {surah_num}"
                )

        try:
            batch_results = self._try_batch_tafsir(script, surah_name, surah_num)
            if batch_results is not None:
                return batch_results
        except Exception as e:
            logger.warning(
                f"⚠️ Batch tafsir failed ({type(e).__name__}: {e}) — "
                f"falling back to per-ayah"
            )

        logger.info("🔍 Tafsir validation (per-ayah fallback, multi-key rotation)")
        return self._validate_tafsir_per_ayah(script, surah_name)

    def _try_batch_tafsir(
        self,
        script: EpisodeScript,
        surah_name: str,
        surah_num: int,
    ) -> Optional[List[Dict[str, Any]]]:
        reviewer = getattr(self.tafsir_validator, "_gemini_reviewer", None)
        if reviewer is None or reviewer._client is None:
            return None

        tafsirs: Dict[int, str] = {}
        fetcher = getattr(self.tafsir_validator, "_fetcher", None)
        if fetcher is None:
            return None

        ayah_scripts: List[Dict[str, Any]] = []
        for scene in script.ayah_scenes:
            ayah_obj = getattr(scene, 'ayah', None)
            if ayah_obj is None:
                continue
            num = getattr(ayah_obj, 'number', 0)
            try:
                tafsirs[num] = fetcher.fetch_combined(surah_num, num) or ""
            except Exception:
                tafsirs[num] = ""

            ayah_scripts.append({
                "number": num,
                "ayah_text": getattr(ayah_obj, 'text', '') or '',
                "explain": getattr(scene, 'explain_text', ''),
                "story": getattr(scene, 'story_text', '') or '',
            })

        from engines.tafsir_validator import ForbiddenAnalogyDetector
        deterministic_hits: Dict[int, List[str]] = {}
        for s in ayah_scripts:
            hits = ForbiddenAnalogyDetector.check(
                ayah_text=s["ayah_text"],
                explanation=s["explain"],
                analogy=s["story"],
            )
            if hits:
                deterministic_hits[s["number"]] = hits
                logger.warning(
                    f"🚫 ForbiddenAnalogyDetector fired on {surah_name} "
                    f"ayah {s['number']}: {len(hits)} hit(s)"
                )
                for h in hits:
                    logger.warning(f"   └─ {h}")

        from engines.batch_engines import BatchTafsirReviewer
        engine = BatchTafsirReviewer(reviewer._client)

        logger.info(
            "🔍 Tafsir validation (BATCH v22.6 — 1 Gemini call for all ayahs)"
        )
        result = engine.review_episode(ayah_scripts, tafsirs)
        if result is None:
            return None

        results: List[Dict[str, Any]] = []
        threshold = getattr(
            self.tafsir_validator, "_confidence_threshold", 0.65
        )
        for review in result.reviews:
            label = f"{surah_name} {review.ayah_number}"
            forbidden_hits = deterministic_hits.get(review.ayah_number, [])
            effectively_passed = (
                review.passed
                and review.confidence >= threshold
                and not forbidden_hits
            )
            merged_concerns = list(review.concerns) + forbidden_hits

            if effectively_passed:
                logger.info(
                    f"✅ Religious OK: {label} (confidence={review.confidence:.2f})"
                )
            else:
                logger.warning(
                    f"⚠️ Religious validation FAILED for {label} "
                    f"(confidence={review.confidence:.2f}"
                    f"{', forbidden-detector fired' if forbidden_hits else ''})"
                )
                for i, c in enumerate(merged_concerns, 1):
                    logger.warning(f"   └─ concern #{i}: {c}")

            method = "gemini-2.5-flash-batch-v22.6"
            if forbidden_hits:
                method += "+forbidden-detector"

            results.append({
                "ayah": review.ayah_number,
                "surah": surah_num,
                "passed": effectively_passed,
                "confidence": (
                    max(review.confidence, 0.95) if forbidden_hits
                    else review.confidence
                ),
                "concerns": merged_concerns,
                "method": method,
            })
        return results

    def _validate_tafsir_per_ayah(
        self, script: EpisodeScript, surah_name: str,
    ) -> List[Dict[str, Any]]:
        """Fallback: validate ayahs one by one (serially, no thread pool).

        v22.7.6: removed the ThreadPoolExecutor(max_workers=1) wrapper —
        it added overhead without parallelism. Same behaviour, simpler code.
        Gemini's 5 RPM ceiling forces serial calls anyway.
        """
        results: List[Dict[str, Any]] = []

        def _validate_one(scene: Any) -> Dict[str, Any]:
            try:
                ayah_obj = getattr(scene, 'ayah', None)
                ayah_text = getattr(ayah_obj, 'text', '') if ayah_obj else ''
                surah_num = getattr(ayah_obj, 'surah', 0) if ayah_obj else 0
                ayah_num = getattr(ayah_obj, 'number', 0) if ayah_obj else 0
                explanation = getattr(scene, 'explain_text', '')
                analogy = getattr(scene, 'story_text', '') or ''

                report = self.tafsir_validator.validate_explanation(
                    ayah_text=ayah_text,
                    surah=surah_num,
                    surah_name=surah_name,
                    ayah_number=ayah_num,
                    llm_explanation=explanation,
                    llm_analogy=analogy,
                )
                return {
                    "ayah": ayah_num,
                    "surah": surah_num,
                    "passed": report.passed,
                    "confidence": report.confidence,
                    "concerns": list(report.concerns),
                    "method": getattr(report, "reviewer", "unknown"),
                }
            except Exception as e:
                logger.error(f"❌ Tafsir validation error: {e}")
                ayah_obj = getattr(scene, 'ayah', None)
                return {
                    "ayah": (
                        getattr(ayah_obj, 'number', 0) if ayah_obj else 0
                    ),
                    "surah": (
                        getattr(ayah_obj, 'surah', 0) if ayah_obj else 0
                    ),
                    "passed": False,
                    "confidence": 0.0,
                    "concerns": [f"Validation error: {e}"],
                    "method": "error",
                }

        for scene in script.ayah_scenes:
            results.append(_validate_one(scene))

        results.sort(key=lambda r: r.get("ayah", 0))
        return results

    def _generate_ai_images(
        self,
        script: EpisodeScript,
        ep_dir: Path,
        strategy: Any,
    ) -> Dict[str, str]:
        if self.image_engine is None:
            raise PipelineError(
                "Leonardo image engine is not configured; AI images are mandatory"
            )
        if strategy.max_ai_images == 0:
            raise PipelineError(
                "Pipeline strategy disabled AI images, but Leonardo images are mandatory"
            )

        images_dir = ep_dir / "ai_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        result: Dict[str, str] = {}

        scenes_to_generate = self._select_scenes_for_images(script, strategy)
        logger.info(
            f"🎨 Image strategy: {strategy.image_reuse_strategy} → "
            f"{len(scenes_to_generate)} unique images "
            f"(budget: {strategy.max_ai_images})"
        )

        import concurrent.futures
        max_workers = 3

        def _gen(scene_key: str, scene_obj: Any) -> str:
            prompt = getattr(scene_obj, 'visual_prompt', None)
            if not prompt:
                raise PipelineError(
                    f"Leonardo image mandatory for {scene_key}, but visual_prompt is empty"
                )
            emotion_obj = getattr(scene_obj, 'scene_emotion', 'warm')
            emotion = emotion_obj.value if hasattr(emotion_obj, 'value') else str(emotion_obj)
            return self.image_engine.generate(
                prompt=prompt,
                output_path=str(images_dir / f"{scene_key}.png"),
                is_hero=False,
                episode_number=script.episode_number,
                emotion=emotion,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="img",
        ) as executor:
            futures = {
                executor.submit(_gen, key, scn): (key, scn)
                for key, scn in scenes_to_generate.items()
            }
            for fut in concurrent.futures.as_completed(futures):
                key, scene = futures[fut]
                try:
                    path = fut.result()
                    scene.image_path = path
                    result[key] = path
                    logger.info(
                        f"🎨 AI image: {key} → {Path(path).name}"
                    )
                except Exception as e:
                    raise PipelineError(
                        f"AI image failed for {key}; Leonardo images are mandatory: {e}"
                    ) from e

        self._apply_image_reuse(script, result, strategy)

        missing = [
            f"ayah_{scene.scene_id}"
            for scene in script.ayah_scenes
            if not getattr(scene, 'image_path', None)
        ]
        if missing:
            raise PipelineError(
                "Leonardo image generation incomplete; missing mandatory ayah image(s): "
                + ", ".join(missing)
            )

        logger.info(f"✅ AI images: {len(result)} generated (mandatory Leonardo mode)")
        return result

    def _select_scenes_for_images(
        self, script: EpisodeScript, strategy: Any,
    ) -> Dict[str, Any]:
        """Select ayah scenes for mandatory Leonardo generation.

        The channel quality target is one hero illustration per ayah. Intro and
        outro reuse ayah art at render time instead of consuming the 7-image
        Leonardo budget.
        """
        ayahs = list(script.ayah_scenes)
        required = len(ayahs)
        budget = strategy.max_ai_images
        if budget < required:
            raise PipelineError(
                f"Leonardo mandatory mode needs {required} ayah images, "
                f"but strategy only allows {budget}. Use HIGH mode or raise max_ai_images."
            )
        return {f"ayah_{s.scene_id}": s for s in ayahs}

    def _apply_image_reuse(
        self, script: EpisodeScript,
        generated: Dict[str, str],
        strategy: Any,
    ) -> None:
        """Keep backward compatibility without allowing CSS fallbacks."""
        # Mandatory mode generates every ayah image. There should be nothing to
        # reuse for ayahs, but keep this guard for future strategy changes.
        last_path: Optional[str] = None
        for scene in script.ayah_scenes:
            key = f"ayah_{scene.scene_id}"
            path = generated.get(key) or getattr(scene, 'image_path', None)
            if path:
                scene.image_path = path
                last_path = path
            elif last_path:
                scene.image_path = last_path

    def _generate_audio(
        self,
        script: EpisodeScript,
        ep_dir: Path,
        strategy: Any,
    ) -> Dict[str, str]:
        audio_map = self.voice_engine.generate_episode_audio(script, ep_dir)

        try:
            from engines.voice_emotion_mapper import get_voice_settings
            _adaptive_available = True
        except ImportError:
            _adaptive_available = False

        tts_directions: Dict[str, Dict[str, Any]] = {}
        try:
            ep_json_path = (
                self.script_engine._paths.temp_episodes
                / f"episode_{script.episode_number:03d}.json"
            )
            if ep_json_path.exists():
                with open(ep_json_path, encoding="utf-8") as f:
                    raw = json.load(f)
                tts_directions = raw.get("_tts_directions", {}) or {}
        except Exception as e:
            logger.debug(f"TTS directions not loaded: {e}")
            tts_directions = {}

        extra_items: List = []
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            emotion = (
                scene.scene_emotion.value
                if hasattr(scene.scene_emotion, 'value')
                else str(scene.scene_emotion)
            )

            segments_with_types = [
                ("hook", scene.hook_text, "excited"),
                ("story", scene.story_text, "warm"),
                ("moral", scene.moral_text, "reverent"),
            ]
            for seg_type, text, seg_emotion in segments_with_types:
                if not text:
                    continue

                tts_dir = tts_directions.get(f"{sid}.{seg_type}", {})
                directed_text = tts_dir.get("directed_text", "").strip()
                final_text = directed_text if directed_text else text

                output_path = str(ep_dir / f"{sid}_{seg_type}.mp3")
                compound_emotion = f"{seg_type}:{seg_emotion}"
                extra_items.append((final_text, output_path, compound_emotion))

        if extra_items:
            logger.info(
                f"🎙️ Synthesizing {len(extra_items)} cinematic segments "
                f"with per-segment emotions "
                f"(adaptive_voice={strategy.use_adaptive_voice})"
            )
            if (
                strategy.use_adaptive_voice
                and hasattr(self.voice_engine, 'synthesize_batch_with_emotions')
            ):
                self.voice_engine.synthesize_batch_with_emotions(extra_items)
            else:
                legacy_items = [(t, p) for t, p, _ in extra_items]
                self.voice_engine.synthesize_batch(legacy_items)

            for scene in script.ayah_scenes:
                sid = f"ayah_{scene.scene_id}"
                for kind in ("hook", "story", "moral"):
                    p = ep_dir / f"{sid}_{kind}.mp3"
                    if p.exists():
                        audio_map[f"{sid}_{kind}"] = str(p)
                        setattr(scene, f"{kind}_audio", str(p))

        return audio_map

    def _color_grade_for(self, emotion: str) -> Optional[str]:
        if not self.enable_color_grade:
            return None
        return self.color_grades_by_emotion.get(
            emotion,
            self.color_grades_by_emotion.get(
                "warm",
                getattr(self.video_cfg, "color_grade_default", None),
            ),
        )

    def _ayah_background_path(self, ep_dir: Path, scene: Any) -> str:
        """Resolve the mandatory Leonardo image for one ayah scene."""
        existing = getattr(scene, 'image_path', None)
        if existing and Path(existing).is_file():
            return str(existing)
        candidate = ep_dir / "ai_images" / f"ayah_{scene.scene_id}.png"
        scene.image_path = str(candidate)
        return str(candidate)

    def _episode_edge_background(self, ep_dir: Path, script: EpisodeScript, *, last: bool = False) -> Optional[str]:
        """Use first/last ayah art for intro/outro without spending extra tokens."""
        scenes = list(script.ayah_scenes)
        if not scenes:
            return None
        scene = scenes[-1] if last else scenes[0]
        return self._ayah_background_path(ep_dir, scene)

    def _render_all_scenes(
        self,
        script: EpisodeScript,
        audio_map: Dict[str, str],
        ep_dir: Path,
    ) -> List[str]:
        scenes_dir = ep_dir / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        if "intro" in audio_map:
            out = str(scenes_dir / "00_intro.mp4")
            intro_bg = getattr(script.intro_scene, 'image_path', None) or self._episode_edge_background(ep_dir, script)
            self.visual_renderer.render(SceneRenderRequest(
                scene_type=script.intro_scene.visual_scene.value,
                palette=script.intro_scene.palette.value,
                text=script.intro_scene.narrator_text,
                is_ayah=False,
                keywords=script.intro_scene.keywords,
                output_path=out,
                extra={
                    "text_style": "narrator",
                    "scene_emotion": "excited",
                    "background_image": intro_bg,
                    "require_background_image": True,
                    "background_motion": "diagonal",
                    "color_grade": self._color_grade_for("excited"),
                },
            ), audio_map["intro"])
            outputs.append(out)

        for i, scene in enumerate(script.ayah_scenes):
            sid = f"ayah_{scene.scene_id}"
            pfx = f"{i + 1:02d}"
            scene_type = scene.visual_scene.value
            palette = scene.palette.value
            emotion = (
                scene.scene_emotion.value
                if hasattr(scene.scene_emotion, 'value')
                else str(scene.scene_emotion)
            )
            kw = scene.keywords
            ayah_bg = self._ayah_background_path(ep_dir, scene)

            if f"{sid}_hook" in audio_map and scene.hook_text:
                out = str(scenes_dir / f"{pfx}a_{sid}_hook.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type, palette=palette,
                    text=scene.hook_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "hook",
                        "scene_emotion": "playful",
                        "background_image": ayah_bg,
                        "require_background_image": True,
                        "background_motion": "hook",
                        "color_grade": self._color_grade_for("playful"),
                    },
                ), audio_map[f"{sid}_hook"])
                outputs.append(out)

            # Note: `{sid}_intro` audio is never produced by _generate_audio
            # (it only emits hook/story/moral per ayah). The block below
            # remains a no-op safety net for future expansion.
            if f"{sid}_intro" in audio_map and getattr(scene, "intro_text", None):
                out = str(scenes_dir / f"{pfx}b_{sid}_intro.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type, palette=palette,
                    text=scene.intro_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "narrator",
                        "scene_emotion": emotion,
                        "background_image": ayah_bg,
                        "require_background_image": True,
                        "background_motion": "explain",
                        "color_grade": self._color_grade_for(emotion),
                    },
                ), audio_map[f"{sid}_intro"])
                outputs.append(out)

            if f"{sid}_story" in audio_map and scene.story_text:
                out = str(scenes_dir / f"{pfx}c_{sid}_analogy.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type, palette=palette,
                    text=scene.story_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "story",
                        "scene_emotion": "warm",
                        "background_image": ayah_bg,
                        "require_background_image": True,
                        "background_motion": "story_left" if i % 2 == 0 else "story_right",
                        "color_grade": self._color_grade_for("warm"),
                    },
                ), audio_map[f"{sid}_story"])
                outputs.append(out)

            if f"{sid}_ayah" in audio_map:
                out = str(scenes_dir / f"{pfx}d_{sid}_ayah.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=(
                        "mosque"
                        if scene_type in ("mosque", "sky", "starry_night")
                        else scene_type
                    ),
                    palette="golden_hour",
                    text=scene.ayah.text, is_ayah=True, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "ayah",
                        "scene_emotion": "reverent",
                        "color_grade": self._color_grade_for("reverent"),
                    },
                ), audio_map[f"{sid}_ayah"])
                outputs.append(out)

            if f"{sid}_explain" in audio_map and scene.explain_text:
                out = str(scenes_dir / f"{pfx}e_{sid}_explain.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type, palette=palette,
                    text=scene.explain_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "narrator",
                        "scene_emotion": emotion,
                        "background_image": ayah_bg,
                        "require_background_image": True,
                        "background_motion": "explain",
                        "color_grade": self._color_grade_for(emotion),
                    },
                ), audio_map[f"{sid}_explain"])
                outputs.append(out)

            if f"{sid}_moral" in audio_map and scene.moral_text:
                out = str(scenes_dir / f"{pfx}f_{sid}_moral.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type, palette="golden_hour",
                    text=scene.moral_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "moral",
                        "scene_emotion": "peaceful",
                        "background_image": ayah_bg,
                        "require_background_image": True,
                        "background_motion": "moral",
                        "color_grade": self._color_grade_for("peaceful"),
                    },
                ), audio_map[f"{sid}_moral"])
                outputs.append(out)

        for sc in script.mid_scenes:
            key = f"mid_{sc.scene_id}"
            if key in audio_map:
                out = str(scenes_dir / f"mid_{sc.scene_id}.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=sc.visual_scene.value,
                    palette=sc.palette.value,
                    text=sc.narrator_text, is_ayah=False, keywords=sc.keywords,
                    output_path=out,
                ), audio_map[key])
                outputs.append(out)

        if "outro" in audio_map:
            out = str(scenes_dir / "99_outro.mp4")
            outro_bg = getattr(script.outro_scene, 'image_path', None) or self._episode_edge_background(ep_dir, script, last=True)
            self.visual_renderer.render(SceneRenderRequest(
                scene_type="starry_night",
                palette="night_stars",
                text=script.outro_scene.narrator_text,
                is_ayah=False,
                keywords=script.outro_scene.keywords,
                output_path=out,
                extra={
                    "text_style": "narrator",
                    "scene_emotion": "peaceful",
                    "background_image": outro_bg,
                    "require_background_image": True,
                    "background_motion": "diagonal",
                    "color_grade": self._color_grade_for("peaceful"),
                },
            ), audio_map["outro"])
            outputs.append(out)

        if not outputs:
            raise PipelineError("No scenes rendered", stage="render_scenes")

        logger.info(f"✅ Rendered {len(outputs)} cinematic segments")
        return outputs

    def _apply_bgm_smart(
        self,
        raw_video: str,
        bgm_video: str,
        script: Any,
        scene_segments: List[str],
    ) -> str:
        try:
            from infrastructure.bgm_director import BGMDirector
            from infrastructure.audio_utils import get_audio_duration
        except ImportError:
            logger.info("ℹ️ BGMDirector unavailable — using fixed BGM volume")
            return self.bgm_mixer.apply_bgm(raw_video, bgm_video)

        scenes_meta: List[dict] = []
        for path_str in scene_segments:
            name = Path(path_str).stem
            duration = get_audio_duration(path_str) or 5.0

            emotion = "warm"
            segment = "narrator"
            is_ayah = False

            if name.endswith("_hook"):
                segment = "hook"
                emotion = "playful"
            elif name.endswith("_analogy"):
                segment = "analogy"
                emotion = "warm"
            elif name.endswith("_ayah"):
                segment = "ayah"
                emotion = "reverent"
                is_ayah = True
            elif name.endswith("_explain"):
                segment = "explain"
                emotion = "warm"
            elif name.endswith("_moral"):
                segment = "moral"
                emotion = "peaceful"
            elif "intro" in name:
                segment = "intro"
                emotion = "excited"
            elif "outro" in name:
                segment = "outro"
                emotion = "peaceful"

            scenes_meta.append({
                "duration_sec": duration,
                "emotion": emotion,
                "segment": segment,
                "is_ayah": is_ayah,
            })

        try:
            curve = BGMDirector.plan_episode_curve(scenes_meta)
            if not curve:
                return self.bgm_mixer.apply_bgm(raw_video, bgm_video)

            logger.info(BGMDirector.summarize_curve(curve))

            if hasattr(self.bgm_mixer, "apply_bgm_with_curve"):
                return self.bgm_mixer.apply_bgm_with_curve(
                    raw_video, bgm_video, curve,
                )
        except Exception as e:
            logger.warning(
                f"⚠️ BGM curve failed ({e}) — falling back to fixed"
            )

        return self.bgm_mixer.apply_bgm(raw_video, bgm_video)

    def _concat_scenes(
        self, segments: List[str], output_path: str,
        *,
        script: Any = None,
    ) -> str:
        if not (self.enable_crossfades and len(segments) <= 20):
            return self.assembler.concat(segments, output_path, re_encode=False)

        if script is not None:
            try:
                from infrastructure.mood_transitions import (
                    concat_with_mood_transitions,
                )
                emotions = self._build_segment_emotions(script, segments)
                if emotions and any(e for e in emotions):
                    return concat_with_mood_transitions(
                        bgm_mixer=self.bgm_mixer,
                        segments=segments,
                        emotions=emotions,
                        output_path=output_path,
                        assembler=self.assembler,
                    )
            except ImportError:
                pass

        return self.bgm_mixer.concat_with_crossfades(
            segments, output_path,
            transition_duration=0.4,
            transition_type="fade",
            assembler=self.assembler,
        )

    def _build_segment_emotions(
        self, script: Any, segments: List[str],
    ) -> List[Optional[str]]:
        emotion_by_id: Dict[str, str] = {}
        for s in script.ayah_scenes:
            emo = (
                s.scene_emotion.value
                if hasattr(s.scene_emotion, 'value')
                else str(s.scene_emotion)
            )
            emotion_by_id[f"ayah_{s.scene_id}"] = emo

        emotions: List[Optional[str]] = []
        for seg in segments:
            name = Path(seg).stem
            if "intro" in name and name.startswith("00"):
                emotions.append("excited")
            elif "outro" in name or name.startswith("99"):
                emotions.append("peaceful")
            elif "_hook" in name:
                emotions.append("playful")
            elif "_ayah" in name and "ayah_" in name:
                emotions.append("reverent")
            elif "_analogy" in name or "_story" in name:
                emotions.append("warm")
            elif "_moral" in name:
                emotions.append("peaceful")
            elif "_explain" in name or "_intro" in name:
                for sid, emo in emotion_by_id.items():
                    if sid in name:
                        emotions.append(emo)
                        break
                else:
                    emotions.append("warm")
            else:
                emotions.append(None)
        return emotions

    def _run_stage(
        self, name: str, fn: Any, report: EpisodeRunReport, *,
        idem_key: Any = None,
    ) -> Any:
        registry = get_registry()

        if idem_key is not None and self._checkpoints.is_completed(
            idem_key, name,
        ):
            cached = self._checkpoints.get_output(idem_key, name)
            logger.info(f"⏭️  stage '{name}' skipped (replay)")
            report.stages.append(StageResult(
                name=name, success=True, duration_sec=0.0, detail="skipped",
            ))
            registry.counter("pipeline.stage.skipped").inc(
                labels={"stage": name},
            )
            return cached

        try:
            from core.stage_retry import run_with_retry, get_policy
            policy = get_policy(name)
            wrapped_fn = lambda: run_with_retry(
                fn, stage_name=name, policy=policy,
            )
        except ImportError:
            wrapped_fn = fn

        with self._emitter.span(f"stage.{name}", stage=name) as span:
            t = time.monotonic()
            try:
                result = wrapped_fn()
                dur = time.monotonic() - t
                report.stages.append(StageResult(
                    name=name, success=True, duration_sec=dur,
                ))
                registry.histogram(
                    "pipeline.stage.duration_ms"
                ).record(dur * 1000, labels={
                    "stage": name, "outcome": "success",
                })
                registry.counter(
                    "pipeline.stage.success"
                ).inc(labels={"stage": name})
                if (
                    idem_key is not None
                    and isinstance(result, dict)
                ):
                    self._checkpoints.record(
                        idem_key, stage=name,
                        duration_ms=int(dur * 1000),
                        output=result,
                    )
                span.set("duration_ms", int(dur * 1000))
                return result
            except Exception as e:
                dur = time.monotonic() - t
                report.stages.append(StageResult(
                    name=name, success=False, duration_sec=dur,
                    detail=type(e).__name__,
                ))
                registry.histogram(
                    "pipeline.stage.duration_ms"
                ).record(dur * 1000, labels={
                    "stage": name, "outcome": "failure",
                })
                registry.counter("pipeline.stage.failure").inc(
                    labels={
                        "stage": name, "error_type": type(e).__name__,
                    },
                )
                raise

    def _check_shutdown(self) -> None:
        if self._shutdown_requested:
            raise RuntimeError("Shutdown requested mid-pipeline")

    @staticmethod
    def _get_traceback() -> str:
        import traceback
        return traceback.format_exc()

    def _mark_failure(
        self, report: EpisodeRunReport, episode_id: Optional[str],
        status: str, error_str: str,
    ) -> None:
        report.final_status = status
        report.error = error_str
        if episode_id:
            try:
                self.repository.update_status(episode_id, status)
            except Exception as e:
                logger.warning(f"⚠️ DB status update failed: {e}")

    def _safe_cleanup(
        self, ep_dir: Path, branded: Path, raw_video: Path,
        scene_segments: List[str], *, episode_number: Optional[int] = None,
    ) -> None:
        """Remove temp scratch files after upload confirmed.

        v22.7.6 changes:
          * filename: episode_NNN.mp4 (not episode_branded_episode.mp4 —
            that name was constant across all episodes, overwriting the
            previous backup every run)
          * copy not move: leave branded_episode.mp4 inside
            temp/episodes/episode_NNN/ so the GitHub Actions artifact
            upload step (which runs AFTER this cleanup) can find it.
            The local archive at videos/episode_NNN.mp4 is a separate
            copy meant for the operator's local filesystem.
          * episode_number now a kwarg so the filename is correct even
            when branded.parent happens not to be ep_dir.
        """
        try:
            raw_video.unlink(missing_ok=True)
            for s in scene_segments:
                Path(s).unlink(missing_ok=True)
            for sub in ("scenes", "mastered", "subs"):
                d = ep_dir / sub
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)

            # Decide on a stable, per-episode archive filename
            if episode_number is not None:
                archive_name = f"episode_{episode_number:03d}.mp4"
            else:
                # Fallback: derive from parent dir name (e.g. episode_002)
                archive_name = f"{branded.parent.name}.mp4"

            final = self.paths.videos / archive_name
            try:
                self.paths.videos.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(branded), str(final))
                logger.info(f"📦 Final video archived: {final}")
            except OSError as e:
                logger.warning(f"⚠️ Final archive failed: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Cleanup partial failure: {e}")

    def _write_dashboards(self, report: EpisodeRunReport) -> None:
        if self.cost_dashboard is None:
            return
        try:
            self.cost_dashboard.write_episode_breakdown(
                episode_number=report.episode_number,
                stages=report.stages,
            )
        except Exception as e:
            logger.warning(f"⚠️ Episode dashboard failed: {e}")
        try:
            self.cost_dashboard.write_monthly_summary()
        except Exception as e:
            logger.warning(f"⚠️ Monthly dashboard failed: {e}")
