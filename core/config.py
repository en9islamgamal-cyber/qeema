"""
core/config.py — VALUE / QEEMA v19.0 (7 episodes/month optimized)
=====================================================
[v19 changes]
- ImageGenConfig: Lightning XL for ALL images (Free Trial 150 tokens)
  → exact fit for 7 eps × 7 images × 3 tokens = 147 tokens (98% utilization)
- EngineConfig.voice_parallel_workers = 2 (ElevenLabs Starter concurrent safety)
- AppConfig.quota: QuotaConfig integrated (hard budget enforcement)
- Default quota: 7 episodes/month, 30k EL credits, 150 Leo tokens

[v18 features kept]
- COLOR_GRADES_BY_EMOTION (5 per-emotion grades)
- Re-tuned audio defaults for child engagement
- Tafsir validator integration ready
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple


# v19: forward declaration for QuotaConfig (defined in core/quota_manager.py)
# Lazy-loaded at runtime to avoid circular imports.
if TYPE_CHECKING:
    from core.quota_manager import QuotaConfig as QuotaConfigImport
else:
    QuotaConfigImport = Any


def _make_default_quota():
    """Lazy-import QuotaConfig at runtime to avoid circular imports."""
    try:
        from core.quota_manager import QuotaConfig
        return QuotaConfig()
    except ImportError:
        return None


@dataclass(frozen=True)
class APIKeysConfig:
    gemini_keys: Tuple[str, ...]
    groq: str
    cohere: str
    anthropic: str
    elevenlabs: str
    elevenlabs_voice_id: str
    leonardo: str
    leonardo_character_ref: str
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
    image_cache: Path
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
            image_cache=temp / "image_cache",
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
            self.image_cache,
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
    audio_sample_rate: int = 44100
    # v18: Per-emotion color grade map. Replaces single global filter.
    # Each emotion gets its own tonal signature for visual variety.
    color_grade_default: Optional[str] = (
        "eq=brightness=0.020:saturation=1.10,"
        "colorchannelmixer=rr=1.050:gg=1.0:bb=0.950"
    )

    # Backward-compat: keep this attribute name; main.py uses it as default
    @property
    def color_grade_vf(self) -> Optional[str]:
        return self.color_grade_default


# v18 NEW: Per-emotion color grade dictionary
# Used by orchestrator to select grade per scene
COLOR_GRADES_BY_EMOTION: Dict[str, str] = {
    # Default warm — for explanations
    "warm": (
        "eq=brightness=0.020:saturation=1.10:contrast=1.05,"
        "colorchannelmixer=rr=1.05:gg=1.0:bb=0.95"
    ),
    # Reverent — cool blue, contemplative (Quranic recitation)
    "reverent": (
        "eq=brightness=-0.005:saturation=0.92:contrast=1.05,"
        "colorchannelmixer=rr=0.95:gg=1.0:bb=1.08"
    ),
    # Playful — vibrant, high saturation
    "playful": (
        "eq=brightness=0.035:saturation=1.25:contrast=1.08,"
        "colorchannelmixer=rr=1.08:gg=1.02:bb=0.95"
    ),
    # Peaceful — soft desaturated pastel
    "peaceful": (
        "eq=brightness=0.015:saturation=0.88:contrast=1.0,"
        "colorchannelmixer=rr=1.02:gg=1.0:bb=1.02"
    ),
    # Excited — dramatic high contrast (for hooks)
    "excited": (
        "eq=brightness=0.040:saturation=1.30:contrast=1.15,"
        "colorchannelmixer=rr=1.10:gg=1.0:bb=0.92"
    ),
}


@dataclass(frozen=True)
class AudioConfig:
    """v18: Re-tuned for child engagement (ages 6-12).
    Defaults match the 'warm' emotion in EMOTION_VOICE_OVERRIDES.
    """
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_stability: float = 0.50  # v18: was 0.68 (less variation)
    elevenlabs_similarity: float = 0.88
    elevenlabs_style: float = 0.55      # v18: was 0.30 (more expressive)
    elevenlabs_speed: float = 1.00      # v18: was 0.85 (natural pace)
    elevenlabs_speaker_boost: bool = True
    elevenlabs_adaptive: bool = True
    google_voice: str = "ar-XA-Wavenet-B"
    google_speaking_rate: float = 0.95  # v18: was 0.85
    google_pitch: float = -1.0
    target_lufs: float = -16.0
    true_peak: float = -1.5
    quran_start_delay_ms: int = 600
    quran_fade_in_sec: float = 1.0
    quran_end_padding_sec: float = 1.5
    quran_reciter: str = "alafasy"


@dataclass(frozen=True)
class ImageGenConfig:
    """v19 — Leonardo Free Trial (150 tokens) optimized for 7 episodes/month.

    [Math]
    - 7 eps × 7 images/ep = 49 images
    - Lightning XL: 3 tokens/image → 49 × 3 = 147 tokens (98% utilization, fits!)
    - Phoenix: 10 tokens/image → 49 × 10 = 490 tokens (would need 3.3x budget)

    [Strategy: Lightning XL for everything]
    Lightning XL produces excellent illustrations. The visual prompt
    engineer + Studio Ghibli locked style compensates for the slight
    quality difference vs Phoenix.

    [Upgrade path documented]
    When you upgrade to Leonardo Premium ($24/mo, 25k tokens):
      - Switch hero_model_id back to LEONARDO_PHOENIX
      - Phoenix is on Premium's "unlimited" list → free!
    """
    enabled: bool = True
    # v19: Lightning XL for ALL images (Free Trial budget)
    hero_model_id: str = "b24e16ff-06e3-43eb-8d33-4416c2d75876"   # Lightning XL
    scene_model_id: str = "b24e16ff-06e3-43eb-8d33-4416c2d75876"  # Lightning XL
    # v18 FIX: Leonardo API max dimension is 1536
    width: int = 1536
    height: int = 864
    # Quality settings
    enable_alchemy: bool = False        # alchemy uses extra tokens — keep off
    enable_high_resolution: bool = False  # HD also uses tokens — keep off on free trial
    guidance_scale: int = 7
    num_images: int = 1
    # Polling
    poll_interval_sec: float = 3.0
    max_poll_attempts: int = 40
    character_ref_strength: float = 0.45


@dataclass(frozen=True)
class BrandingConfig:
    channel_name_ar: str = "قِيمَة"
    channel_name_en: str = "VALUE"
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
    # v17: 1 → 3. Three browsers in parallel render scenes 3x faster.
    # RAM cost: ~1.2GB on GitHub runner (7GB total → safe).
    browser_pool_size: int = 3
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
    # v19: Starter plan = 2 concurrent streams. Use 2 workers, no headroom for retries
    # is OK because the retry policy uses sequential retries (not concurrent).
    voice_parallel_workers: int = 2
    voice_enable_cache: bool = True
    render_scene_timeout_sec: int = 180
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
    enable_ai_images: bool = True
    image_gen_parallel_workers: int = 3


@dataclass(frozen=True)
class StageTimeouts:
    """v17 NEW: Per-stage timeout budgets in seconds.

    Used by orchestrator to bound each stage. Prevents one bad ffmpeg
    invocation from eating the whole job budget.
    """
    script: int = 300              # 5 min
    ai_images: int = 180           # 3 min (parallel, paid plan, fast)
    audio: int = 600               # 10 min (TTS for ~25 segments)
    audio_master: int = 120        # 2 min
    render_scenes: int = 2400      # 40 min (44 segments × ~30s with pool=3)
    concat_raw: int = 300          # 5 min (stream-copy is fast)
    bgm_mix: int = 600             # 10 min
    wrap_branded: int = 600        # 10 min (was 900s; stream-copy now)
    thumbnail: int = 60            # 1 min
    upload: int = 1800             # 30 min


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
    stage_timeouts: StageTimeouts = field(default_factory=StageTimeouts)
    # v19: Quota management (HARD budget enforcement)
    quota: "QuotaConfigImport" = field(default_factory=lambda: _make_default_quota())

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
