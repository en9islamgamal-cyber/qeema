"""
main.py — VALUE / QEEMA v22.5 (UNIFIED PRODUCTION)
==================================================
Composition root — wires everything together.

[v22.5 — Gemini-only architecture]
  ✓ Multi-task script generation (single Gemini call)
  ✓ Per-ayah Gemini tafsir validation (4 RPM throttled)
  ✓ Combined per-scene TTS
  ✓ Quota-aware degradation strategy (auto-select mode)
  ✓ Cost dashboard
  ✓ 3-day phase pipeline (Phase 1 = script + tafsir, Phase 2 = visuals + assets, Phase 3 = render + publish)
  ✓ Local file repository (--skip-supabase flag)

[Architecture decisions]
  - PipelineStrategy is the single source of truth for "which path to take"
  - No more if/else scattered in orchestrator — all decisions queried from strategy
  - Strategy is computed ONCE at episode start, immutable thereafter
  - Quota state captured in strategy snapshot for reproducibility
  - Tafsir validation is mandatory whenever a Gemini key is configured

[v22.5 removals from earlier versions]
  - Claude / Anthropic dependency (credit was zero, won't be funded)
  - Heuristic tafsir fallback (structurally too weak)
  - Batched-tafsir Claude optimization (no Claude → no batching)
  - use_claude_tafsir / use_batched_tafsir strategy fields
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
from typing import Any, Optional

from core.config import AppConfig
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
from infrastructure.ffmpeg_assembler import FFmpegAssembler
from infrastructure.repository_supabase import SupabaseRepository
from infrastructure.youtube_uploader import YouTubeUploader
from orchestrator import Orchestrator

# ════════════════════════════════════════════════════════════════
# Optional engines — graceful degradation if module missing
# ════════════════════════════════════════════════════════════════

# v20.1: Local file-backed repository (no Supabase needed for testing)
try:
    from infrastructure.repository_local import LocalRepository
    _HAS_LOCAL_REPO = True
except ImportError:
    _HAS_LOCAL_REPO = False
    LocalRepository = None  # type: ignore

# v16: Leonardo image engine
try:
    from engines.image_engine import LeonardoImageEngine, LeonardoConfig
    _HAS_IMAGE_ENGINE = True
except ImportError:
    _HAS_IMAGE_ENGINE = False
    LeonardoImageEngine = None  # type: ignore
    LeonardoConfig = None  # type: ignore

# v19: Quota manager (CRITICAL for budget operation)
try:
    from core.quota_manager import QuotaManager, QuotaConfig
    _HAS_QUOTA_MANAGER = True
except ImportError:
    _HAS_QUOTA_MANAGER = False
    QuotaManager = None  # type: ignore
    QuotaConfig = None  # type: ignore

# v21: Pipeline strategy (NEW)
try:
    from core.pipeline_strategy import (
        StrategyFactory, parse_mode, QualityMode,
    )
    _HAS_STRATEGY = True
except ImportError:
    _HAS_STRATEGY = False
    StrategyFactory = None  # type: ignore
    parse_mode = None  # type: ignore
    QualityMode = None  # type: ignore

# v20: Cost dashboard
try:
    from core.cost_dashboard import CostDashboard
    _HAS_DASHBOARD = True
except ImportError:
    _HAS_DASHBOARD = False
    CostDashboard = None  # type: ignore

# v18: Cost tracker
try:
    from core.cost_tracker import CostTracker
    _HAS_COST_TRACKER = True
except ImportError:
    _HAS_COST_TRACKER = False
    CostTracker = None  # type: ignore

# v18: TafsirValidator (CRITICAL for religious accuracy)
try:
    from engines.tafsir_validator import TafsirValidator
    _HAS_TAFSIR_VALIDATOR = True
except ImportError:
    _HAS_TAFSIR_VALIDATOR = False
    TafsirValidator = None  # type: ignore

# v18: Hook optimizer (Thompson Sampling)
try:
    from engines.hook_optimizer import HookOptimizer
    _HAS_HOOK_OPTIMIZER = True
except ImportError:
    _HAS_HOOK_OPTIMIZER = False
    HookOptimizer = None  # type: ignore

# v18: Review gate
try:
    from engines.review_gate import ReviewGate
    _HAS_REVIEW_GATE = True
except ImportError:
    _HAS_REVIEW_GATE = False
    ReviewGate = None  # type: ignore

# v15: Quality scorer
try:
    from engines.quality_score import QualityScorerAdapter as _Q15
    _HAS_V15_QUALITY = True
except ImportError:
    _HAS_V15_QUALITY = False
    _Q15 = None  # type: ignore

# Subtitle engine
try:
    from engines.subtitle_engine import SubtitleEngine
    _HAS_SUBTITLES = True
except ImportError:
    _HAS_SUBTITLES = False
    SubtitleEngine = None  # type: ignore

# Legacy quality validator (fallback)
try:
    from engines.quality_validator import ScriptQualityValidator
    _HAS_LEGACY_QUALITY = True
except ImportError:
    _HAS_LEGACY_QUALITY = False
    ScriptQualityValidator = None  # type: ignore

# v20: Multi-task script engine helpers (functions, not class)
try:
    from engines.script_engine_v20 import (
        build_full_episode_prompt,
        parse_full_episode_response,
        build_visual_prompt_from_scene,
    )
    _HAS_MULTI_TASK = True
except ImportError:
    _HAS_MULTI_TASK = False

VERSION: str = "22.5.0"


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
        "--approve", action="store_true",
        help="v18: bypass review gate (use after manual review of episode)",
    )
    p.add_argument(
        "--review-threshold", type=int, default=10,
        help="v18: episodes ≤ this number need manual review (default: 10)",
    )
    p.add_argument(
        "--mode", choices=["high", "balanced", "economy", "auto"],
        default="auto",
        help="v20: quality mode (default: auto-select based on quota)",
    )
    p.add_argument(
        "--skip-supabase", action="store_true",
        help="v20.1: use local JSON-file repository instead of Supabase "
             "(for testing/dev — no remote DB needed)",
    )
    p.add_argument(
        "--reset-local-state", action="store_true",
        help="v20.1: wipe local repository state before run (dev only). "
             "Has no effect when using Supabase.",
    )
    p.add_argument(
        "--phase", type=int, choices=[1, 2, 3], default=None,
        help="v22.5: run only a specific phase of the 3-day pipeline. "
             "1=planning (script+tafsir+enrich), "
             "2=assets (Leonardo+ElevenLabs), "
             "3=render+upload. "
             "If omitted, auto-detects next pending phase.",
    )
    p.add_argument(
        "--phase-state-dir", type=str, default="state/phases",
        help="v22.5: directory for persistent phase state (default: state/phases)",
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
    """Apply ENV overrides using dataclasses.replace() — preserves all fields."""
    crafting_enabled = os.getenv(
        "QEEMA_CRAFTING",
        str(config.engine.enable_prompt_crafting).lower()
    ).lower() in ("1", "true", "yes")
    ssml_enabled = os.getenv(
        "QEEMA_SSML",
        str(config.engine.add_ssml).lower()
    ).lower() in ("1", "true", "yes")
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
# TafsirValidator builder — v22.5 (Gemini-only)
# ════════════════════════════════════════════════════════════════
def _build_tafsir_validator(
    gemini_review_key: Optional[str] = None,
    cache_path: Optional[Path] = None,
) -> Any:
    """Build a v22.5 TafsirValidator (Gemini-only architecture).

    Returns None if no Gemini key was configured. The validator has only
    one signature now: TafsirValidator(gemini_review_key=..., cache_path=...).

    Args:
        gemini_review_key: Gemini API key for religious validation
        cache_path: Path to persistent JSON cache for tafsir API responses.
                    Strongly recommended — without it, every run hits quran.com
                    fresh for all ayahs (14 HTTP calls per episode).
    """
    if not _HAS_TAFSIR_VALIDATOR:
        return None
    if not gemini_review_key:
        logging.getLogger("main").warning(
            "⚠️ TafsirValidator NOT wired — no Gemini key for tafsir review"
        )
        return None

    log = logging.getLogger("main")
    try:
        v = TafsirValidator(
            gemini_review_key=gemini_review_key,
            cache_path=cache_path,
        )
        log.info("✅ TafsirValidator wired (Gemini-only — v22.5)")
        return v
    except Exception as e:
        log.error(f"❌ TafsirValidator init failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# Composition
# ════════════════════════════════════════════════════════════════
def _build_orchestrator(
    config: AppConfig, *,
    dry_run: bool,
    approval_explicit: bool = False,
    review_threshold: int = 10,
    skip_supabase: bool = False,
    reset_local_state: bool = False,
    requested_mode: str = "auto",
) -> Orchestrator:
    """Wire all dependencies. This is the only place that knows the full graph."""
    log = logging.getLogger("main")

    # ─── 1. Infrastructure ──────────────────────────────────────
    assembler = FFmpegAssembler(config.video)

    # ─── 2. Repository selection ────────────────────────────────
    repository: Any
    if skip_supabase:
        if not _HAS_LOCAL_REPO:
            raise RuntimeError(
                "--skip-supabase requested but LocalRepository module unavailable. "
                "Make sure infrastructure/repository_local.py exists."
            )
        local_state_dir = config.paths.root / "state"
        repository = LocalRepository(state_dir=local_state_dir)
        if reset_local_state:
            repository.reset()
        log.info(
            f"📁 Using LOCAL repository (Supabase skipped): "
            f"{local_state_dir / 'local_episodes.json'}"
        )
        try:
            stats = repository.stats()
            if stats:
                stats_str = ", ".join(f"{s}: {n}" for s, n in stats.items())
                log.info(f"   Current state: {stats_str}")
        except Exception:
            pass  # stats() is optional convenience method
    else:
        if not config.api_keys.supabase_url or not config.api_keys.supabase_key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY required, OR pass --skip-supabase "
                "to use the local file-backed repository instead."
            )
        repository = SupabaseRepository(
            config.api_keys.supabase_url,
            config.api_keys.supabase_key,
        )
        log.info("☁️  Using SUPABASE repository (production mode)")

    # ─── 3. YouTube uploader ─────────────────────────────────────
    uploader: Optional[YouTubeUploader] = None
    if not dry_run:
        if not all([
            config.api_keys.youtube_client_id,
            config.api_keys.youtube_client_secret,
            config.api_keys.youtube_refresh_token,
        ]):
            raise RuntimeError(
                "YouTube credentials required for non-dry-run. "
                "Pass --dry-run to skip upload."
            )
        uploader = YouTubeUploader(
            config.api_keys.youtube_client_id,
            config.api_keys.youtube_client_secret,
            config.api_keys.youtube_refresh_token,
            chunk_size_mb=config.engine.upload_chunk_size_mb,
            max_retries=config.engine.upload_max_retries,
        )

    # ─── 4. Quality validator ────────────────────────────────────
    quality_validator = None
    if _HAS_V15_QUALITY:
        threshold = float(os.getenv("QUALITY_THRESHOLD", "70"))
        quality_validator = _Q15(threshold=threshold)
        log.info(f"✅ Using v17 quality scorer (threshold={threshold})")
    elif _HAS_LEGACY_QUALITY:
        quality_validator = ScriptQualityValidator()
        log.info("⚠️  Falling back to legacy quality validator")

    # ─── 5. Quota manager (must be FIRST so engines can use it) ──
    quota_manager = None
    if _HAS_QUOTA_MANAGER:
        try:
            quota_cfg = getattr(config, 'quota', None) or QuotaConfig()
            quota_manager = QuotaManager(paths=config.paths, config=quota_cfg)
            quota_manager.print_report()
        except Exception as e:
            log.warning(f"⚠️ QuotaManager init failed: {e}")

    # ─── 6. Engines ──────────────────────────────────────────────
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
        quota_manager=quota_manager,
    )

    # Color grade filter (v17: baked into per-scene encoding)
    cg_filter = (
        config.video.color_grade_vf
        if config.engine.enable_color_grade
        else None
    )
    if cg_filter:
        log.info(
            f"🎨 Color grade baked into per-scene encoding: {cg_filter[:60]}..."
        )

    visual_renderer = ProceduralRenderer(
        paths=config.paths,
        video_cfg=config.video,
        proc_cfg=config.procedural,
        branding=config.branding,
        assembler=assembler,
        color_grade_filter=cg_filter,
    )

    intro_outro = IntroOutroEngine(
        paths=config.paths,
        video_cfg=config.video,
        proc_cfg=config.procedural,
        branding=config.branding,
        assembler=assembler,
        browser_pool=visual_renderer._pool,
        color_grade_filter=cg_filter,
    )

    thumbnail_builder = ThumbnailEngine(
        paths=config.paths,
        branding=config.branding,
    )

    bgm_mixer = BGMMixer(
        paths=config.paths,
        bgm_volume=config.engine.bgm_volume,
    )

    # Subtitles (default ON in v18+)
    subtitle_engine = None
    enable_subtitles = os.getenv("ENABLE_SUBTITLES", "true").lower() == "true"
    if enable_subtitles and _HAS_SUBTITLES:
        subtitle_engine = SubtitleEngine(paths=config.paths)
        log.info("✅ Subtitle engine enabled")

    # ─── 7. Leonardo image engine ────────────────────────────────
    image_engine = None
    if (
        _HAS_IMAGE_ENGINE
        and config.api_keys.leonardo
        and config.engine.enable_ai_images
    ):
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
            image_engine = LeonardoImageEngine(
                leo_cfg, quota_manager=quota_manager,
            )
            log.info("✅ Leonardo image engine wired")
        except Exception as e:
            log.warning(
                f"⚠️ Leonardo init failed: {e} — falling back to CSS scenes"
            )
            image_engine = None
    elif not config.api_keys.leonardo:
        log.info(
            "ℹ️ LEONARDO_API_KEY not set — using CSS scenes (no AI images)"
        )

    # ─── 8. TafsirValidator (CRITICAL — religious accuracy) ──────
    # Persistent cache lives at state/tafsir_cache.json — same path the pipeline
    # action restores via actions/cache. Without this, every episode hits
    # quran.com fresh for all 7 ayahs × 2 tafsirs = 14 HTTP calls.
    tafsir_cache_path = config.paths.root / "state" / "tafsir_cache.json"
    tafsir_cache_path.parent.mkdir(parents=True, exist_ok=True)
    tafsir_validator = _build_tafsir_validator(
        gemini_review_key=config.api_keys.tafsir_review_key,
        cache_path=tafsir_cache_path,
    )
    if not tafsir_validator:
        log.warning(
            "⚠️ Religious validation DISABLED — no Gemini key configured. "
            "Set GEMINI_API_KEY for tafsir validation. NOT RECOMMENDED for production."
        )

    # ─── 9. Hook optimizer ───────────────────────────────────────
    hook_optimizer = None
    if _HAS_HOOK_OPTIMIZER:
        try:
            hook_optimizer = HookOptimizer(paths=config.paths)
            log.info("✅ HookOptimizer wired")
        except Exception as e:
            log.warning(f"⚠️ HookOptimizer init failed: {e}")

    # ─── 10. Review gate ─────────────────────────────────────────
    review_gate = None
    if _HAS_REVIEW_GATE:
        try:
            review_gate = ReviewGate(
                review_threshold=review_threshold,
                review_dir=config.paths.root / "review",
            )
            log.info(f"✅ ReviewGate wired (threshold={review_threshold})")
        except Exception as e:
            log.warning(f"⚠️ ReviewGate init failed: {e}")

    # ─── 11. Cost tracker ────────────────────────────────────────
    cost_tracker = None
    if _HAS_COST_TRACKER:
        try:
            cost_tracker = CostTracker(log_dir=config.paths.logs)
            log.info("✅ CostTracker wired")
        except Exception as e:
            log.warning(f"⚠️ CostTracker init failed: {e}")

    # ─── 12. Cost dashboard (v20 — generated each run) ──────────
    cost_dashboard = None
    if _HAS_DASHBOARD:
        try:
            cost_dashboard = CostDashboard(
                paths=config.paths,
                quota_manager=quota_manager,
                cost_tracker=cost_tracker,
            )
            log.info("✅ CostDashboard wired")
        except Exception as e:
            log.warning(f"⚠️ CostDashboard init failed: {e}")

    # ─── 13. Pipeline strategy factory (v21 NEW) ────────────────
    if not _HAS_STRATEGY:
        raise RuntimeError(
            "core/pipeline_strategy.py is REQUIRED in v21+. "
            "Make sure the file exists in the repo."
        )

    parsed_mode = parse_mode(requested_mode)

    # ─── 14. Per-emotion color grades (v18) ─────────────────────
    enable_color_grade = os.getenv("ENABLE_COLOR_GRADE", "true").lower() == "true"
    enable_crossfades = os.getenv("ENABLE_CROSSFADES", "true").lower() == "true"
    try:
        from core.config import COLOR_GRADES_BY_EMOTION
        color_grades = COLOR_GRADES_BY_EMOTION if enable_color_grade else {}
    except ImportError:
        color_grades = {}

    # ─── 15. Build orchestrator with all wired components ───────
    return Orchestrator(
        # Core engines
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
        image_engine=image_engine,
        # v18 extras
        tafsir_validator=tafsir_validator,
        hook_optimizer=hook_optimizer,
        review_gate=review_gate,
        cost_tracker=cost_tracker,
        color_grades_by_emotion=color_grades,
        approval_explicit=approval_explicit,
        # v19 quota
        quota_manager=quota_manager,
        # v20 cost dashboard
        cost_dashboard=cost_dashboard,
        # v21 strategy
        strategy_factory=StrategyFactory,
        requested_mode=parsed_mode,
        has_multi_task_engine=_HAS_MULTI_TASK,
        # Legacy flags
        dry_run=dry_run,
        enable_subtitles=enable_subtitles,
        enable_color_grade=enable_color_grade,
        enable_crossfades=enable_crossfades,
    )


# ════════════════════════════════════════════════════════════════
# Operations
# ════════════════════════════════════════════════════════════════
def _print_status(config: AppConfig, skip_supabase: bool = False) -> int:
    """List episodes from repository (Supabase or local)."""
    repo: Any
    if skip_supabase and _HAS_LOCAL_REPO:
        repo = LocalRepository(state_dir=config.paths.root / "state")
        print(
            f"\n📁 LOCAL repository: "
            f"{config.paths.root / 'state' / 'local_episodes.json'}\n"
        )
    else:
        if not config.api_keys.supabase_url or not config.api_keys.supabase_key:
            print(
                "⚠️ Supabase not configured. "
                "Pass --skip-supabase to use local repo."
            )
            return 1
        repo = SupabaseRepository(
            config.api_keys.supabase_url, config.api_keys.supabase_key,
        )
        print("\n☁️  SUPABASE repository\n")

    episodes = repo.list_episodes()
    print(f"{'EP':<5}{'Status':<22}{'YouTube URL'}")
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
        print(
            f"  ✓ ElevenLabs (voice_id: {config.api_keys.elevenlabs_voice_id})"
        )
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
def _should_use_phase_router() -> bool:
    """Decide whether to use phase router based on env vars.

    Returns True if QEEMA_USE_PHASE_ROUTER=true (set in pipeline.yml for
    the daily 3-day cadence). Defaults to False so non-phase runs use the
    legacy orchestrator.run() path unchanged.
    """
    val = os.environ.get("QEEMA_USE_PHASE_ROUTER", "false").lower()
    return val in ("true", "1", "yes")


def main() -> int:
    args = _build_argparser().parse_args()
    print(_banner())

    # Propagate --skip-supabase to env BEFORE config validation
    if args.skip_supabase:
        os.environ["SKIP_SUPABASE"] = "true"

    # ─── 1. Load + validate config ──────────────────────────────
    project_root = Path(__file__).parent.resolve()
    config = AppConfig.load(project_root)
    config = _apply_env_overrides(config)

    try:
        config.validate()
    except ConfigurationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    # ─── 2. Setup logging + paths ───────────────────────────────
    config.paths.ensure_all()
    setup_logging(config.paths.logs, level=args.log_level)
    log = logging.getLogger("main")
    log.info(f"🟢 QEEMA v{VERSION} starting (root={project_root})")

    # ─── 3. Setup observability ─────────────────────────────────
    spans_path = config.paths.logs / "spans.jsonl"
    configure_emitter(SpanEmitter(jsonl_path=spans_path, also_log=False))
    log.info(f"📊 Observability: spans → {spans_path}")

    # ─── 4. Read-only operations ────────────────────────────────
    if args.status:
        return _print_status(config, skip_supabase=args.skip_supabase)
    if args.list_voices:
        return _print_voices(config)

    # ─── 5. Build orchestrator ──────────────────────────────────
    try:
        orchestrator = _build_orchestrator(
            config,
            dry_run=args.dry_run,
            approval_explicit=args.approve,
            review_threshold=args.review_threshold,
            skip_supabase=args.skip_supabase,
            reset_local_state=args.reset_local_state,
            requested_mode=args.mode,
        )
    except ConfigurationError as e:
        log.error(f"❌ Configuration error: {e}")
        return 2
    except Exception as e:
        log.error(f"❌ Orchestrator build failed: {e}")
        traceback.print_exc()
        return 1

    # ─── 6. Signal handling ─────────────────────────────────────
    def _shutdown_handler(signum: int, frame: Any) -> None:
        log.warning(f"Received signal {signum} — requesting shutdown")
        orchestrator.request_shutdown()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # ─── 7. Run pipeline ────────────────────────────────────────
    exit_code = 1
    try:
        orchestrator.warmup()

        # v22.5: phase-based execution
        if args.phase is not None or _should_use_phase_router():
            # Phase-based execution path
            from core.phase_state import PhaseStateManager
            from core.phase_router import PhaseRouter

            phase_state_dir = Path(args.phase_state_dir)
            state_manager = PhaseStateManager(phase_state_dir)
            router = PhaseRouter(
                orchestrator=orchestrator,
                state_manager=state_manager,
            )

            # Determine episode number
            if args.episode is not None:
                episode_num = args.episode
            else:
                # Auto-pick next pending: ask the orchestrator's repo
                pending = orchestrator.repository.get_pending()
                if pending is None:
                    log.info("📭 No pending episodes — exiting cleanly")
                    return 0
                episode_num = pending["episode_number"]

            phase_arg = args.phase  # None = auto-detect
            log.info(
                f"🎬 Phase router: episode={episode_num} "
                f"phase={phase_arg if phase_arg else 'auto'}"
            )

            phase_result = router.run_phase(
                episode_number=episode_num, phase=phase_arg,
            )

            if phase_result.success:
                log.info(
                    f"✅ Phase {phase_result.phase} complete for episode "
                    f"{episode_num} ({phase_result.duration_sec:.1f}s)"
                )
                if phase_result.next_phase:
                    log.info(
                        f"⏭️ Next phase: {phase_result.next_phase} "
                        f"(will run on next scheduled day)"
                    )
                else:
                    log.info(f"🎉 Episode {episode_num} fully completed!")
                exit_code = 0
            else:
                log.error(
                    f"❌ Phase {phase_result.phase} failed for episode "
                    f"{episode_num}: {phase_result.error}"
                )
                exit_code = 1

        elif args.episode is not None:
            report = orchestrator.run(args.episode)
            print("\n" + report.summary())
            exit_code = 0 if report.success else 1
        else:
            report = orchestrator.run_next()
            if report is None:
                log.info("📭 No pending episodes — exiting cleanly")
                return 0
            print("\n" + report.summary())
            exit_code = 0 if report.success else 1

        # Snapshot metrics
        metrics_path = config.paths.logs / "metrics.json"
        try:
            registry = get_registry()
            registry.write_snapshot(metrics_path)
            log.info(f"📊 Metrics snapshot → {metrics_path}")
        except Exception as e:
            log.warning(f"⚠️ Metrics snapshot failed: {e}")

    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            orchestrator.shutdown()
        except Exception as e:
            log.warning(f"⚠️ Shutdown error: {e}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
