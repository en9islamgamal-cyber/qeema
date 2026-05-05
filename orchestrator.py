"""
orchestrator.py — VALUE / QEEMA v17.0 (Revolution: stream-copy + parallel + AI images)
==========================================================
[Pipeline Stages v17 — in order]
  1. script        → ScriptEngine.generate (cinematic: hook/story/moral)
  2. audio         → VoiceEngine.generate_episode_audio (v14 aware)
  3. audio_master  → VoiceEngine.master_episode
  4. render_scenes → VisualRenderer.render (v14 scene_emotion + text_style)
  5. concat_raw    → BGMMixer.concat_with_crossfades (smooth transitions)
  6. bgm_mix       → BGMMixer.apply_bgm (nasheed background)
  7. subtitles     → SubtitleEngine.generate + BGMMixer.burn_subtitles (opt)
  8. color_grade   → BGMMixer.apply_color_grade (warm tint)
  9. wrap_branded  → IntroOutroEngine.wrap_episode
  10. thumbnail    → ThumbnailEngine.create
  11. upload       → VideoUploader.upload
  12. cleanup      → ONLY after upload + DB confirmation

[Key v14 Changes]
- Cinematic ayah rendering: hook/story/moral each get their own video segment
- BGM mixing stage after concat (nasheed at low volume)
- Optional subtitle burn (controlled by ENABLE_SUBTITLES env var)
- Color grading pass for warm visual tone
- SceneRenderRequest now passes text_style + scene_emotion
- VoiceEngine called with v14-aware segment keys
"""
from __future__ import annotations

import logging
import os
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

logger = logging.getLogger(__name__)

