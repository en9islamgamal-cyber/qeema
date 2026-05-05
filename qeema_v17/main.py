"""
main.py — VALUE / QEEMA v17.0 (REVOLUTION)
==================================================
Composition root — wires everything together.

[Revolution v17 — full rewrite of post-render pipeline]
  - REMOVED: separate color_grade stage (was 600s timeout)
  - REVOLUTION: color grade baked into per-scene encoding
  - REVOLUTION: stream-copy wrap_branded (5s vs 15min)
  - FIXED: Leonardo width 1536 (was 1920 → API rejection)
  - FIXED: browser pool 1→3 (parallel rendering -66% time)
  - FIXED: ElevenLabs concurrency 4→3 (no more 429 on first call)
  - FIXED: MetricsRegistry.dump_to_file → safe attr check
  - FIXED: All v15 cosmetic log strings → v17

[Pipeline timing — v17 vs v16]
                    v16             v17
  script:           19s             19s
  ai_images:        ~50s (failed)   ~25s (success)
  audio:            42s             42s
  audio_master:     26s             26s
  render_scenes:    1691s (28min)   ~600s (10min)  ← pool=3
  concat_raw:       4s              4s
  bgm_mix:          0s              0s
  color_grade:      600s (TIMEOUT)  REMOVED        ← baked into render
  wrap_branded:     900s (TIMEOUT)  ~5s            ← stream-copy
  TOTAL:            3312s+ FAIL     ~750s SUCCESS
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Optional

from core.config import AppConfig, EngineConfig
from core.exceptions import ConfigurationError
from core.logging_setup import setup_logging
from core.observability import SpanEmitter, configure_emitter, get_registry
from data.curriculum import total_episodes
from engines.intro_outro_engine import IntroOutroEngine
from engines.script_engine import ScriptEngine
from engines.thumbnail_engine import ThumbnailEngine
from engines.visual_render_engine import ProceduralRenderer
from engines.voice_engine import VoiceEngine
from infrastructure.bgm_mixer import BGMMixer
from infrastructure.browser_pool import BrowserPool
from infrastructure.ffmpeg_assembler import FFmpegAssembler
from infrastructure.repository_supabase import SupabaseRepository
from infrastructure.youtube_uploader import YouTubeUploader
from orchestrator import Orchestrator

# v16: Leonardo image engine (optional)
try:
    from engines.image_engine import LeonardoImageEngine, LeonardoConfig
    _HAS_IMAGE_ENGINE = True
except ImportError:
    _HAS_IMAGE_ENGINE = False
    LeonardoImageEngine = None
    LeonardoConfig = None

# v15: prefer new heuristic-based quality scorer; fall back to original
try:
    from engines.quality_score import QualityScorerAdapter as _Q15
    _HAS_V15_QUALITY = True
except ImportError:
    _HAS_V15_QUALITY = False

# Subtitle engine optional — only loaded if subtitles enabled
try:
    from engines.subtitle_engine import SubtitleEngine
    _HAS_SUBTITLES = True
except ImportError:
    SubtitleEngine = None
    _HAS_SUBTITLES = False

# Original quality validator (fallback)
try:
    from engines.quality_validator import ScriptQualityValidator
    _HAS_LEGACY_QUALITY = True
except ImportError:
    _HAS_LEGACY_QUALITY = False

VERSION: str = "17.0.0"


# ════════════════════════════════════════════════════════════════
# Banner
# ════════════════════════════════════════════════════════════════
def _banner() -> str:
    return f"""\
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║       قِيمَة  ·  VALUE                                              ║
║       Quranic Children's Content Pipeline                        ║
║                                                                  ║
║       Version {VERSION:<12}     Episodes: {total_episodes():>3}                   ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════
def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qeema",
        description="QEEMA / VALUE — Quranic content generation pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--episode", "-e", type=int, metavar="N",
        help="Run a specific episode number",
    )
    g.add_argument(
        "--status", action="store_true",
        help="Print episode status table and exit",
    )
    g.add_argument(
        "--list-voices", action="store_true",
        help="Print configured TTS providers and exit",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Skip YouTube upload (still generates the video)",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return p


# ════════════════════════════════════════════════════════════════
# Config helpers
# ════════════════════════════════════════════════════════════════
def _apply_env_overrides(config: AppConfig) -> AppConfig:
    """
    v15 fix: Apply ENV overrides using dataclasses.replace() to preserve
    all fields. The previous implementation re-instantiated EngineConfig
    by listing fields manually and silently dropped v15-added fields
    (enable_bgm, enable_crossfades, etc.) when QEEMA_CRAFTING was set.
    """
    # Legacy ENV vars (backward compat)
    crafting_enabled = os.getenv(
        "QEEMA_CRAFTING",
        str(config.engine.enable_prompt_crafting).lower()
    ).lower() in ("1", "true", "yes")
    ssml_enabled = os.getenv(
        "QEEMA_SSML",
        str(config.engine.add_ssml).lower()
    ).lower() in ("1", "true", "yes")

    # v15 NEW ENV vars (read but don't override defaults unless set)
    bgm_enabled = os.getenv("ENABLE_BGM", str(config.engine.enable_bgm).lower())
    bgm_enabled_bool = bgm_enabled.lower() in ("1", "true", "yes")

    new_engine = replace(
        config.engine,
        enable_prompt_crafting=crafting_enabled,
        add_ssml=ssml_enabled,
        enable_bgm=bgm_enabled_bool,
    )
    object.__setattr__(config, 'engine', new_engine)
    return config


# ════════════════════════════════════════════════════════════════
# Composition
# ════════════════════════════════════════════════════════════════
def _build_orchestrator(
    config: AppConfig, *, dry_run: bool,
) -> Orchestrator:
    """Wire all dependencies. This is the only place that knows the full graph."""

    # ── Infrastructure
    assembler = FFmpegAssembler(config.video)
    repository = SupabaseRepository(
        config.api_keys.supabase_url, config.api_keys.supabase_key,
    )

    uploader: Optional[YouTubeUploader] = None
    if not dry_run:
        uploader = YouTubeUploader(
            config.api_keys.youtube_client_id,
            config.api_keys.youtube_client_secret,
            config.api_keys.youtube_refresh_token,
            chunk_size_mb=config.engine.upload_chunk_size_mb,
            max_retries=config.engine.upload_max_retries,
        )

    # ── Quality validator: prefer v15 heuristic scorer; fall back to legacy
    quality_validator = None
    if _HAS_V15_QUALITY:
        threshold = float(os.getenv("QUALITY_THRESHOLD", "70"))
        quality_validator = _Q15(threshold=threshold)
        logging.getLogger("main").info(
            f"✅ Using v17 quality scorer (threshold={threshold})"
        )
    elif _HAS_LEGACY_QUALITY:
        quality_validator = ScriptQualityValidator()
        logging.getLogger("main").info("⚠️ Falling back to legacy quality validator")

    # ── Engines
    script_engine = ScriptEngine(
        api_keys=config.api_keys,
        paths=config.paths,
        engine_cfg=config.engine,
        quality_validator=quality_validator,
    )
    voice_engine = VoiceEngine(
        api_keys=config.api_keys,
        paths=config.paths,
        audio_cfg=config.audio,
        engine_cfg=config.engine,
    )

    # v17: Resolve color grade filter once.
    # If enable_color_grade is False, pass None → no inline filter applied.
    cg_filter = config.video.color_grade_vf if config.engine.enable_color_grade else None
    if cg_filter:
        logging.getLogger("main").info(
            f"🎨 v17 Color grade baked into per-scene encoding: {cg_filter[:60]}..."
        )

    # Visual renderer manages its own browser pool internally
    visual_renderer = ProceduralRenderer(
        paths=config.paths,
        video_cfg=config.video,
        proc_cfg=config.procedural,
        branding=config.branding,
        assembler=assembler,
        color_grade_filter=cg_filter,  # v17 inline color grade
    )

    # IntroOutro shares the visual renderer's browser pool
    # v17: same color grade filter ensures intro/outro/body have IDENTICAL
    # codec params → wrap_branded uses stream-copy concat (5 sec vs 15 min)
    intro_outro = IntroOutroEngine(
        paths=config.paths,
        video_cfg=config.video,
        proc_cfg=config.procedural,
        branding=config.branding,
        assembler=assembler,
        browser_pool=visual_renderer._pool,  # noqa: SLF001
        color_grade_filter=cg_filter,  # v17
    )

    thumbnail_builder = ThumbnailEngine(
        paths=config.paths,
        branding=config.branding,
    )

    # ── v17: BGM mixer kept (still needed for BGM and subtitles)
    # apply_color_grade method is now dead code (kept for backward compat)
    bgm_mixer = BGMMixer(
        paths=config.paths,
        bgm_volume=config.engine.bgm_volume,
    )

    subtitle_engine = None
    enable_subtitles = os.getenv("ENABLE_SUBTITLES", "false").lower() == "true"
    if enable_subtitles and _HAS_SUBTITLES and SubtitleEngine is not None:
        subtitle_engine = SubtitleEngine(paths=config.paths)
        logging.getLogger("main").info("✅ Subtitle engine enabled")

    # v15: Read cinematic feature flags from ENV (with sane defaults)
    enable_color_grade = os.getenv("ENABLE_COLOR_GRADE", "true").lower() == "true"
    enable_crossfades = os.getenv("ENABLE_CROSSFADES", "true").lower() == "true"

    # ── v16: Leonardo image engine (paid plan)
    image_engine = None
    if _HAS_IMAGE_ENGINE and config.api_keys.leonardo and config.engine.enable_ai_images:
        try:
            leo_cfg = LeonardoConfig(
                api_key=config.api_keys.leonardo,
                cache_dir=config.paths.image_cache,
                hero_model_id=config.image_gen.hero_model_id,
                scene_model_id=config.image_gen.scene_model_id,
                width=config.image_gen.width,
                height=config.image_gen.height,
                num_images=config.image_gen.num_images,
                guidance_scale=config.image_gen.guidance_scale,
                enable_alchemy=config.image_gen.enable_alchemy,
                enable_high_resolution=config.image_gen.enable_high_resolution,
                poll_interval_sec=config.image_gen.poll_interval_sec,
                max_poll_attempts=config.image_gen.max_poll_attempts,
                character_ref_id=config.api_keys.leonardo_character_ref or None,
                init_strength=config.image_gen.character_ref_strength,
            )
            image_engine = LeonardoImageEngine(leo_cfg)
            logging.getLogger("main").info("✅ Leonardo image engine wired")
        except Exception as e:
            logging.getLogger("main").warning(
                f"⚠️ Leonardo init failed: {e} — falling back to CSS scenes"
            )
            image_engine = None
    elif not config.api_keys.leonardo:
        logging.getLogger("main").info(
            "ℹ️ LEONARDO_API_KEY not set — using CSS scenes (no AI images)"
        )

    # ── Orchestrator (v16: wire AI images)
    return Orchestrator(
        script_engine=script_engine,
        voice_engine=voice_engine,
        visual_renderer=visual_renderer,
        assembler=assembler,
        repository=repository,
        uploader=uploader,
        intro_outro=intro_outro,
        thumbnail_builder=thumbnail_builder,
        quality_validator=quality_validator,
        paths=config.paths,
        video_cfg=config.video,
        bgm_mixer=bgm_mixer,
        subtitle_engine=subtitle_engine,
        image_engine=image_engine,  # v16
        dry_run=dry_run,
        enable_subtitles=enable_subtitles,
        enable_color_grade=enable_color_grade,
        enable_crossfades=enable_crossfades,
    )


# ════════════════════════════════════════════════════════════════
# Operations
# ════════════════════════════════════════════════════════════════
def _print_status(config: AppConfig) -> int:
    repo = SupabaseRepository(
        config.api_keys.supabase_url, config.api_keys.supabase_key,
    )
    episodes = repo.list_episodes()
    print(f"\n{'EP':<5}{'Status':<22}{'YouTube URL'}")
    print("-" * 70)
    for ep in episodes:
        n = ep.get("episode_number", "?")
        s = ep.get("status", "unknown")
        u = ep.get("youtube_url", "") or ""
        print(f"{n:<5}{s:<22}{u}")
    print(f"\nTotal: {len(episodes)} episode(s)")
    return 0


def _print_voices(config: AppConfig) -> int:
    print("\nConfigured TTS providers:")
    if config.api_keys.elevenlabs:
        print(f"  ✓ ElevenLabs (voice_id: {config.api_keys.elevenlabs_voice_id})")
        print(f"     stability={config.audio.elevenlabs_stability}")
        print(f"     similarity={config.audio.elevenlabs_similarity}")
        print(f"     style={config.audio.elevenlabs_style}")
        print(f"     speed={config.audio.elevenlabs_speed}")
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        print(f"  ✓ Google TTS (voice: {config.audio.google_voice})")
    print("\nLLM providers:")
    for i, _ in enumerate(config.api_keys.gemini_keys, 1):
        print(f"  ✓ Gemini #{i}")
    if config.api_keys.groq:
        print("  ✓ Groq (Llama 3.3)")
    print()
    return 0


# ════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════
def main() -> int:
    args = _build_argparser().parse_args()
    print(_banner())

    # ── 1. Load config
    project_root = Path(__file__).parent.resolve()
    config = AppConfig.load(project_root)

    # ── 1b. Apply ENV overrides (v15: uses replace() — preserves all fields)
    config = _apply_env_overrides(config)

    try:
        config.validate()
    except ConfigurationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    # ── 2. Setup logging + paths
    config.paths.ensure_all()
    setup_logging(config.paths.logs, level=args.log_level)
    log = logging.getLogger("main")
    log.info(f"🟢 QEEMA v{VERSION} starting (root={project_root})")

    # ── 2b. Setup observability
    spans_path = config.paths.logs / "spans.jsonl"
    configure_emitter(SpanEmitter(jsonl_path=spans_path, also_log=False))
    log.info(f"📊 Observability: spans → {spans_path}")

    # ── 3. Read-only ops (no orchestrator needed)
    if args.status:
        return _print_status(config)
    if args.list_voices:
        return _print_voices(config)

    # ── 4. Build orchestrator
    try:
        orchestrator = _build_orchestrator(config, dry_run=args.dry_run)
    except ConfigurationError as e:
        log.error(f"❌ Configuration error: {e}")
        return 2
    except Exception as e:
        log.error(f"❌ Orchestrator build failed: {e}")
        traceback.print_exc()
        return 1

    # ── 5. Signal handling for graceful shutdown
    def _shutdown_handler(signum, frame):
        log.warning(f"Received signal {signum} — requesting shutdown")
        orchestrator.request_shutdown()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # ── 6. Run
    try:
        orchestrator.warmup()

        if args.episode is not None:
            report = orchestrator.run(args.episode)
        else:
            report = orchestrator.run_next()
            if report is None:
                log.info("📭 No pending episodes — exiting cleanly")
                return 0

        print("\n" + report.summary())

        # Snapshot metrics for observability
        metrics_path = config.paths.logs / "metrics.json"
        try:
            registry = get_registry()
            registry.write_snapshot(metrics_path)  # v17 fix: was dump_to_file
            log.info(f"📊 Metrics snapshot → {metrics_path}")
        except Exception as e:
            log.warning(f"⚠️ Metrics snapshot failed: {e}")

        return 0 if report.success else 1

    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        traceback.print_exc()
        return 1
    finally:
        try:
            orchestrator.shutdown()
        except Exception as e:
            log.warning(f"⚠️ Shutdown error: {e}")


if __name__ == "__main__":
    sys.exit(main())
