"""
orchestrator.py — VALUE / QEEMA v21.0 (UNIFIED PRODUCTION)
==========================================================
[v21 — Strategy-driven Orchestration]
Every decision point queries PipelineStrategy. No more dead code paths.

[Pipeline Stages — driven by strategy]
  1. script             → Multi-task OR legacy 6-call (strategy decides)
  2. tafsir_validation  → Batched (1 Claude call) OR per-ayah loop
  3. ai_images          → Up to N images (strategy.max_ai_images)
  4. audio              → Combined per-scene (1 call) OR per-segment
  5. audio_master       → VoiceEngine.master_episode
  6. render_scenes      → 6-segment cinematic structure
  7. concat_raw         → BGMMixer with crossfades
  8. bgm_mix            → Background music
  9. subtitles          → ASS subtitles burned in (if enabled)
  10. wrap_branded      → Intro + outro + CTA
  11. thumbnail         → 3 variants for A/B testing
  12. review_gate       → Block first N episodes for manual review
  13. upload            → YouTube
  14. dashboard         → Generate per-episode + monthly markdown
  15. cleanup           → Remove temp files after upload confirmed

[Key fixes from v20.1 critique]
  - Multi-task script ACTUALLY USED (when available)
  - Batched tafsir ACTUALLY USED (saves 80% Anthropic)
  - Combined TTS ACTUALLY USED (saves ~40% chars)
  - Strategy decides AI image count based on quota
  - Cost dashboard generated each episode
  - Hook optimizer called BEFORE script (Thompson Sampling)
  - StageResult.success used consistently (no .passed bug)
  - TafsirValidator.validate_explanation() called with correct kwargs
"""
from __future__ import annotations

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

