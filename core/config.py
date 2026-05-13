"""
core/config.py
====================================================================
Centralized configuration for QEEMA v2.

All settings come from one of three sources, in priority order:
  1. Environment variables (set in GitHub Secrets or .env)
  2. Hard-coded defaults in this file
  3. None — and we raise a clear error if a required value is missing

Design principle: ALL paths and ENV reads happen here, nowhere else.
Other modules import config and read attributes — they never touch
os.environ or Path directly. This makes the whole system testable and
debuggable from one place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ════════════════════════════════════════════════════════════════════
# Project paths
# ════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).parent.parent.resolve()

# Static assets that ship with the code (committed to git)
ASSETS_DIR = ROOT / "assets"
LOGO_PATH = ASSETS_DIR / "logo.png"
OUTRO_VIDEO_PATH = ASSETS_DIR / "outro.mp4"  # channel outro animation (appended to every episode)
FONT_PATH = ASSETS_DIR / "Amiri-Bold.ttf"
BGM_PATH = ASSETS_DIR / "bgm.mp3"  # optional, soft background music

# Runtime directories (created on first use, not committed)
STATE_DIR = ROOT / "state"          # JSON state files
LOGS_DIR = ROOT / "logs"            # log files
TEMP_DIR = ROOT / "temp"            # ephemeral artifacts
EPISODES_DIR = STATE_DIR / "episodes"  # one folder per episode

# Cache directories for the pipeline
TILAWAH_CACHE_DIR = TEMP_DIR / "tilawah"          # downloaded Quran audio
LEONARDO_CACHE_DIR = TEMP_DIR / "leonardo"        # generated images
ELEVENLABS_CACHE_DIR = TEMP_DIR / "elevenlabs"    # generated speech


def ensure_runtime_dirs() -> None:
    """Create all runtime dirs. Call this once at startup."""
    for d in [
        STATE_DIR, LOGS_DIR, TEMP_DIR, EPISODES_DIR,
        TILAWAH_CACHE_DIR, LEONARDO_CACHE_DIR, ELEVENLABS_CACHE_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════════
# Required env vars (will raise on import if missing)
# ════════════════════════════════════════════════════════════════════

def _require_env(name: str) -> str:
    """Fetch an env var or raise."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"❌ Required environment variable {name!r} is not set. "
            f"Add it to your .env file or GitHub Secrets."
        )
    return value


def _optional_env(name: str, default: str = "") -> str:
    """Fetch an env var, returning `default` if missing OR empty."""
    value = os.environ.get(name, "").strip()
    return value if value else default


