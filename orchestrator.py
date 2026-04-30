"""
orchestrator.py — VALUE / QEEMA v11.0 (Production)
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

[Key Improvements vs v10]
- Transactional cleanup: only delete after BOTH upload and DB commit succeed
- Dependency injection: orchestrator knows nothing about provider details
- Stage-level metrics: every step is timed and logged
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
from engines.script_engine import ScriptEngine
from engines.voice_engine import VoiceEngine

logger = logging.getLogger(__name__)


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
            if self.dry_run:
                log.info("🧪 DRY RUN: skipping upload")
                report.video_url = "dry-run-no-upload"
                upload_ok = True
            elif self.uploader is None:
                raise RuntimeError("No uploader configured (and not dry-run)")
            else:
                result = self._run_stage(
                    "upload",
                    lambda: self.uploader.upload(
                        UploadRequest(
                            video_path=str(branded),
                            title=script.youtube_title,
                            description=script.youtube_description,
                            tags=list(script.youtube_tags),
                            thumbnail_path=thumb,
                        )
                    ),
                    report,
                )
                report.video_url = result.video_url
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
    def _run_stage(self, name: str, fn, report: EpisodeRunReport) -> Any:
        start = time.monotonic()
        try:
            result = fn()
            duration = time.monotonic() - start
            report.stages.append(StageResult(
                name=name, success=True, duration_sec=duration,
            ))
            return result
        except Exception as e:
            duration = time.monotonic() - start
            report.stages.append(StageResult(
                name=name, success=False, duration_sec=duration,
                detail=f"{type(e).__name__}",
            ))
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