PIPELINE_VERSION: str = "18.0.0"


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
    # v18 NEW
    tafsir_validation: List[Dict[str, Any]] = field(default_factory=list)
    quality_score: Optional[float] = None
    cost_usd: Optional[float] = None

    def summary(self) -> str:
        emoji = "✅" if self.success else "❌"
        lines = [
            f"{emoji} Episode {self.episode_number} — {self.final_status} "
            f"({self.total_duration_sec:.1f}s)"
        ]
        for s in self.stages:
            mark = "✓" if s.success else "✗"
            lines.append(f"  {mark} {s.name:<16} {s.duration_sec:>6.1f}s {s.detail}")
        if self.video_url:
            lines.append(f"  📺 {self.video_url}")
        if self.error:
            lines.append(f"  ⚠️ {self.error}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Orchestrator v18
# ════════════════════════════════════════════════════════════════
class Orchestrator:
    """Production orchestrator with full DI — v18 production-grade pipeline."""

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
        image_engine=None,
        # v18 NEW engines (all optional — graceful degradation)
        tafsir_validator=None,        # engines.tafsir_validator.TafsirValidator
        hook_optimizer=None,          # engines.hook_optimizer.HookOptimizer
        review_gate=None,             # engines.review_gate.ReviewGate
        cost_tracker=None,            # core.cost_tracker.CostTracker
        color_grades_by_emotion: Optional[Dict[str, str]] = None,  # v18
        approval_explicit: bool = False,  # v18: bypass review gate
        dry_run: bool = False,
        enable_subtitles: bool = True,    # v18: ON by default
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
        self.image_engine = image_engine

        # v18 NEW
        self.tafsir_validator = tafsir_validator
        self.hook_optimizer = hook_optimizer
        self.review_gate = review_gate
        self.cost_tracker = cost_tracker
        self.color_grades_by_emotion = color_grades_by_emotion or {}
        self.approval_explicit = approval_explicit

        # v17 engines
        self.bgm_mixer = bgm_mixer or BGMMixer(paths=paths)
        self.subtitle_engine = subtitle_engine or SubtitleEngine(paths=paths)

        # Feature flags — v18: subtitles ON by default
        self.enable_subtitles = enable_subtitles or (
            os.getenv("ENABLE_SUBTITLES", "true").lower() == "true"
        )
        # Allow ENV override
        env_color = os.getenv("ENABLE_COLOR_GRADE")
        if env_color is not None:
            self.enable_color_grade = env_color.lower() == "true"
        else:
            self.enable_color_grade = enable_color_grade

        env_xfade = os.getenv("ENABLE_CROSSFADES")
        if env_xfade is not None:
            self.enable_crossfades = env_xfade.lower() == "true"
        else:
            self.enable_crossfades = enable_crossfades

        env_bgm = os.getenv("ENABLE_BGM")
        self.enable_bgm = env_bgm.lower() == "true" if env_bgm is not None else True

        self._shutdown_requested: bool = False
        checkpoints_root = paths.root / "state" / "checkpoints"
        self._checkpoints = CheckpointStore(checkpoints_root)
        self._emitter: SpanEmitter = get_emitter()

    # ─── Lifecycle ───────────────────────────────────────────────
    def warmup(self) -> None:
        logger.info("🔥 Warming up v20 orchestrator")
        self.visual_renderer.warmup()
        self.intro_outro.build_intro()
        self.intro_outro.build_outro()
        logger.info("✅ Orchestrator v20 warm")

    def shutdown(self) -> None:
        logger.info("🧹 Shutting down orchestrator")
        try:
            self.visual_renderer.shutdown()
        except Exception as e:
            logger.warning(f"⚠️ visual_renderer shutdown error: {e}")

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        logger.warning("⚠️ Shutdown requested")

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
        log = with_context(logger, episode_number=episode_number, stage="orchestrator")

        idem_key = IdempotencyKey.derive(
            episode_number=episode_number,
            pipeline_version=PIPELINE_VERSION,
            inputs={
                "voice_id": self.voice_engine._primary_voice_id(),
                "video_resolution": f"{self.video_cfg.width}x{self.video_cfg.height}",
                "dry_run": self.dry_run,
            },
        )
        self._checkpoints.initialize(idem_key, episode_number=episode_number)

        with self._emitter.span(
            "episode.run",
            episode_number=episode_number,
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

    def _run_pipeline(self, *, episode_number, report, start, log, idem_key) -> EpisodeRunReport:
        # Phase 0: repo registration
        try:
            record = self.repository.get_or_create(episode_number)
            episode_id = record["id"]
            self.repository.update_status(episode_id, EpisodeStatus.PROCESSING.value)
        except Exception as e:
            report.final_status = "REPO_FAILED"
            report.error = f"Repository error: {e}"
            log.error(f"❌ {report.error}")
            return report

        ep_dir = self.paths.temp_episodes / f"episode_{episode_number:03d}"
        ep_dir.mkdir(parents=True, exist_ok=True)

        try:
            # ── Stage 1: Script (cinematic v16)
            script: EpisodeScript = self._run_stage("script",
                lambda: self.script_engine.generate(episode_number), report)
            script.episode_id = episode_id
            if self._shutdown_requested:
                raise RuntimeError("Shutdown before audio")

            # ── v18 Stage 1.25: TAFSIR VALIDATION (CRITICAL)
            # Religious accuracy check via Claude Opus + authentic tafsir.
            # Blocks publication if explanations contradict authentic sources.
            if self.tafsir_validator is not None:
                tafsir_results = self._run_stage(
                    "tafsir_validation",
                    lambda: self._validate_tafsir(script),
                    report,
                )
                report.tafsir_validation = tafsir_results
                # Hard fail if any ayah's explanation didn't pass
                rejected = [r for r in tafsir_results if not r.get("passed", False)]
                if rejected:
                    concerns = []
                    for r in rejected:
                        ayah_n = r.get("ayah", "?")
                        for c in r.get("concerns", []):
                            concerns.append(f"Ayah {ayah_n}: {c}")
                    raise QualityGateError(
                        f"Tafsir validation FAILED for {len(rejected)} ayah(s)",
                        critiques=concerns,
                        episode_number=episode_number,
                        stage="tafsir_validation",
                    )
                logger.info(
                    f"✅ Tafsir validation passed: {len(tafsir_results)} ayahs "
                    f"(method={tafsir_results[0].get('method') if tafsir_results else 'n/a'})"
                )

            # ── Stage 1.5: AI image generation (v16 NEW — Leonardo)
            # Runs in parallel with audio if enabled
            if self.image_engine is not None:
                self._run_stage("ai_images",
                    lambda: self._generate_ai_images(script, ep_dir), report)

            # ── Stage 2: Audio
            audio_map = self._run_stage("audio",
                lambda: self._generate_audio_v14(script, ep_dir), report)

            mastered = self._run_stage("audio_master",
                lambda: self.voice_engine.master_episode(audio_map, ep_dir), report)
            if self._shutdown_requested:
                raise RuntimeError("Shutdown before render")

            # ── Stage 3: Render scenes (v14: more segments per ayah)
            scene_segments = self._run_stage("render_scenes",
                lambda: self._render_all_scenes_v14(script, mastered, ep_dir), report)
            if self._shutdown_requested:
                raise RuntimeError("Shutdown before concat")

            # ── Stage 4: Concat with crossfades (v14 NEW)
            raw_video = ep_dir / "raw_episode.mp4"
            self._run_stage("concat_raw",
                lambda: self._concat_scenes(scene_segments, str(raw_video)), report)

            # ── Stage 5: BGM mixing (v14 NEW)
            bgm_video = ep_dir / "bgm_episode.mp4"
            self._run_stage("bgm_mix",
                lambda: self.bgm_mixer.apply_bgm(str(raw_video), str(bgm_video)), report)
            bgm_result = str(bgm_video) if bgm_video.exists() else str(raw_video)

            # ── Stage 6: Subtitles (v14 NEW, optional)
            post_subs = bgm_result
            if self.enable_subtitles:
                subs_video = ep_dir / "subs_episode.mp4"
                timing_map = self.subtitle_engine.build_timing_map_from_audio(mastered)
                ass_path = self.subtitle_engine.generate(script, timing_map, ep_dir / "subs")
                post_subs_result = self.bgm_mixer.burn_subtitles(
                    bgm_result, ass_path, str(subs_video)
                )
                post_subs = post_subs_result
                report.stages.append(StageResult("subtitles", True, 0.0, ass_path))

            # ── v17: Color grade is BAKED INTO per-scene encoding
            # No separate stage needed. Saves 10+ minutes of re-encode.
            pre_brand = post_subs

            # ── Stage 8: Wrap branded (intro + outro)
            # v17: stream-copy concat now works (intro/outro/body share codec params)
            # → completes in ~5 sec instead of 15 min timeout.
            cta_audio = mastered.get("cta")
            branded = ep_dir / "branded_episode.mp4"
            self._run_stage("wrap_branded",
                lambda: self.intro_outro.wrap_episode(
                    pre_brand, str(branded), cta_audio_path=cta_audio
                ), report)

            # ── Stage 9: Thumbnail (v18: 3 variants for A/B testing)
            thumbs: List[str] = []
            if hasattr(self.thumbnail_builder, 'create_variants'):
                thumbs = self._run_stage("thumbnail_variants",
                    lambda: self.thumbnail_builder.create_variants(script, episode_number),
                    report)
                thumb = thumbs[0] if thumbs else None
            else:
                thumb = self._run_stage("thumbnail",
                    lambda: self.thumbnail_builder.create(script, episode_number), report)
                thumbs = [thumb] if thumb else []

            # ── v18: REVIEW GATE (CRITICAL — first 10 episodes manual review)
            if self.review_gate is not None:
                validation_summary = {
                    "quality_score": getattr(report, "quality_score", None),
                    "tafsir_validation": getattr(report, "tafsir_validation", []),
                    "stages": [
                        {"name": s.name, "passed": s.passed, "duration_sec": s.duration_sec}
                        for s in report.stages
                    ],
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
                        f"⏸ Review gate BLOCKED publication: {verdict.reason}"
                    )
                    log.warning(f"📋 Review summary: {verdict.review_file}")
                    self.repository.update_status(
                        episode_id, "awaiting_review",
                        youtube_url=None,
                    )
                    report.video_url = f"file://{branded}"
                    report.success = True  # Pipeline succeeded — just paused for review
                    report.final_status = "awaiting_review"
                    return report

            # ── Stage 10: Upload
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
                    return {"video_id": result.video_id, "video_url": result.video_url}

                upload_result = self._run_stage("upload", _do_upload, report, idem_key=idem_key)
                report.video_url = upload_result["video_url"]

                # v18: Upload additional thumbnail variants for YouTube Test & Compare
                if len(thumbs) > 1 and hasattr(self.uploader, "upload_thumbnail_variant"):
                    for i, t in enumerate(thumbs[1:], start=2):
                        try:
                            self.uploader.upload_thumbnail_variant(
                                upload_result["video_id"], t, slot=i
                            )
                            log.info(f"✅ Uploaded thumbnail variant {i}")
                        except Exception as e:
                            log.warning(f"⚠️ Thumbnail variant {i} upload failed: {e}")

            # ── Stage 11: Mark complete
            self.repository.update_status(
                episode_id, EpisodeStatus.COMPLETED.value,
                youtube_url=report.video_url,
            )

            # ── Stage 12: Cleanup
            self._safe_cleanup(ep_dir, branded, Path(bgm_result), scene_segments)
            report.success = True
            report.final_status = EpisodeStatus.COMPLETED.value

        except QualityGateError as e:
            self._mark_failure(report, episode_id, EpisodeStatus.FAILED_QUALITY.value, str(e))
        except PermanentError as e:
            self._mark_failure(report, episode_id, EpisodeStatus.FAILED_PERMANENT.value, str(e))
        except (TransientError, PipelineError, Exception) as e:
            self._mark_failure(report, episode_id, EpisodeStatus.FAILED.value,
                               f"{type(e).__name__}: {e}")

        report.total_duration_sec = time.monotonic() - start
        log.info("\n" + report.summary())
        return report

    # ─── v14: Audio generation (handles new hook/story/moral fields) ──
    def _generate_audio_v14(self, script: EpisodeScript, ep_dir: Path) -> Dict[str, str]:
        """
        v14-aware audio generation. Calls VoiceEngine.generate_episode_audio
        after patching the script to include v14 segment keys.

        For backward compat with VoiceEngine (which reads intro_text/explain_text),
        we call the standard method but also synthesize hook/story/moral separately.
        """
        # Use the standard voice engine (it handles intro, explain, outro)
        audio_map = self.voice_engine.generate_episode_audio(script, ep_dir)

        # Synthesize v14 NEW segments: hook, story, moral
        extra_items = []
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            if scene.hook_text:
                p = str(ep_dir / f"{sid}_hook.mp3")
                extra_items.append((scene.hook_text, p))
            if scene.story_text:
                p = str(ep_dir / f"{sid}_story.mp3")
                extra_items.append((scene.story_text, p))
            if scene.moral_text:
                p = str(ep_dir / f"{sid}_moral.mp3")
                extra_items.append((scene.moral_text, p))

        if extra_items:
            logger.info(f"🎙️ Synthesizing {len(extra_items)} cinematic segments")
            self.voice_engine.synthesize_batch(extra_items)
            for scene in script.ayah_scenes:
                sid = f"ayah_{scene.scene_id}"
                hook_p = str(ep_dir / f"{sid}_hook.mp3")
                story_p = str(ep_dir / f"{sid}_story.mp3")
                moral_p = str(ep_dir / f"{sid}_moral.mp3")
                if Path(hook_p).exists():
                    audio_map[f"{sid}_hook"] = hook_p
                    scene.hook_audio = hook_p
                if Path(story_p).exists():
                    audio_map[f"{sid}_story"] = story_p
                    scene.story_audio = story_p
                if Path(moral_p).exists():
                    audio_map[f"{sid}_moral"] = moral_p
                    scene.moral_audio = moral_p

        return audio_map

    # ─── v16: AI image generation (Leonardo) ────────────────────
    def _generate_ai_images(
        self,
        script: EpisodeScript,
        ep_dir: Path,
    ) -> Dict[str, str]:
        """
        v16: Generate AI background images for all scenes via Leonardo.
        Stores paths on scene.image_path. Returns map of scene_id → image_path.
        Failures fall back to CSS gradients (gracefully).
        """
        if self.image_engine is None:
            return {}

        images_dir = ep_dir / "ai_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        result: Dict[str, str] = {}

        # Submit jobs in parallel (Leonardo handles concurrent submissions)
        import concurrent.futures
        max_workers = 3  # be polite to Leonardo API

        def _gen_intro() -> Optional[str]:
            prompt = script.intro_scene.visual_prompt
            if not prompt:
                return None
            return self.image_engine.generate(
                prompt=prompt,
                output_path=str(images_dir / "intro.png"),
                is_hero=True,
                episode_number=script.episode_number,
            )

        def _gen_outro() -> Optional[str]:
            prompt = script.outro_scene.visual_prompt
            if not prompt:
                return None
            return self.image_engine.generate(
                prompt=prompt,
                output_path=str(images_dir / "outro.png"),
                is_hero=True,
                episode_number=script.episode_number,
            )

        def _gen_ayah(scene) -> Optional[str]:
            prompt = scene.visual_prompt
            if not prompt:
                return None
            return self.image_engine.generate(
                prompt=prompt,
                output_path=str(images_dir / f"ayah_{scene.scene_id}.png"),
                is_hero=False,
                episode_number=script.episode_number,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="img"
        ) as executor:
            futures: Dict[Any, Any] = {}
            futures[executor.submit(_gen_intro)] = ("intro", script.intro_scene)
            futures[executor.submit(_gen_outro)] = ("outro", script.outro_scene)
            for s in script.ayah_scenes:
                futures[executor.submit(_gen_ayah, s)] = (f"ayah_{s.scene_id}", s)

            for fut in concurrent.futures.as_completed(futures):
                key, scene = futures[fut]
                try:
                    path = fut.result()
                    if path:
                        scene.image_path = path
                        result[key] = path
                        logger.info(f"🎨 AI image: {key} → {Path(path).name}")
                    else:
                        logger.info(f"⚠️ AI image: {key} → fallback to CSS")
                except Exception as e:
                    logger.warning(f"⚠️ AI image failed for {key}: {e}")

        logger.info(f"✅ AI images: {len(result)}/{len(futures)} generated")
        return result

    # ─── v18: Tafsir validation ───────────────────────────────────
    def _validate_tafsir(self, script) -> List[Dict[str, Any]]:
        """v18: Validate every ayah explanation against authentic tafsir.

        Returns a list of validation reports, one per ayah.
        Hard fails if any ayah's explanation contradicts authentic sources.
        """
        if self.tafsir_validator is None:
            return []

        results: List[Dict[str, Any]] = []
        import concurrent.futures

        def _validate_one(scene) -> Dict[str, Any]:
            try:
                ayah_text = getattr(scene.ayah, 'text', '')
                surah_num = getattr(scene.ayah, 'surah', 0)
                ayah_num = getattr(scene.ayah, 'number', 0)
                explanation = getattr(scene, 'explain_text', '')
                analogy = getattr(scene, 'story_text', '') or None
                takeaway = getattr(scene, 'moral_text', '') or None

                report = self.tafsir_validator.validate(
                    ayah_text=ayah_text,
                    surah=surah_num,
                    ayah=ayah_num,
                    llm_explanation=explanation,
                    llm_analogy=analogy,
                    llm_takeaway=takeaway,
                )
                return {
                    "ayah": ayah_num,
                    "surah": surah_num,
                    "passed": report.passed,
                    "confidence": report.confidence,
                    "concerns": report.concerns,
                    "method": report.method,
                }
            except Exception as e:
                logger.error(f"❌ Tafsir validation error for ayah: {e}")
                return {
                    "ayah": getattr(scene.ayah, 'number', 0),
                    "surah": getattr(scene.ayah, 'surah', 0),
                    "passed": False,
                    "confidence": 0.0,
                    "concerns": [f"Validation error: {e}"],
                    "method": "error",
                }

        # Validate ayahs in parallel (3 at a time to be polite to APIs)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="tafsir"
        ) as executor:
            futures = [executor.submit(_validate_one, s) for s in script.ayah_scenes]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

        # Sort by ayah number for readable logs
        results.sort(key=lambda r: r.get("ayah", 0))
        return results

    # ─── v18: Per-emotion color grade resolver ──────────────────
    def _color_grade_for(self, emotion: str) -> Optional[str]:
        """v18: Returns the FFmpeg color grade filter for the given emotion.

        Falls back to default grade if emotion not in mapping.
        Returns None if color grading is disabled.
        """
        if not self.enable_color_grade:
            return None
        return self.color_grades_by_emotion.get(
            emotion,
            self.color_grades_by_emotion.get(
                "warm",
                self.video_cfg.color_grade_default
            ),
        )

    # ─── v14: Scene rendering ────────────────────────────────────
    def _render_all_scenes_v14(
        self,
        script: EpisodeScript,
        audio_map: Dict[str, str],
        ep_dir: Path,
    ) -> List[str]:
        """
        v14 scene rendering: each ayah now produces up to 6 segments.
        Segment order: hook → intro → story → [ayah recitation] → explain → moral
        """
        scenes_dir = ep_dir / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        # ── Intro narrator
        if "intro" in audio_map:
            out = str(scenes_dir / "00_intro.mp4")
            intro_bg = script.intro_scene.image_path  # v16
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
                    "color_grade": self._color_grade_for("excited"),  # v18
                },
            ), audio_map["intro"])
            outputs.append(out)

        # ── Per-ayah: 6-segment cinematic structure
        for i, scene in enumerate(script.ayah_scenes):
            sid = f"ayah_{scene.scene_id}"
            pfx = f"{i + 1:02d}"
            scene_type = scene.visual_scene.value
            palette = scene.palette.value
            emotion = scene.scene_emotion.value if hasattr(scene.scene_emotion, 'value') else str(scene.scene_emotion)
            kw = scene.keywords
            ayah_bg = scene.image_path  # v16

            # 1. Hook (NEW v14)
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
                        "color_grade": self._color_grade_for("playful"),  # v18
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
                        "color_grade": self._color_grade_for(emotion),  # v18
                    },
                ), audio_map[f"{sid}_intro"])
                outputs.append(out)

            # 3. Analogy (was "story" pre-v16, now hold the analogy_text content)
            if f"{sid}_story" in audio_map and scene.story_text:
                out = str(scenes_dir / f"{pfx}c_{sid}_analogy.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type,
                    palette=palette,
                    text=scene.story_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "story",
                        "scene_emotion": "warm",
                        "background_image": ayah_bg,
                        "color_grade": self._color_grade_for("warm"),  # v18
                    },
                ), audio_map[f"{sid}_story"])
                outputs.append(out)

            # 4. Quran recitation (gold typography)
            # v16: NO background image during recitation — pure focused visual
            if f"{sid}_ayah" in audio_map:
                out = str(scenes_dir / f"{pfx}d_{sid}_ayah.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type="mosque" if scene_type in ("mosque", "sky", "starry_night")
                               else scene_type,
                    palette="golden_hour",
                    text=scene.ayah.text, is_ayah=True, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "ayah",
                        "scene_emotion": "reverent",
                        "color_grade": self._color_grade_for("reverent"),  # v18
                    },
                ), audio_map[f"{sid}_ayah"])
                outputs.append(out)

            # 5. Explain text
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
                        "color_grade": self._color_grade_for(emotion),  # v18
                    },
                ), audio_map[f"{sid}_explain"])
                outputs.append(out)

            # 6. Moral / Takeaway
            if f"{sid}_moral" in audio_map and scene.moral_text:
                out = str(scenes_dir / f"{pfx}f_{sid}_moral.mp4")
                self.visual_renderer.render(SceneRenderRequest(
                    scene_type=scene_type,
                    palette="golden_hour",
                    text=scene.moral_text, is_ayah=False, keywords=kw,
                    output_path=out,
                    extra={
                        "text_style": "moral",
                        "scene_emotion": "peaceful",
                        "background_image": ayah_bg,
                        "color_grade": self._color_grade_for("peaceful"),  # v18
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
            outro_bg = script.outro_scene.image_path  # v16
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
                    "color_grade": self._color_grade_for("peaceful"),  # v18
                },
            ), audio_map["outro"])
            outputs.append(out)

        if not outputs:
            raise PipelineError("No scenes rendered", stage="render_scenes")

        logger.info(f"✅ Rendered {len(outputs)} cinematic segments")
        return outputs

    # ─── Concat (with optional crossfades) ───────────────────────
    def _concat_scenes(self, segments: List[str], output_path: str) -> str:
        if self.enable_crossfades and len(segments) <= 20:
            return self.bgm_mixer.concat_with_crossfades(
                segments, output_path,
                transition_duration=0.4,
                transition_type="fade",
                assembler=self.assembler,
            )
        return self.assembler.concat(segments, output_path, re_encode=False)

    # ─── Stage runner ────────────────────────────────────────────
    def _run_stage(self, name, fn, report, *, idem_key=None) -> Any:
        registry = get_registry()
        if idem_key is not None and self._checkpoints.is_completed(idem_key, name):
            cached = self._checkpoints.get_output(idem_key, name)
            logger.info(f"⏭️  stage '{name}' skipped (replay)")
            report.stages.append(StageResult(name=name, success=True, duration_sec=0.0, detail="skipped"))
            registry.counter("pipeline.stage.skipped").inc(labels={"stage": name})
            return cached

        with self._emitter.span(f"stage.{name}", stage=name) as span:
            t = time.monotonic()
            try:
                result = fn()
                dur = time.monotonic() - t
                report.stages.append(StageResult(name=name, success=True, duration_sec=dur))
                registry.histogram("pipeline.stage.duration_ms").record(dur * 1000, labels={"stage": name, "outcome": "success"})
                registry.counter("pipeline.stage.success").inc(labels={"stage": name})
                if idem_key is not None and isinstance(result, dict):
                    self._checkpoints.record(idem_key, stage=name, duration_ms=int(dur * 1000), output=result)
                span.set("duration_ms", int(dur * 1000))
                return result
            except Exception as e:
                dur = time.monotonic() - t
                report.stages.append(StageResult(name=name, success=False, duration_sec=dur, detail=type(e).__name__))
                registry.histogram("pipeline.stage.duration_ms").record(dur * 1000, labels={"stage": name, "outcome": "failure"})
                registry.counter("pipeline.stage.failure").inc(labels={"stage": name, "error_type": type(e).__name__})
                raise

    def _mark_failure(self, report, episode_id, status, error_str) -> None:
        report.final_status = status
        report.error = error_str
        try:
            self.repository.update_status(episode_id, status)
        except Exception as e:
            logger.warning(f"⚠️ DB status update failed: {e}")

    def _safe_cleanup(self, ep_dir, branded, raw_video, scene_segments) -> None:
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
