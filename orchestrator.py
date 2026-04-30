"""
orchestrator.py — VALUE / QEEMA v12.0 (Production)
========================================================
The Orchestrator coordinates all engines through the episode lifecycle.

[Pipeline Stages]
  1. script        → ScriptEngine.generate
  2. audio         → VoiceEngine.generate_episode_audio + master
  3. render_scenes → VisualRenderer.render (parallel-safe)
  4. concat_raw    → FFmpeg concat scene segments
  5. wrap_branded  → IntroOutroEngine.wrap_episode
  6. thumbnail     → ThumbnailEngine.create
  7. upload        → VideoUploader.upload
  8. cleanup       → ONLY after upload + DB confirmation

[Key Improvements vs v11]
- Idempotency: per-episode key prevents duplicate YouTube uploads
- Observability: structured spans for every stage, written to spans.jsonl
- Stage-level metrics: latency histograms + success/failure counters
- Transactional cleanup: only delete after BOTH upload and DB commit succeed
- Dependency injection: orchestrator knows nothing about provider details
- Signal-driven shutdown: SIGTERM triggers graceful stop
- EpisodeRunReport: structured outcome for monitoring

[State Machine]
  PENDING → PROCESSING → COMPLETED
                       ↘ FAILED (transient — retryable)
                       ↘ FAILED_QUALITY (script gate failed)
                       ↘ FAILED_PERMANENT (no retry)
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import PathsConfig, VideoConfig
from core.exceptions import (
    PermanentError,
    PipelineError,
    QualityGateError,
    QeemaError,
    TransientError,
)
from core.idempotency import (
    CheckpointStore,
    IdempotencyKey,
)
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
from engines.voice_engine import VoiceEngine

logger = logging.getLogger(__name__)


# Bump this when stages or their inputs change in a way that should
# invalidate prior checkpoints. The idempotency key incorporates this,
# so a version change forces a clean replay.
PIPELINE_VERSION: str = "12.0.0"


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

    def summary(self) -> str:
        emoji = "✅" if self.success else "❌"
        lines = [
            f"{emoji} Episode {self.episode_number} — {self.final_status} "
            f"({self.total_duration_sec:.1f}s)"
        ]
        for s in self.stages:
            mark = "✓" if s.success else "✗"
            lines.append(f"  {mark} {s.name:<14} {s.duration_sec:>6.1f}s {s.detail}")
        if self.video_url:
            lines.append(f"  📺 {self.video_url}")
        if self.error:
            lines.append(f"  ⚠️ {self.error}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Orchestrator
# ════════════════════════════════════════════════════════════════
class Orchestrator:
    """Production orchestrator with full DI."""

    def __init__(
        self,
        *,
        script_engine: ScriptEngine,
        voice_engine: VoiceEngine,
        visual_renderer: VisualRenderer,
        assembler: VideoAssembler,
        repository: EpisodeRepository,
        uploader: Optional[VideoUploader],   # None for dry-run
        intro_outro: IntroOutroBuilder,
        thumbnail_builder: ThumbnailBuilder,
        quality_validator: QualityValidator,
        paths: PathsConfig,
        video_cfg: VideoConfig,
        dry_run: bool = False,
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
        self._shutdown_requested: bool = False

        # Checkpoint store: enables idempotent stage replay across crashes.
        # Checkpoints live under paths.root/state/checkpoints, sharded by
        # idempotency key prefix.
        checkpoints_root = paths.root / "state" / "checkpoints"
        self._checkpoints: CheckpointStore = CheckpointStore(checkpoints_root)

        # Span emitter: structured tracing for forensic debugging.
        # Picks up the globally configured emitter (set up in main.py).
        self._emitter: SpanEmitter = get_emitter()

    # ───────────────────────────────────────────────────────────
    # Lifecycle
    # ───────────────────────────────────────────────────────────
    def warmup(self) -> None:
        logger.info("🔥 Warming up orchestrator")
        self.visual_renderer.warmup()
        # Pre-build intro/outro (cached afterward)
        self.intro_outro.build_intro()
        self.intro_outro.build_outro()
        logger.info("✅ Orchestrator warm")

    def shutdown(self) -> None:
        logger.info("🧹 Shutting down orchestrator")
        try:
            self.visual_renderer.shutdown()
        except Exception as e:
            logger.warning(f"⚠️ visual_renderer shutdown error: {e}")

    def request_shutdown(self) -> None:
        """Signal that we want to stop after current stage completes."""
        self._shutdown_requested = True
        logger.warning("⚠️ Shutdown requested; will stop after current stage")

    # ───────────────────────────────────────────────────────────
    # Public entry points
    # ───────────────────────────────────────────────────────────
    def run_next(self) -> Optional[EpisodeRunReport]:
        """Pick the next pending episode and run it."""
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
            logger, episode_number=episode_number, stage="orchestrator"
        )

        # ── Compute an idempotency key BEFORE any side effects.
        # Same episode + same version + same key inputs → same key,
        # which lets us skip already-completed stages on replay and
        # prevents duplicate YouTube uploads after partial failures.
        idem_key = IdempotencyKey.derive(
            episode_number=episode_number,
            pipeline_version=PIPELINE_VERSION,
            inputs={
                "voice_id": self.voice_engine._primary_voice_id(),
                "video_resolution": f"{self.video_cfg.width}x{self.video_cfg.height}",
                "audio_bitrate": self.video_cfg.audio_bitrate,
                "fps": self.video_cfg.fps,
                "dry_run": self.dry_run,
            },
        )
        self._checkpoints.initialize(idem_key, episode_number=episode_number)
        log.info(f"🔑 idempotency key: {idem_key.value[:8]}... (full in checkpoints)")

        # Wrap the entire run in a top-level span. Every stage span is a
        # child of this one, so the trace tree mirrors the pipeline.
        with self._emitter.span(
            "episode.run",
            episode_number=episode_number,
            idempotency_key=idem_key.value,
            pipeline_version=PIPELINE_VERSION,
            dry_run=self.dry_run,
        ):
            return self._run_pipeline(
                episode_number=episode_number,
                report=report,
                start=start,
                log=log,
                idem_key=idem_key,
            )

    def _run_pipeline(
        self,
        *,
        episode_number: int,
        report: EpisodeRunReport,
        start: float,
        log,
        idem_key: IdempotencyKey,
    ) -> EpisodeRunReport:
        """Pipeline body, extracted so the top-level span context stays clean."""

        # ── Phase 0: register with repo
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
            # ── Stage 1: Script
            script: EpisodeScript = self._run_stage(
                "script",
                lambda: self.script_engine.generate(episode_number),
                report,
            )
            script.episode_id = episode_id
            if self._shutdown_requested:
                raise RuntimeError("Shutdown requested before audio stage")

            # ── Stage 2: Audio
            audio_map = self._run_stage(
                "audio",
                lambda: self.voice_engine.generate_episode_audio(script, ep_dir),
                report,
            )
            mastered = self._run_stage(
                "audio_master",
                lambda: self.voice_engine.master_episode(audio_map, ep_dir),
                report,
            )
            if self._shutdown_requested:
                raise RuntimeError("Shutdown requested before render")

            # ── Stage 3: Render scenes
            scene_segments = self._run_stage(
                "render_scenes",
                lambda: self._render_all_scenes(script, mastered, ep_dir),
                report,
            )
            if self._shutdown_requested:
                raise RuntimeError("Shutdown requested before concat")

            # ── Stage 4: Concat raw
            raw_video = ep_dir / "raw_episode.mp4"
            self._run_stage(
                "concat_raw",
                lambda: self.assembler.concat(
                    scene_segments, str(raw_video), re_encode=False,
                ),
                report,
            )

            # ── Stage 5: Wrap branded
            branded = ep_dir / "branded_episode.mp4"
            self._run_stage(
                "wrap_branded",
                lambda: self.intro_outro.wrap_episode(str(raw_video), str(branded)),
                report,
            )

            # ── Stage 6: Thumbnail
            thumb = self._run_stage(
                "thumbnail",
                lambda: self.thumbnail_builder.create(script, episode_number),
                report,
            )

            # ── Stage 7: Upload
            # Idempotency-protected: if a previous run completed this stage
            # but crashed before marking COMPLETED in the DB, the next run
            # will skip the upload and reuse the recorded video_id.
            # This prevents duplicate uploads on YouTube.
            if self.dry_run:
                log.info("🧪 DRY RUN: skipping upload")
                report.video_url = "dry-run-no-upload"
                upload_ok = True
            elif self.uploader is None:
                raise RuntimeError("No uploader configured (and not dry-run)")
            else:
                def _do_upload() -> dict:
                    result = self.uploader.upload(
                        UploadRequest(
                            video_path=str(branded),
                            title=script.youtube_title,
                            description=script.youtube_description,
                            tags=list(script.youtube_tags),
                            thumbnail_path=thumb,
                        )
                    )
                    return {
                        "video_id": result.video_id,
                        "video_url": result.video_url,
                        "thumbnail_uploaded": result.thumbnail_uploaded,
                    }

                upload_result = self._run_stage(
                    "upload",
                    _do_upload,
                    report,
                    idem_key=idem_key,
                )
                report.video_url = upload_result["video_url"]
                upload_ok = True

            # ── Stage 8: Mark complete in DB
            self.repository.update_status(
                episode_id,
                EpisodeStatus.COMPLETED.value,
                youtube_url=report.video_url,
            )

            # ── Stage 9: Transactional cleanup (ONLY now)
            self._safe_cleanup(ep_dir, branded, raw_video, scene_segments)

            report.success = True
            report.final_status = EpisodeStatus.COMPLETED.value

        except QualityGateError as e:
            self._mark_failure(
                report, episode_id, EpisodeStatus.FAILED_QUALITY.value,
                f"Quality gate failed: {e}",
            )
        except PermanentError as e:
            self._mark_failure(
                report, episode_id, EpisodeStatus.FAILED_PERMANENT.value,
                f"Permanent error: {e}",
            )
        except (TransientError, PipelineError) as e:
            self._mark_failure(
                report, episode_id, EpisodeStatus.FAILED.value,
                f"{type(e).__name__}: {e}",
            )
        except Exception as e:
            self._mark_failure(
                report, episode_id, EpisodeStatus.FAILED.value,
                f"Unexpected error: {type(e).__name__}: {e}",
            )

        report.total_duration_sec = time.monotonic() - start
        log.info("\n" + report.summary())
        return report

    # ───────────────────────────────────────────────────────────
    # Internals
    # ───────────────────────────────────────────────────────────
    def _run_stage(
        self,
        name: str,
        fn,
        report: EpisodeRunReport,
        *,
        idem_key: Optional[IdempotencyKey] = None,
    ) -> Any:
        """
        Run a pipeline stage with structured tracing and metrics.

        If `idem_key` is supplied AND the stage was already recorded as
        completed, the function is NOT re-run — the recorded output is
        returned. This is the crash-recovery path that prevents
        duplicate YouTube uploads.

        Note: not every stage is checkpoint-replayable in practice. Stages
        that produce files (render_scenes, concat) can't be replayed if
        cleanup ran. The caller decides whether to pass idem_key.
        """
        registry = get_registry()

        # If we have a key and this stage was already completed, skip work.
        if idem_key is not None and self._checkpoints.is_completed(idem_key, name):
            cached = self._checkpoints.get_output(idem_key, name)
            logger.info(
                f"⏭️  stage '{name}' already completed; skipping (replay)"
            )
            report.stages.append(StageResult(
                name=name, success=True, duration_sec=0.0,
                detail="skipped (idempotent replay)",
            ))
            registry.counter("pipeline.stage.skipped").inc(
                labels={"stage": name},
            )
            return cached

        with self._emitter.span(f"stage.{name}", stage=name) as span:
            start = time.monotonic()
            try:
                result = fn()
                duration = time.monotonic() - start

                report.stages.append(StageResult(
                    name=name, success=True, duration_sec=duration,
                ))
                registry.histogram("pipeline.stage.duration_ms").record(
                    duration * 1000.0,
                    labels={"stage": name, "outcome": "success"},
                )
                registry.counter("pipeline.stage.success").inc(
                    labels={"stage": name},
                )

                # Best-effort checkpoint write. Only records dict outputs;
                # non-dict results aren't checkpoint-replayable but the
                # stage still ran successfully so we don't fail.
                if idem_key is not None and isinstance(result, dict):
                    self._checkpoints.record(
                        idem_key,
                        stage=name,
                        duration_ms=int(duration * 1000),
                        output=result,
                    )

                span.set("duration_ms", int(duration * 1000))
                return result
            except Exception as e:
                duration = time.monotonic() - start
                report.stages.append(StageResult(
                    name=name, success=False, duration_sec=duration,
                    detail=f"{type(e).__name__}",
                ))
                registry.histogram("pipeline.stage.duration_ms").record(
                    duration * 1000.0,
                    labels={"stage": name, "outcome": "failure"},
                )
                registry.counter("pipeline.stage.failure").inc(
                    labels={"stage": name, "error_type": type(e).__name__},
                )
                # Span automatically captures the exception via context exit
                raise

    def _render_all_scenes(
        self,
        script: EpisodeScript,
        audio_map: Dict[str, str],
        ep_dir: Path,
    ) -> List[str]:
        """Render every scene sequentially. Returns list of mp4 paths in order."""
        scenes_dir = ep_dir / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        # ── Intro narrator
        if "intro" in audio_map:
            out = str(scenes_dir / "00_intro.mp4")
            self.visual_renderer.render(
                SceneRenderRequest(
                    scene_type=script.intro_scene.visual_scene.value,
                    palette=script.intro_scene.palette.value,
                    text=script.intro_scene.narrator_text,
                    is_ayah=False,
                    keywords=script.intro_scene.keywords,
                    output_path=out,
                ),
                audio_map["intro"],
            )
            outputs.append(out)

        # ── Per-ayah scenes (intro_text → recitation → explain_text)
        for i, scene in enumerate(script.ayah_scenes):
            sid = f"ayah_{scene.scene_id}"
            sid_prefix = f"{i + 1:02d}"

            # 1. intro_text scene (narrator voice, low-key palette)
            if f"{sid}_intro" in audio_map:
                out = str(scenes_dir / f"{sid_prefix}a_{sid}_intro.mp4")
                self.visual_renderer.render(
                    SceneRenderRequest(
                        scene_type=scene.visual_scene.value,
                        palette=scene.palette.value,
                        text=scene.intro_text,
                        is_ayah=False,
                        keywords=scene.keywords,
                        output_path=out,
                    ),
                    audio_map[f"{sid}_intro"],
                )
                outputs.append(out)

            # 2. Ayah recitation scene (gold typography, large)
            if f"{sid}_ayah" in audio_map:
                out = str(scenes_dir / f"{sid_prefix}b_{sid}_ayah.mp4")
                self.visual_renderer.render(
                    SceneRenderRequest(
                        scene_type=scene.visual_scene.value,
                        palette=scene.palette.value,
                        text=scene.ayah.text,
                        is_ayah=True,
                        keywords=scene.keywords,
                        output_path=out,
                    ),
                    audio_map[f"{sid}_ayah"],
                )
                outputs.append(out)

            # 3. explain_text scene
            if f"{sid}_explain" in audio_map:
                out = str(scenes_dir / f"{sid_prefix}c_{sid}_explain.mp4")
                self.visual_renderer.render(
                    SceneRenderRequest(
                        scene_type=scene.visual_scene.value,
                        palette=scene.palette.value,
                        text=scene.explain_text,
                        is_ayah=False,
                        keywords=scene.keywords,
                        output_path=out,
                    ),
                    audio_map[f"{sid}_explain"],
                )
                outputs.append(out)

        # ── Mid scenes (if any)
        for sc in script.mid_scenes:
            key = f"mid_{sc.scene_id}"
            if key in audio_map:
                out = str(scenes_dir / f"mid_{sc.scene_id}.mp4")
                self.visual_renderer.render(
                    SceneRenderRequest(
                        scene_type=sc.visual_scene.value,
                        palette=sc.palette.value,
                        text=sc.narrator_text,
                        is_ayah=False,
                        keywords=sc.keywords,
                        output_path=out,
                    ),
                    audio_map[key],
                )
                outputs.append(out)

        # ── Outro
        if "outro" in audio_map:
            out = str(scenes_dir / "99_outro.mp4")
            self.visual_renderer.render(
                SceneRenderRequest(
                    scene_type=script.outro_scene.visual_scene.value,
                    palette=script.outro_scene.palette.value,
                    text=script.outro_scene.narrator_text,
                    is_ayah=False,
                    keywords=script.outro_scene.keywords,
                    output_path=out,
                ),
                audio_map["outro"],
            )
            outputs.append(out)

        if not outputs:
            raise PipelineError(
                "No scenes rendered (audio_map was empty?)",
                stage="render_scenes",
            )
        logger.info(f"✅ Rendered {len(outputs)} scenes")
        return outputs

    def _mark_failure(
        self,
        report: EpisodeRunReport,
        episode_id: str,
        status_value: str,
        error_str: str,
    ) -> None:
        report.final_status = status_value
        report.error = error_str
        try:
            self.repository.update_status(episode_id, status_value)
        except Exception as e:
            logger.warning(f"⚠️ DB status update failed: {e}")

    def _safe_cleanup(
        self,
        ep_dir: Path,
        branded_video: Path,
        raw_video: Path,
        scene_segments: List[str],
    ) -> None:
        """
        Delete intermediates ONLY after upload + DB commit confirmed.
        Preserves: branded_video (final delivery) + script JSON.
        """
        try:
            # Delete raw (already concatenated into branded)
            raw_video.unlink(missing_ok=True)
            # Delete scene segments
            for s in scene_segments:
                Path(s).unlink(missing_ok=True)
            # Delete scenes/ dir, mastered/ dir, html_templates fragments
            for sub in ("scenes", "mastered"):
                d = ep_dir / sub
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            # Move branded to final output dir, then drop ep_dir
            final = self.paths.videos / f"episode_{branded_video.stem}.mp4"
            try:
                shutil.move(str(branded_video), str(final))
                logger.info(f"📦 Final video moved: {final}")
            except OSError as e:
                logger.warning(f"⚠️ final move failed: {e}")
            logger.info(f"🧹 Cleaned up {ep_dir}")
        except Exception as e:
            # Cleanup failures are NOT pipeline failures
            logger.warning(f"⚠️ Cleanup partial failure: {e}")