PIPELINE_VERSION: str = "21.0.0"


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
    strategy_summary: Optional[str] = None  # v21

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
# Orchestrator v21
# ════════════════════════════════════════════════════════════════
class Orchestrator:
    """Production orchestrator with strategy-driven decision making."""

    def __init__(
        self,
        *,
        # Core engines (required)
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
        # Optional engines
        bgm_mixer: Optional[BGMMixer] = None,
        subtitle_engine: Optional[SubtitleEngine] = None,
        image_engine: Any = None,
        tafsir_validator: Any = None,
        hook_optimizer: Any = None,
        review_gate: Any = None,
        cost_tracker: Any = None,
        # v19+
        quota_manager: Any = None,
        # v20
        cost_dashboard: Any = None,
        # v21 — strategy-driven
        strategy_factory: Any = None,
        requested_mode: Any = None,           # QualityMode enum
        has_multi_task_engine: bool = False,
        # Per-emotion features
        color_grades_by_emotion: Optional[Dict[str, str]] = None,
        # Flags
        approval_explicit: bool = False,
        dry_run: bool = False,
        enable_subtitles: bool = True,
        enable_color_grade: bool = True,
        enable_crossfades: bool = True,
    ) -> None:
        # Core engines
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

        # Optional engines
        self.bgm_mixer = bgm_mixer or BGMMixer(paths=paths)
        self.subtitle_engine = subtitle_engine
        self.image_engine = image_engine
        self.tafsir_validator = tafsir_validator
        self.hook_optimizer = hook_optimizer
        self.review_gate = review_gate
        self.cost_tracker = cost_tracker

        # v19+
        self.quota_manager = quota_manager

        # v20
        self.cost_dashboard = cost_dashboard

        # v21 strategy
        self.strategy_factory = strategy_factory
        self.requested_mode = requested_mode
        self.has_multi_task_engine = has_multi_task_engine

        # Per-emotion features
        self.color_grades_by_emotion = color_grades_by_emotion or {}
        self.approval_explicit = approval_explicit

        # Feature flags
        self.enable_subtitles = enable_subtitles
        self.enable_color_grade = enable_color_grade
        self.enable_crossfades = enable_crossfades
        env_bgm = os.getenv("ENABLE_BGM")
        self.enable_bgm = (
            env_bgm.lower() == "true" if env_bgm is not None else True
        )

        # Initialize subtitle engine if needed but not provided
        if self.enable_subtitles and self.subtitle_engine is None:
            try:
                self.subtitle_engine = SubtitleEngine(paths=paths)
            except Exception as e:
                logger.warning(f"⚠️ Subtitle engine init failed: {e}")
                self.enable_subtitles = False

        # State
        self._shutdown_requested: bool = False
        self._current_strategy: Optional[Any] = None  # PipelineStrategy

        checkpoints_root = paths.root / "state" / "checkpoints"
        self._checkpoints = CheckpointStore(checkpoints_root)
        self._emitter: SpanEmitter = get_emitter()

    # ─── Lifecycle ───────────────────────────────────────────────
    def warmup(self) -> None:
        logger.info("🔥 Warming up v21 orchestrator")
        self.visual_renderer.warmup()
        self.intro_outro.build_intro()
        self.intro_outro.build_outro()
        logger.info("✅ Orchestrator v21 warm")

    def shutdown(self) -> None:
        logger.info("🧹 Shutting down orchestrator")
        try:
            self.visual_renderer.shutdown()
        except Exception as e:
            logger.warning(f"⚠️ visual_renderer shutdown error: {e}")

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        logger.warning("⚠️ Shutdown requested")

    # ─── Strategy computation ────────────────────────────────────
    def _compute_strategy(self, episode_number: int) -> Any:
        """v21: Compute the pipeline strategy for this episode."""
        if self.strategy_factory is None:
            # Fallback: pretend HIGH mode
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
            has_anthropic_key=self.tafsir_validator is not None,
            has_leonardo_engine=self.image_engine is not None,
            has_multi_task_engine=self.has_multi_task_engine,
        )
        logger.info(strategy.detailed_report())
        return strategy

    # ─── Public entry points ─────────────────────────────────────
    def run_next(self) -> Optional[EpisodeRunReport]:
        record = self.repository.get_pending()
        if not record:
            logger.info("📭 No pending episodes")
            return None
        return self.run(record["episode_number"])

    def run(self, episode_number: int) -> EpisodeRunReport:
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

        # Compute strategy ONCE for this episode
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

        with self._emitter.span(
            "episode.run",
            episode_number=episode_number,
            pipeline_version=PIPELINE_VERSION,
            dry_run=self.dry_run,
            mode=self._current_strategy.mode.value,
        ):
            return self._run_pipeline(
                episode_number=episode_number,
                report=report,
                start=start,
                log=log,
                idem_key=idem_key,
            )

    # ─── Main pipeline ───────────────────────────────────────────
    def _run_pipeline(
        self, *,
        episode_number: int,
        report: EpisodeRunReport,
        start: float,
        log: logging.Logger,
        idem_key: Any,
    ) -> EpisodeRunReport:
        strategy = self._current_strategy
        episode_id: Optional[str] = None

        # Phase 0: repo registration
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
            # ── Stage 1: Script ──────────────────────────────────
            # v22: Use UnifiedScriptEngine path if available (multi-task)
            script_call = self._make_script_call(episode_number, strategy)
            script: EpisodeScript = self._run_stage(
                "script",
                script_call,
                report,
            )
            script.episode_id = episode_id
            self._check_shutdown()

            # ── Stage 2: Tafsir validation (CRITICAL) ────────────
            if self.tafsir_validator is not None and strategy.use_claude_tafsir:
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
                logger.info(
                    "ℹ️ Tafsir Claude validation skipped "
                    "(strategy=ECONOMY or no API key)"
                )

            # ── Stage 3: AI image generation ─────────────────────
            if (
                self.image_engine is not None
                and strategy.max_ai_images > 0
            ):
                self._run_stage(
                    "ai_images",
                    lambda: self._generate_ai_images(
                        script, ep_dir, strategy,
                    ),
                    report,
                )

            # ── Stage 4: Audio ───────────────────────────────────
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

            # ── Stage 5: Render scenes ───────────────────────────
            scene_segments = self._run_stage(
                "render_scenes",
                lambda: self._render_all_scenes(script, mastered, ep_dir),
                report,
            )
            self._check_shutdown()

            # ── Stage 6: Concat with crossfades (mood-aware in v23) ──
            raw_video = ep_dir / "raw_episode.mp4"
            self._run_stage(
                "concat_raw",
                lambda: self._concat_scenes(scene_segments, str(raw_video), script=script),
                report,
            )

            # ── Stage 7: BGM mixing (v22.2: mood-aware curve if possible) ──
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

            # ── Stage 8: Subtitles ───────────────────────────────
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

                    # v22.2: Validate Arabic typography of generated ASS
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

            # ── Stage 9: Wrap branded ────────────────────────────
            cta_audio = mastered.get("cta")
            branded = ep_dir / "branded_episode.mp4"
            self._run_stage(
                "wrap_branded",
                lambda: self.intro_outro.wrap_episode(
                    post_subs, str(branded), cta_audio_path=cta_audio,
                ),
                report,
            )

            # ── Stage 10: Thumbnail (3 variants for A/B testing) ─
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

            # ── Stage 11: Review gate ────────────────────────────
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

            # ── Stage 12: Upload ─────────────────────────────────
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

                # Upload thumbnail variants for YouTube Test & Compare
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

            # ── Stage 13: Mark complete ──────────────────────────
            self.repository.update_status(
                episode_id, EpisodeStatus.COMPLETED.value,
                youtube_url=report.video_url,
            )

            # ── Stage 14: Mark episode in quota manager ──────────
            if self.quota_manager is not None:
                try:
                    self.quota_manager.episode_started()
                except Exception as e:
                    log.warning(f"⚠️ Quota update failed: {e}")

            # ── Stage 15: Cleanup ────────────────────────────────
            self._safe_cleanup(
                ep_dir, branded, Path(bgm_result), scene_segments,
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

        # Always write dashboards (even on failure for debugging)
        report.total_duration_sec = time.monotonic() - start
        self._write_dashboards(report)
        log.info("\n" + report.summary())
        return report

    # ─── v22: Strategy-aware script call ─────────────────────────
    def _make_script_call(self, episode_number: int, strategy: Any) -> Any:
        """Build the appropriate script.generate() callable.

        v22.2: Passes HookOptimizer to UnifiedScriptEngine for Thompson Sampling.
        """
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

    # ─── v21: Tafsir validation (uses BATCHED API) ──────────────
    def _validate_tafsir(
        self, script: EpisodeScript, strategy: Any,
    ) -> List[Dict[str, Any]]:
        """v21: Use batched validation if available, fall back to per-ayah.

        The validate_episode_batched_v20() method makes ONE Claude call
        for all ayahs in the episode (vs N calls). 80% Anthropic savings.
        """
        if self.tafsir_validator is None:
            return []

        # Build episode_data dict in the shape TafsirValidator expects
        episode_data = self._build_episode_data_for_tafsir(script)

        # Determine the surah (use the first ayah's surah)
        surah_num = 0
        surah_name = "Unknown"
        if script.ayah_scenes:
            first = script.ayah_scenes[0]
            ayah_obj = getattr(first, 'ayah', None)
            if ayah_obj is not None:
                surah_num = getattr(ayah_obj, 'surah', 0)
                surah_name = getattr(ayah_obj, 'surah_name', '') or (
                    f"سورة {surah_num}"
                )

        # Try batched (v20) first if strategy says so
        if (
            strategy.use_batched_tafsir
            and hasattr(self.tafsir_validator, 'validate_episode_batched_v20')
        ):
            try:
                logger.info("🚀 Using BATCHED tafsir validation (1 Claude call)")
                all_passed, all_concerns = (
                    self.tafsir_validator.validate_episode_batched_v20(
                        episode_data=episode_data,
                        surah=surah_num,
                        surah_name=surah_name,
                    )
                )
                # Convert to per-ayah format for report
                return self._batched_to_per_ayah(
                    all_passed, all_concerns, script,
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ Batched tafsir failed ({e}) — "
                    f"falling back to per-ayah"
                )

        # Fall back to per-ayah validation
        return self._validate_tafsir_per_ayah(script, surah_name)

    def _build_episode_data_for_tafsir(
        self, script: EpisodeScript,
    ) -> Dict[str, Any]:
        """Build the episode_data dict TafsirValidator.validate_episode_batched_v20 expects."""
        ayah_scenes = []
        for scene in script.ayah_scenes:
            ayah_obj = getattr(scene, 'ayah', None)
            if ayah_obj is None:
                continue
            ayah_scenes.append({
                "ayah": {
                    "number": getattr(ayah_obj, 'number', 0),
                    "text": getattr(ayah_obj, 'text', ''),
                    "surah": getattr(ayah_obj, 'surah', 0),
                },
                "explain_text": getattr(scene, 'explain_text', ''),
                "story_text": getattr(scene, 'story_text', ''),
                "moral_text": getattr(scene, 'moral_text', ''),
            })
        return {"ayah_scenes": ayah_scenes}

    @staticmethod
    def _batched_to_per_ayah(
        all_passed: bool, all_concerns: List[str], script: EpisodeScript,
    ) -> List[Dict[str, Any]]:
        """Convert batched (passed, concerns) to per-ayah dicts for report."""
        # Group concerns by ayah number (concerns are like "[name 3] foo")
        import re
        concerns_by_ayah: Dict[int, List[str]] = {}
        for c in all_concerns:
            m = re.match(r'\[.*?(\d+)\]\s*(.*)', c)
            if m:
                num = int(m.group(1))
                concerns_by_ayah.setdefault(num, []).append(m.group(2))
            else:
                concerns_by_ayah.setdefault(0, []).append(c)

        results = []
        for scene in script.ayah_scenes:
            ayah_obj = getattr(scene, 'ayah', None)
            num = getattr(ayah_obj, 'number', 0) if ayah_obj else 0
            ayah_concerns = concerns_by_ayah.get(num, [])
            results.append({
                "ayah": num,
                "surah": getattr(ayah_obj, 'surah', 0) if ayah_obj else 0,
                "passed": len(ayah_concerns) == 0,
                "confidence": 0.9 if not ayah_concerns else 0.5,
                "concerns": ayah_concerns,
                "method": "claude-batched-v20",
            })
        return results

    def _validate_tafsir_per_ayah(
        self, script: EpisodeScript, surah_name: str,
    ) -> List[Dict[str, Any]]:
        """Fallback: validate ayahs one by one using validate_explanation."""
        results: List[Dict[str, Any]] = []
        import concurrent.futures

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

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="tafsir",
        ) as executor:
            futures = [
                executor.submit(_validate_one, s)
                for s in script.ayah_scenes
            ]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

        results.sort(key=lambda r: r.get("ayah", 0))
        return results

    # ─── AI image generation (strategy-aware) ────────────────────
    def _generate_ai_images(
        self,
        script: EpisodeScript,
        ep_dir: Path,
        strategy: Any,
    ) -> Dict[str, str]:
        """v21: Generate AI images respecting strategy.max_ai_images budget.

        Image strategy:
          - "unique":  every scene gets a unique image (HIGH mode)
          - "reuse":   intro/outro share, similar emotion ayahs share (BAL)
          - "minimal": 3 hero images only, others use CSS (ECON)
          - "css_only": no AI images at all (engine missing/disabled)
        """
        if self.image_engine is None or strategy.max_ai_images == 0:
            return {}

        images_dir = ep_dir / "ai_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        result: Dict[str, str] = {}

        # Determine which scenes get unique images based on strategy
        scenes_to_generate = self._select_scenes_for_images(script, strategy)
        logger.info(
            f"🎨 Image strategy: {strategy.image_reuse_strategy} → "
            f"{len(scenes_to_generate)} unique images "
            f"(budget: {strategy.max_ai_images})"
        )

        # Submit jobs in parallel
        import concurrent.futures
        max_workers = 3

        def _gen(scene_key: str, scene_obj: Any) -> Optional[str]:
            prompt = getattr(scene_obj, 'visual_prompt', None)
            if not prompt:
                return None
            try:
                return self.image_engine.generate(
                    prompt=prompt,
                    output_path=str(images_dir / f"{scene_key}.png"),
                    is_hero=scene_key in ("intro", "outro"),
                    episode_number=script.episode_number,
                )
            except Exception as e:
                logger.warning(f"⚠️ AI image gen failed for {scene_key}: {e}")
                return None

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
                    if path:
                        scene.image_path = path
                        result[key] = path
                        logger.info(
                            f"🎨 AI image: {key} → {Path(path).name}"
                        )
                    else:
                        logger.info(
                            f"⚠️ AI image: {key} → fallback to CSS"
                        )
                except Exception as e:
                    logger.warning(
                        f"⚠️ AI image failed for {key}: {e}"
                    )

        # Apply reuse strategy: copy hero image to scenes without unique image
        if strategy.image_reuse_strategy in ("reuse", "minimal"):
            self._apply_image_reuse(script, result, strategy)

        logger.info(f"✅ AI images: {len(result)} generated")
        return result

    def _select_scenes_for_images(
        self, script: EpisodeScript, strategy: Any,
    ) -> Dict[str, Any]:
        """Pick which scenes get unique AI images based on strategy.

        For HIGH (7 images): intro + 5 ayahs + outro = 7
        For BALANCED (5):    intro + 3 ayahs + outro = 5
        For ECONOMY (3):     intro + 1 hero ayah + outro = 3
        """
        budget = strategy.max_ai_images
        selected: Dict[str, Any] = {}

        # Always include intro + outro if budget allows
        if budget >= 1:
            selected["intro"] = script.intro_scene
        if budget >= 2:
            selected["outro"] = script.outro_scene

        remaining = budget - len(selected)
        ayahs = list(script.ayah_scenes)

        if remaining >= len(ayahs):
            # All ayahs get unique
            for s in ayahs:
                selected[f"ayah_{s.scene_id}"] = s
        else:
            # Pick evenly-distributed ayahs
            if remaining > 0 and ayahs:
                stride = max(1, len(ayahs) // remaining)
                picked = ayahs[::stride][:remaining]
                for s in picked:
                    selected[f"ayah_{s.scene_id}"] = s

        return selected

    def _apply_image_reuse(
        self, script: EpisodeScript,
        generated: Dict[str, str],
        strategy: Any,
    ) -> None:
        """Apply intro→outro and similar-emotion ayah reuse."""
        # Ayahs without their own image get the intro image as background
        intro_path = generated.get("intro")
        if intro_path:
            for scene in script.ayah_scenes:
                if not getattr(scene, 'image_path', None):
                    scene.image_path = intro_path

    # ─── Audio generation (strategy-aware) ───────────────────────
    def _generate_audio(
        self,
        script: EpisodeScript,
        ep_dir: Path,
        strategy: Any,
    ) -> Dict[str, str]:
        """v22.1: Generate audio with per-segment emotion-mapped voice settings.

        For each segment (hook/story/explain/moral), look up the right
        VoiceSettings via voice_emotion_mapper, and apply them per call.

        Falls back to static config defaults if:
          - strategy.use_adaptive_voice is False (ECONOMY mode)
          - voice_emotion_mapper module unavailable
          - VoiceEngine doesn't expose per-call settings injection
        """
        # Use the standard voice engine entry (handles intro/explain/outro)
        audio_map = self.voice_engine.generate_episode_audio(script, ep_dir)

        # Synthesize per-scene segments with adaptive settings
        try:
            from engines.voice_emotion_mapper import get_voice_settings
            _adaptive_available = True
        except ImportError:
            _adaptive_available = False

        extra_items: List = []
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            emotion = (
                scene.scene_emotion.value
                if hasattr(scene.scene_emotion, 'value')
                else str(scene.scene_emotion)
            )

            # v23: Per-segment emotion mapping
            # Hook is more energetic (excited/playful), moral is contemplative (peaceful)
            segments_with_emotions = [
                ("hook", scene.hook_text, "playful" if emotion == "warm" else "excited"),
                ("story", scene.story_text, emotion),  # follows scene emotion
                ("moral", scene.moral_text, "peaceful"),  # always contemplative
            ]
            for seg_type, text, seg_emotion in segments_with_emotions:
                if not text:
                    continue
                output_path = str(ep_dir / f"{sid}_{seg_type}.mp3")
                extra_items.append((text, output_path, seg_emotion))

        if extra_items:
            logger.info(
                f"🎙️ Synthesizing {len(extra_items)} cinematic segments "
                f"with per-segment emotions "
                f"(adaptive_voice={strategy.use_adaptive_voice})"
            )
            # v23: Use emotion-aware batch if strategy says + method available
            if (
                strategy.use_adaptive_voice
                and hasattr(self.voice_engine, 'synthesize_batch_with_emotions')
            ):
                self.voice_engine.synthesize_batch_with_emotions(extra_items)
            else:
                # Fallback to old method (drops emotion)
                legacy_items = [(t, p) for t, p, _ in extra_items]
                self.voice_engine.synthesize_batch(legacy_items)

            # Map back to scene attributes
            for scene in script.ayah_scenes:
                sid = f"ayah_{scene.scene_id}"
                for kind in ("hook", "story", "moral"):
                    p = ep_dir / f"{sid}_{kind}.mp3"
                    if p.exists():
                        audio_map[f"{sid}_{kind}"] = str(p)
                        setattr(scene, f"{kind}_audio", str(p))

        return audio_map

    # ─── Color grade resolver ────────────────────────────────────
    def _color_grade_for(self, emotion: str) -> Optional[str]:
        """Per-emotion color grade lookup."""
        if not self.enable_color_grade:
            return None
        return self.color_grades_by_emotion.get(
            emotion,
            self.color_grades_by_emotion.get(
                "warm",
                getattr(self.video_cfg, "color_grade_default", None),
            ),
        )

    # ─── Scene rendering ─────────────────────────────────────────
    def _render_all_scenes(
        self,
        script: EpisodeScript,
        audio_map: Dict[str, str],
        ep_dir: Path,
    ) -> List[str]:
        """Render all scenes — 6-segment cinematic structure per ayah."""
        scenes_dir = ep_dir / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        # ── Intro narrator
        if "intro" in audio_map:
            out = str(scenes_dir / "00_intro.mp4")
            intro_bg = getattr(script.intro_scene, 'image_path', None)
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
                    "color_grade": self._color_grade_for("excited"),
                },
            ), audio_map["intro"])
            outputs.append(out)

        # ── Per-ayah: 6-segment structure
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
            ayah_bg = getattr(scene, 'image_path', None)

            # 1. Hook
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
                        "color_grade": self._color_grade_for("playful"),
                    },
                ), audio_map[f"{sid}_hook"])
                outputs.append(out)

            # 2. Intro text
            if f"{sid}_intro" in audio_map and scene.intro_text:
                out = str(scenes_dir / f"{pfx}b_{sid}_intro.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type, palette=palette,
                    text=scene.intro_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "narrator",
                        "scene_emotion": emotion,
                        "background_image": ayah_bg,
                        "color_grade": self._color_grade_for(emotion),
                    },
                ), audio_map[f"{sid}_intro"])
                outputs.append(out)

            # 3. Analogy
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
                        "color_grade": self._color_grade_for("warm"),
                    },
                ), audio_map[f"{sid}_story"])
                outputs.append(out)

            # 4. Quran recitation
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

            # 5. Explain
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
                        "color_grade": self._color_grade_for(emotion),
                    },
                ), audio_map[f"{sid}_explain"])
                outputs.append(out)

            # 6. Moral / Takeaway
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
                        "color_grade": self._color_grade_for("peaceful"),
                    },
                ), audio_map[f"{sid}_moral"])
                outputs.append(out)

        # ── Mid scenes
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

        # ── Outro
        if "outro" in audio_map:
            out = str(scenes_dir / "99_outro.mp4")
            outro_bg = getattr(script.outro_scene, 'image_path', None)
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
                    "color_grade": self._color_grade_for("peaceful"),
                },
            ), audio_map["outro"])
            outputs.append(out)

        if not outputs:
            raise PipelineError("No scenes rendered", stage="render_scenes")

        logger.info(f"✅ Rendered {len(outputs)} cinematic segments")
        return outputs

    # ─── v22.2: Smart BGM application with per-scene volume curve ──
    def _apply_bgm_smart(
        self,
        raw_video: str,
        bgm_video: str,
        script: Any,
        scene_segments: List[str],
    ) -> str:
        """Apply BGM. Uses volume curve (per-scene) if BGMDirector available,
        else falls back to fixed-volume apply_bgm.
        """
        try:
            from infrastructure.bgm_director import BGMDirector
            from infrastructure.audio_utils import get_audio_duration
        except ImportError:
            logger.info("ℹ️ BGMDirector unavailable — using fixed BGM volume")
            return self.bgm_mixer.apply_bgm(raw_video, bgm_video)

        # Build per-scene metadata aligned with scene_segments order
        scenes_meta: List[dict] = []
        for path_str in scene_segments:
            from pathlib import Path
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

    # ─── Concat scenes (v23: mood-aware transitions) ────────────
    def _concat_scenes(
        self, segments: List[str], output_path: str,
        *,
        script: Any = None,
    ) -> str:
        """Concat scenes. v23 picks per-pair transitions based on emotions."""
        if not (self.enable_crossfades and len(segments) <= 20):
            return self.assembler.concat(segments, output_path, re_encode=False)

        # v23: Try mood-aware transitions if module + script available
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
                pass  # Module not available, fall back

        # Fallback: single-duration crossfade
        return self.bgm_mixer.concat_with_crossfades(
            segments, output_path,
            transition_duration=0.4,
            transition_type="fade",
            assembler=self.assembler,
        )

    def _build_segment_emotions(
        self, script: Any, segments: List[str],
    ) -> List[Optional[str]]:
        """Build a per-segment emotion list matching `segments` order.

        Heuristic mapping based on filename patterns from _render_all_scenes:
          00_intro.mp4         → "excited" (intro narrator)
          *_hook.mp4           → "playful"
          *_intro.mp4 (ayah)   → scene's emotion
          *_analogy.mp4        → "warm"
          *_ayah.mp4           → "reverent" (Quran)
          *_explain.mp4        → scene's emotion
          *_moral.mp4          → "peaceful"
          99_outro.mp4         → "peaceful"
        """
        from pathlib import Path

        # Build a map: scene_id → emotion
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
                # Find ayah scene emotion
                for sid, emo in emotion_by_id.items():
                    if sid in name:
                        emotions.append(emo)
                        break
                else:
                    emotions.append("warm")
            else:
                emotions.append(None)
        return emotions

    # ─── Stage runner with idempotency + retry ──────────────────
    def _run_stage(
        self, name: str, fn: Any, report: EpisodeRunReport, *,
        idem_key: Any = None,
    ) -> Any:
        registry = get_registry()

        # Check checkpoint replay
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

        # v22: Wrap the stage function with retry logic
        try:
            from core.stage_retry import run_with_retry, get_policy
            policy = get_policy(name)
            wrapped_fn = lambda: run_with_retry(
                fn, stage_name=name, policy=policy,
            )
        except ImportError:
            wrapped_fn = fn  # No retry if module missing

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

    # ─── Helpers ─────────────────────────────────────────────────
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
        scene_segments: List[str],
    ) -> None:
        """Remove temp files after upload confirmed."""
        try:
            raw_video.unlink(missing_ok=True)
            for s in scene_segments:
                Path(s).unlink(missing_ok=True)
            for sub in ("scenes", "mastered", "subs"):
                d = ep_dir / sub
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            final = self.paths.videos / f"episode_{branded.stem}.mp4"
            try:
                shutil.move(str(branded), str(final))
                logger.info(f"📦 Final video: {final}")
            except OSError as e:
                logger.warning(f"⚠️ Final move failed: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Cleanup partial failure: {e}")

    def _write_dashboards(self, report: EpisodeRunReport) -> None:
        """v20: Write per-episode + monthly dashboards."""
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
