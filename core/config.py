"""
core/config.py — VALUE / QEEMA v11.0 (Production)
=====================================================
Centralized configuration with explicit validation.

[Design Goals]
1. Fail fast: invalid config caught at startup, not deep in pipeline
2. Single source of truth: all knobs live here
3. Environment-aware: prod vs test settings via env vars
4. Type-safe: dataclasses with explicit types
5. No hidden state: dataclasses are frozen where possible
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple


# ════════════════════════════════════════════════════════════════
# 1. API Keys (read once at startup)
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class APIKeysConfig:
    """All API credentials. Read from environment."""
    gemini_keys: Tuple[str, ...]                # supports multiple keys
    groq: str
    cohere: str
    anthropic: str
    elevenlabs: str
    elevenlabs_voice_id: str
    leonardo: str
    supabase_url: str
    supabase_key: str
    youtube_client_id: str
    youtube_client_secret: str
    youtube_refresh_token: str

    @classmethod
    def from_env(cls) -> "APIKeysConfig":
        gemini_keys = tuple(
            v for v in (
                os.getenv("GEMINI_API_KEY", ""),
                os.getenv("GEMINI_API_KEY_2", ""),
                os.getenv("GEMINI_API_KEY_3", ""),
            ) if v
        )
        return cls(
            gemini_keys=gemini_keys,
            groq=os.getenv("GROQ_API_KEY", ""),
            cohere=os.getenv("COHERE_API_KEY", ""),
            anthropic=os.getenv("ANTHROPIC_API_KEY", ""),
            elevenlabs=os.getenv("ELEVENLABS_API_KEY", ""),
            elevenlabs_voice_id=os.getenv(
                "ELEVENLABS_VOICE_ID", "UR972wNGq3zluze0LoIp"
            ),
            leonardo=os.getenv("LEONARDO_API_KEY", ""),
            supabase_url=os.getenv("SUPABASE_URL", ""),
            supabase_key=os.getenv("SUPABASE_KEY", ""),
            youtube_client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
            youtube_client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
            youtube_refresh_token=os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
        )

    def validate(self) -> List[str]:
        """Return list of missing-but-required keys."""
        missing: List[str] = []
        if not self.supabase_url:
            missing.append("SUPABASE_URL")
        if not self.supabase_key:
            missing.append("SUPABASE_KEY")
        if not self.gemini_keys and not self.groq:
            missing.append("GEMINI_API_KEY or GROQ_API_KEY")
        if not self.elevenlabs and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            missing.append("ELEVENLABS_API_KEY or GCP service account")
        # YouTube only required if not in DRY_RUN
        if os.getenv("DRY_RUN", "false").lower() != "true":
            if not self.youtube_client_id:
                missing.append("YOUTUBE_CLIENT_ID")
            if not self.youtube_client_secret:
                missing.append("YOUTUBE_CLIENT_SECRET")
            if not self.youtube_refresh_token:
                missing.append("YOUTUBE_REFRESH_TOKEN")
        return missing


# ════════════════════════════════════════════════════════════════
# 2. Filesystem paths
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PathsConfig:
    """All filesystem paths used by the pipeline."""
    root: Path
    logs: Path
    temp: Path
    assets: Path
    output: Path
    temp_episodes: Path
    tts_cache: Path
    quran_cache: Path
    web_renders: Path
    html_templates: Path
    scene_cache: Path
    videos: Path
    thumbnails: Path
    fonts: Path
    overlays: Path
    branding: Path
    logo_primary: Path
    amiri_font: Path
    intro_video: Path
    outro_video: Path
    bgm_file: Path

    @classmethod
    def from_root(cls, root: Path) -> "PathsConfig":
        assets = root / "assets"
        temp = root / "temp"
        output = root / "output"
        return cls(
            root=root,
            logs=root / "logs",
            temp=temp,
            assets=assets,
            output=output,
            temp_episodes=temp / "episodes",
            tts_cache=temp / "tts_cache",
            quran_cache=assets / "quran_audio",
            web_renders=temp / "web_renders",
            html_templates=temp / "html_templates",
            scene_cache=temp / "scene_cache",
            videos=output / "videos",
            thumbnails=assets / "thumbnails",
            fonts=assets / "fonts",
            overlays=assets / "overlays",
            branding=assets / "branding",
            logo_primary=assets / "logo.png",
            amiri_font=assets / "fonts" / "Amiri-Bold.ttf",
            intro_video=assets / "branding" / "intro.mp4",
            outro_video=assets / "branding" / "outro.mp4",
            bgm_file=assets / "overlays" / "bgm.mp3",
        )

    def ensure_all(self) -> None:
        """Create all directories that should exist."""
        for d in (
            self.logs, self.temp, self.assets, self.output,
            self.temp_episodes, self.tts_cache, self.quran_cache,
            self.web_renders, self.html_templates, self.scene_cache,
            self.videos, self.thumbnails, self.fonts, self.overlays,
            self.branding,
        ):
            d.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════
# 3. Video / encoding settings
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class VideoConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 60
    codec: str = "libx264"
    profile: str = "high"
    crf: int = 17
    preset: str = "slow"
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "256k"


# ════════════════════════════════════════════════════════════════
# 4. Audio settings
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AudioConfig:
    # ElevenLabs
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_stability: float = 0.50
    elevenlabs_similarity: float = 0.85
    elevenlabs_style: float = 0.50
    elevenlabs_speaker_boost: bool = True
    # Google fallback
    google_voice: str = "ar-XA-Wavenet-B"
    google_speaking_rate: float = 0.95
    google_pitch: float = -1.0
    # Mastering
    target_lufs: float = -16.0
    true_peak: float = -1.5
    # Mixing
    quran_start_delay_ms: int = 600
    quran_fade_in_sec: float = 1.0
    quran_end_padding_sec: float = 1.5


# ════════════════════════════════════════════════════════════════
# 5. Branding
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class BrandingConfig:
    channel_name_ar: str = "قِيمَة"
    channel_name_en: str = "VALUE"
    channel_tagline_ar: str = "قصص تربوية من نور القرآن"
    subscribe_text: str = "اشترك في القناة"
    intro_duration_sec: float = 5.0
    outro_duration_sec: float = 5.0


# ════════════════════════════════════════════════════════════════
# 6. Procedural rendering
# ════════════════════════════════════════════════════════════════
PALETTES: Dict[str, List[str]] = {
    "warm_sunset": ["#FFB347", "#FFCC70", "#FFE5B4", "#FF6B6B", "#FFA07A"],
    "calm_blue":   ["#7FB3D5", "#A9CCE3", "#D4E6F1", "#85C1E2", "#5DADE2"],
    "lush_green":  ["#52BE80", "#82E0AA", "#ABEBC6", "#239B56", "#7DCEA0"],
    "night_stars": ["#1B2631", "#283747", "#34495E", "#FFD700", "#F5B041"],
    "golden_hour": ["#D4AF37", "#F39C12", "#F5B041", "#FAD7A0", "#F8C471"],
}


@dataclass(frozen=True)
class ProceduralConfig:
    palettes: Dict[str, List[str]] = field(default_factory=lambda: dict(PALETTES))
    particle_count: int = 80
    ken_burns_speed: float = 0.05
    word_transition_enabled: bool = True
    browser_pool_size: int = 1
    render_warmup_ms: int = 2000


# ════════════════════════════════════════════════════════════════
# 7. Engine tuning (concurrency, retries)
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EngineConfig:
    # ScriptEngine
    script_max_ayah_attempts: int = 3
    # VoiceEngine
    voice_parallel_workers: int = 4
    voice_enable_cache: bool = True
    # VisualRenderer
    render_scene_timeout_sec: int = 120
    # Uploader
    upload_chunk_size_mb: int = 5
    upload_max_retries: int = 5


# ════════════════════════════════════════════════════════════════
# 8. Master config
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AppConfig:
    """Aggregates all sub-configs. Single object passed throughout pipeline."""
    api_keys: APIKeysConfig
    paths: PathsConfig
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    branding: BrandingConfig = field(default_factory=BrandingConfig)
    procedural: ProceduralConfig = field(default_factory=ProceduralConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)

    @classmethod
    def load(cls, root: Path) -> "AppConfig":
        return cls(
            api_keys=APIKeysConfig.from_env(),
            paths=PathsConfig.from_root(root),
        )

    def validate(self) -> None:
        """Raise ConfigurationError if invalid."""
        from core.exceptions import ConfigurationError
        missing = self.api_keys.validate()
        if missing:
            raise ConfigurationError(
                f"Missing required configuration: {', '.join(missing)}"
            )