# ════════════════════════════════════════════════════════════════════
# API Credentials
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class APIKeys:
    """All third-party credentials in one place."""

    # ── Gemini (Google) ──────────────────────────────────────────
    # We support up to 3 keys for daily quota rotation:
    #   Free tier = 20 calls/day per key.
    #   3 keys = 60 calls/day total (far more than needed).
    gemini_primary: str
    gemini_secondary: Optional[str]
    gemini_tertiary: Optional[str]

    # ── ElevenLabs ───────────────────────────────────────────────
    elevenlabs_key: str

    # ── Leonardo.ai ──────────────────────────────────────────────
    leonardo_key: str

    # ── Supabase (persistent storage) ────────────────────────────
    supabase_url: str
    supabase_key: str

    # ── YouTube ──────────────────────────────────────────────────
    youtube_client_id: str
    youtube_client_secret: str
    youtube_refresh_token: str

    # ── Defaults (must come last in dataclass) ───────────────────
    elevenlabs_voice_id: str = "UR972wNGq3zluze0LoIp"

    @classmethod
    def from_env(cls) -> "APIKeys":
        """Load from environment variables. Raises on missing required keys."""
        return cls(
            # Gemini (at least one required)
            gemini_primary=_require_env("GEMINI_API_KEY"),
            gemini_secondary=_optional_env("GEMINI_API_KEY_2") or None,
            gemini_tertiary=_optional_env("GEMINI_API_KEY_3") or None,

            # ElevenLabs
            elevenlabs_key=_require_env("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_optional_env(
                "ELEVENLABS_VOICE_ID", "UR972wNGq3zluze0LoIp"
            ),

            # Leonardo
            leonardo_key=_require_env("LEONARDO_API_KEY"),

            # Supabase
            supabase_url=_require_env("SUPABASE_URL"),
            supabase_key=_require_env("SUPABASE_KEY"),

            # YouTube
            youtube_client_id=_require_env("YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_require_env("YOUTUBE_CLIENT_SECRET"),
            youtube_refresh_token=_require_env("YOUTUBE_REFRESH_TOKEN"),
        )

    def gemini_keys_list(self) -> List[str]:
        """Return all configured Gemini keys (1-3)."""
        keys = [self.gemini_primary]
        if self.gemini_secondary:
            keys.append(self.gemini_secondary)
        if self.gemini_tertiary:
            keys.append(self.gemini_tertiary)
        return keys


# ════════════════════════════════════════════════════════════════════
# Pipeline behavior config
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PipelineConfig:
    """How the pipeline behaves. Tweak these to tune the channel."""

    # ── ElevenLabs voice settings (B preset per agreement) ───────
    # stability=0.35: more expressive, less monotone (good for storytelling)
    # similarity_boost=0.75: stays true to the voice
    # style=0.65: warmer, more "storyteller" tone
    # speed=1.05: slightly faster, more energetic for kids 6-10
    elevenlabs_stability: float = 0.35
    elevenlabs_similarity: float = 0.75
    elevenlabs_style: float = 0.65
    elevenlabs_speed: float = 1.05
    elevenlabs_model: str = "eleven_multilingual_v2"

    # ── Tilawah (Quran recitation) ───────────────────────────────
    reciter: str = "husary"  # الحصري — slow, clear, kid-friendly
    # Audio CDN base URL (everyayah.com format)
    # We'll fetch: {base}/Husary_64kbps/{surah:03d}{ayah:03d}.mp3
    tilawah_base_url: str = "https://everyayah.com/data/Husary_64kbps"
    # For surah-opening recitation we also fetch the basmala once
    basmala_url: str = "https://everyayah.com/data/Husary_64kbps/001001.mp3"

    # ── Leonardo image generation ────────────────────────────────
    leonardo_model_id: str = "b24e16ff-06e3-43eb-8d33-4416c2d75876"  # Phoenix
    leonardo_width: int = 1280
    leonardo_height: int = 720
    leonardo_num_images: int = 1
    leonardo_guidance: int = 7
    leonardo_alchemy: bool = True
    leonardo_poll_interval_sec: float = 4.0
    leonardo_max_poll_attempts: int = 30

    # ── Video output ─────────────────────────────────────────────
    video_width: int = 1920
    video_height: int = 1080
    video_fps: int = 30
    video_crf: int = 19  # high quality
    video_preset: str = "slow"  # better compression, slower encoding

    # ── Audio mixing ─────────────────────────────────────────────
    # Silence between major segments (hook → intro → tilawah → explain → ...)
    silence_short_ms: int = 500
    silence_medium_ms: int = 800
    # Crossfade between image scenes
    image_crossfade_ms: int = 800
    # Ken Burns zoom amplitude (4-6% as agreed)
    ken_burns_zoom_pct: float = 5.0

    # ── Logo / watermark ─────────────────────────────────────────
    # Logo overlay in the corner of every scene
    logo_overlay_enabled: bool = True
    logo_overlay_width: int = 320  # px on 1920×1080 canvas (was 180 — bigger now!)
    logo_overlay_position: str = "bottom_right"  # bottom_right | top_right
    logo_overlay_opacity: float = 0.85  # slightly more opaque (was 0.75)
    logo_overlay_margin: int = 40  # px from edges (was 30)
    # Larger logo for the intro splash (first 2 seconds)
    logo_intro_width: int = 600  # was 420 — much bigger now
    logo_intro_duration_sec: float = 2.5  # was 2.0 — slightly longer

    # ── BGM (background music) ───────────────────────────────────
    bgm_enabled: bool = True
    bgm_volume_db: float = -22.0  # very quiet under narration
    bgm_volume_db_during_tilawah: float = -60.0  # essentially silent

    # ── Limits & safety ──────────────────────────────────────────
    # Max attempts when calling Gemini/Leonardo/ElevenLabs
    gemini_max_retries: int = 3
    leonardo_max_retries: int = 2
    elevenlabs_max_retries: int = 2
    # Min seconds between Gemini calls per key (4 RPM = 15 sec)
    gemini_min_interval_sec: float = 15.0

    # ── YouTube ──────────────────────────────────────────────────
    youtube_category_id: str = "27"  # Education
    youtube_privacy: str = "public"   # public | unlisted | private
    youtube_default_lang: str = "ar"


# ════════════════════════════════════════════════════════════════════
# Convenience top-level config object (lazy-loaded)
# ════════════════════════════════════════════════════════════════════

_keys_cache: Optional[APIKeys] = None
_pipeline_cache: Optional[PipelineConfig] = None


def get_api_keys() -> APIKeys:
    """Lazy load API keys. Raises if any required env var is missing."""
    global _keys_cache
    if _keys_cache is None:
        _keys_cache = APIKeys.from_env()
    return _keys_cache


def get_pipeline_config() -> PipelineConfig:
    """Pipeline behavior settings (currently no env overrides)."""
    global _pipeline_cache
    if _pipeline_cache is None:
        _pipeline_cache = PipelineConfig()
    return _pipeline_cache


# ════════════════════════════════════════════════════════════════════
# Runtime flags (often set via CLI args, not env)
# ════════════════════════════════════════════════════════════════════

@dataclass
class RuntimeFlags:
    """Per-run flags. Passed from main.py to the orchestrator."""
    dry_run: bool = False              # don't upload to YouTube
    force_regenerate: bool = False     # ignore cache, regenerate everything
    skip_youtube: bool = False         # alias for dry_run
    episode_number: Optional[int] = None  # specific episode to build
    verbose: bool = False
