"""
main.py — VALUE / QEEMA v12.0 (Production)
==================================================
Composition root — wires everything together.

[Responsibilities]
  1. Parse CLI arguments
  2. Load + validate config
  3. Setup logging + observability
  4. Build infrastructure (DB, browser pool, ffmpeg)
  5. Build engines (script, voice, visual, intro/outro, thumbnail)
  6. Build orchestrator with all dependencies
  7. Install signal handlers for graceful shutdown
  8. Run requested operation

[Operations]
  --episode N        : run episode #N
  --next            : run next pending from queue (default)
  --status          : print episode dashboard
  --dry-run         : skip YouTube upload
  --list-voices     : print available TTS providers
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Optional

from core.config import AppConfig
from core.exceptions import ConfigurationError
from core.logging_setup import setup_logging
from core.observability import SpanEmitter, configure_emitter, get_registry
from data.curriculum import total_episodes
from engines.intro_outro_engine import IntroOutroEngine
from engines.quality_validator import ScriptQualityValidator
from engines.script_engine import ScriptEngine
from engines.thumbnail_engine import ThumbnailEngine
from engines.visual_render_engine import ProceduralRenderer
from engines.voice_engine import VoiceEngine
from infrastructure.browser_pool import BrowserPool
from infrastructure.ffmpeg_assembler import FFmpegAssembler
from infrastructure.repository_supabase import SupabaseRepository
from infrastructure.youtube_uploader import YouTubeUploader
from orchestrator import Orchestrator

VERSION: str = "12.0.0"


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
        "--next", action="store_true",
        help="Run next pending episode (default)",
    )
    g.add_argument(
        "--status", action="store_true",
        help="Print episode dashboard and exit",
    )
    g.add_argument(
        "--list-voices", action="store_true",
        help="List configured TTS providers and exit",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Skip YouTube upload (still produces video file)",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return p


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

    # ── Browser pool (shared by visual renderer + intro/outro)
    browser_pool = BrowserPool(
        pool_size=config.procedural.browser_pool_size,
        render_size=(config.video.width, config.video.height),
    )

    # ── Engines
    quality_validator = ScriptQualityValidator()
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

    # Visual renderer manages its own browser pool internally
    visual_renderer = ProceduralRenderer(
        paths=config.paths,
        video_cfg=config.video,
        proc_cfg=config.procedural,
        branding=config.branding,
        assembler=assembler,
    )

    # IntroOutro shares the visual renderer's browser pool to avoid
    # launching two Chromium instances. We expose it via the renderer's
    # internal attribute (clean coupling: same pool object).
    intro_outro = IntroOutroEngine(
        paths=config.paths,
        video_cfg=config.video,
        proc_cfg=config.procedural,
        branding=config.branding,
        assembler=assembler,
        browser_pool=visual_renderer._pool,  # noqa: SLF001  (intentional sharing)
    )

    thumbnail_builder = ThumbnailEngine(
        paths=config.paths,
        branding=config.branding,
    )

    # ── Orchestrator
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
        dry_run=dry_run,
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

    # ── 2b. Setup observability (structured tracing).
    # Spans go to logs/spans.jsonl as one JSON object per line. CI can
    # upload this as an artifact for post-mortem analysis. The file is
    # append-only; clean logs/ between runs if rotation matters.
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
        log.error(f"❌ {e}")
        return 2
    except Exception as e:
        log.error(f"❌ Orchestrator setup failed: {e}")
        log.debug(traceback.format_exc())
        return 3

    # ── 5. Signal handlers
    def _shutdown(signum, _frame):
        log.warning(f"⚠️ Caught signal {signum}; requesting graceful shutdown")
        orchestrator.request_shutdown()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            # Some platforms (Windows, certain containers) don't support all signals
            pass

    # ── 6. Execute
    try:
        orchestrator.warmup()
        if args.episode is not None:
            report = orchestrator.run(args.episode)
        else:
            report = orchestrator.run_next()

        if report is None:
            return 0
        return 0 if report.success else 1
    except KeyboardInterrupt:
        log.warning("⚠️ Interrupted by user")
        return 130
    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        log.debug(traceback.format_exc())
        return 4
    finally:
        try:
            orchestrator.shutdown()
        except Exception as e:
            log.warning(f"⚠️ shutdown error: {e}")

        # Persist metrics snapshot for post-run analysis. Atomic write —
        # if the process is killed mid-write, the previous snapshot
        # stays intact.
        try:
            metrics_path = config.paths.logs / "metrics.json"
            get_registry().write_snapshot(metrics_path)
            log.info(f"📊 Metrics snapshot → {metrics_path}")
        except Exception as e:
            log.warning(f"⚠️ Failed to write metrics snapshot: {e}")


if __name__ == "__main__":
    sys.exit(main())
