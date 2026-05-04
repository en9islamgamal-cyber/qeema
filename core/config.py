"""
core/config.py — VALUE / QEEMA v16.0
=====================================================
[v16 additions]
- ImageGenConfig: Leonardo.ai paid plan settings
- AudioConfig.elevenlabs_adaptive: enable per-emotion voice overrides
- BrandingConfig.channel_tagline_ar: changed (no more grandfather)
- AppConfig.image_gen: optional Leonardo configuration
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class APIKeysConfig:
    gemini_keys: Tuple[str, ...]
    groq: str
    cohere: str
    anthropic: str
    elevenlabs: str
    elevenlabs_voice_id: str
    leonardo: str
    leonardo_character_ref: str  # v16 NEW: optional Character Reference UUID
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
            leonardo_character_ref=os.getenv("LEONARDO_CHARACTER_REF", ""),
            supabase_url=os.getenv("SUPABASE_URL", ""),
            supabase_key=os.getenv("SUPABASE_KEY", ""),
            youtube_client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
            youtube_client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
            youtube_refresh_token=os.getenv("YOUTUBE_REFRESH_TOKEN", ""),
        )

    def validate(self) -> List[str]:
        missing: List[str] = []
        if not self.supabase_url:
            missing.append("SUPABASE_URL")
        if not self.supabase_key:
            missing.append("SUPABASE_KEY")
        if not self.gemini_keys and not self.groq:
            missing.append("GEMINI_API_KEY or GROQ_API_KEY")
        if not self.elevenlabs and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            missing.append("ELEVENLABS_API_KEY or GCP service account")
        if os.getenv("DRY_RUN", "false").lower() != "true":
            if not self.youtube_client_id:
                missing.append("YOUTUBE_CLIENT_ID")
            if not self.youtube_client_secret:
                missing.append("YOUTUBE_CLIENT_SECRET")
            if not self.youtube_refresh_token:
                missing.append("YOUTUBE_REFRESH_TOKEN")
        return missing


@dataclass(frozen=True)
class PathsConfig:
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
    image_cache: Path  # v16 NEW: Leonardo image cache
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
            image_cache=temp / "image_cache",  # v16
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
        for d in (
            self.logs, self.temp, self.assets, self.output,
            self.temp_episodes, self.tts_cache, self.quran_cache,
            self.web_renders, self.html_templates, self.scene_cache,
            self.image_cache,  # v16
            self.videos, self.thumbnails, self.fonts, self.overlays,
            self.branding,
        ):
            d.mkdir(parents=True, exist_ok=True)


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


@dataclass(frozen=True)
class AudioConfig:
    """v16: assumes paid ElevenLabs plan, adaptive voice enabled by default."""
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_stability: float = 0.68
    elevenlabs_similarity: float = 0.88
    elevenlabs_style: float = 0.30
    elevenlabs_speed: float = 0.85
    elevenlabs_speaker_boost: bool = True
    elevenlabs_adaptive: bool = True   # v16 NEW: enable per-emotion overrides
    google_voice: str = "ar-XA-Wavenet-B"
    google_speaking_rate: float = 0.85
    google_pitch: float = -1.0
    target_lufs: float = -16.0
    true_peak: float = -1.5
    quran_start_delay_ms: int = 600
    quran_fade_in_sec: float = 1.0
    quran_end_padding_sec: float = 1.5
    quran_reciter: str = "alafasy"


@dataclass(frozen=True)
class ImageGenConfig:
    """v16 NEW: Leonardo.ai image generation config (paid Apprentice plan)."""
    enabled: bool = True
    # Model UUIDs
    hero_model_id: str = "6b645e3a-d64f-4341-a6d8-7a3690fbf042"  # Phoenix
    scene_model_id: str = "b24e16ff-06e3-43eb-8d33-4416c2d75876"  # Lightning XL
    # Image dimensions
    width: int = 1920
    height: int = 1080
    # Quality
    enable_alchemy: bool = False        # premium upgrade, costs more tokens
    enable_high_resolution: bool = True
    guidance_scale: int = 7
    num_images: int = 1
    # Polling
    poll_interval_sec: float = 3.0
    max_poll_attempts: int = 40
    # Strength when using Character Reference
    character_ref_strength: float = 0.45


@dataclass(frozen=True)
class BrandingConfig:
    channel_name_ar: str = "قِيمَة"
    channel_name_en: str = "VALUE"
    # v16: removed grandfather references
    channel_tagline_ar: str = "تدبر القرآن للأطفال والكبار"
    subscribe_text: str = "اشترك في القناة"
    intro_duration_sec: float = 3.5
    outro_duration_sec: float = 7.0


@dataclass(frozen=True)
class ProceduralConfig:
    palettes: Dict[str, List[str]] = field(default_factory=lambda: dict(PALETTES))
    particle_count: int = 35
    ken_burns_speed: float = 0.05
    word_transition_enabled: bool = True
    browser_pool_size: int = 1
    render_warmup_ms: int = 2000


PALETTES: Dict[str, List[str]] = {
    "warm_sunset": ["#FFB347", "#FFCC70", "#FFE5B4", "#FF6B6B", "#FFA07A"],
    "calm_blue":   ["#7FB3D5", "#A9CCE3", "#D4E6F1", "#85C1E2", "#5DADE2"],
    "lush_green":  ["#52BE80", "#82E0AA", "#ABEBC6", "#239B56", "#7DCEA0"],
    "night_stars": ["#1B2631", "#283747", "#34495E", "#FFD700", "#F5B041"],
    "golden_hour": ["#D4AF37", "#F39C12", "#F5B041", "#FAD7A0", "#F8C471"],
    "soft_morning": ["#FFF8E7", "#FFE5B4", "#FFDAB9", "#FFB347", "#F39C12"],
    "deep_teal":    ["#0D3B4A", "#1B6B7B", "#2E9EAD", "#7DD8E0", "#B2EBF2"],
}


@dataclass(frozen=True)
class EngineConfig:
    script_max_ayah_attempts: int = 3
    voice_parallel_workers: int = 4
    voice_enable_cache: bool = True
    render_scene_timeout_sec: int = 120
    upload_chunk_size_mb: int = 5
    upload_max_retries: int = 5
    enable_prompt_crafting: bool = True
    prompt_crafting_model: str = "gemini-2.5-flash"
    add_ssml: bool = True
    enable_bgm: bool = True
    enable_subtitles: bool = False
    enable_crossfades: bool = True
    enable_color_grade: bool = True
    bgm_volume: float = 0.08
    crossfade_duration: float = 0.4
    quran_parallel_workers: int = 6
    # v16 NEW
    enable_ai_images: bool = True
    image_gen_parallel_workers: int = 3


@dataclass(frozen=True)
class AppConfig:
    api_keys: APIKeysConfig
    paths: PathsConfig
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    image_gen: ImageGenConfig = field(default_factory=ImageGenConfig)
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
        from core.exceptions import ConfigurationError
        missing = self.api_keys.validate()
        if missing:
            raise ConfigurationError(
                f"Missing required configuration: {', '.join(missing)}"
            )
