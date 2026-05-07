"""
core/interfaces.py — VALUE / QEEMA v11.0 (Production)
=========================================================
Abstract interfaces (Ports) per Hexagonal Architecture.

[Why interfaces?]
1. Testability       — inject mocks for unit tests
2. Replaceability    — swap ElevenLabs for OpenAI TTS without orchestrator changes
3. Type safety       — mypy/pyright catch contract violations
4. Documentation     — contracts are explicit
5. Decoupling        — domain logic independent of infrastructure

[Coverage]
- LLMProvider          : script generation
- TTSProvider          : text-to-speech
- QuranAudioSource     : per-CDN Quran audio fetcher
- VisualRenderer       : procedural scene rendering
- VideoAssembler       : ffmpeg-based concat/encode
- EpisodeRepository    : persistence layer
- VideoUploader        : YouTube (or other) uploader
- QualityValidator     : artifact quality checks
- IntroOutroBuilder    : branded intro/outro wrapper
- ThumbnailBuilder     : thumbnail image creator
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ════════════════════════════════════════════════════════════════
# Data transfer objects
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TTSRequest:
    """Request payload for TTS providers."""
    text: str
    output_path: str
    voice_id: Optional[str] = None
    language: str = "ar"
    emotion: Optional[str] = None  # v23: per-segment emotion for adaptive settings


@dataclass(frozen=True)
class TTSResult:
    output_path: str
    duration_sec: float
    provider: str
    voice_id: str
    cached: bool = False


@dataclass(frozen=True)
class QuranAudioRequest:
    surah: int
    ayah: int
    output_path: str
    reciter: str = "alafasy"


@dataclass(frozen=True)
class QuranAudioResult:
    output_path: str
    duration_sec: float
    source: str
    cached: bool = False


@dataclass(frozen=True)
class SceneRenderRequest:
    """Request to render a single scene (intro/ayah part/outro)."""
    scene_type: str            # garden, sky, mosque, ...
    palette: str               # warm_sunset, calm_blue, ...
    text: str                  # narrator text or ayah text
    is_ayah: bool              # ayah scenes get larger gold typography
    keywords: List[str] = field(default_factory=list)
    output_path: str = ""
    # v14 NEW: cinematic style hints
    extra: dict = field(default_factory=dict)
    # extra keys: text_style (narrator|hook|story|moral|ayah), scene_emotion (warm|reverent|...)


@dataclass(frozen=True)
class SceneRenderResult:
    output_path: str
    duration_sec: float
    width: int
    height: int


@dataclass(frozen=True)
class UploadRequest:
    video_path: str
    title: str
    description: str
    tags: List[str]
    thumbnail_path: Optional[str] = None
    privacy: str = "public"
    made_for_kids: bool = True
    category_id: str = "27"  # Education


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    video_url: str
    thumbnail_uploaded: bool


@dataclass
class QualityReport:
    """Output of a QualityValidator. Mutable for incremental scoring."""
    passed: bool
    overall_score: float
    field_scores: Dict[str, float] = field(default_factory=dict)
    critiques: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════
# Protocol: LLMProvider
# ════════════════════════════════════════════════════════════════
@runtime_checkable
class LLMProvider(Protocol):
    """
    Any LLM that can generate JSON responses.

    Using Protocol (structural typing) rather than ABC because
    we want duck-typing flexibility (3rd-party SDKs already provide
    their own classes).
    """
    name: str

    def generate_json(
        self,
        prompt: str,
        system_instruction: str = "",
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: TTSProvider
# ════════════════════════════════════════════════════════════════
class TTSProvider(ABC):
    """Abstract TTS provider. Use ABC because behavior is non-trivial."""

    name: str
    supports_arabic: bool = True
    voice_id: str = ""

    @abstractmethod
    def synthesize(self, request: TTSRequest) -> TTSResult:
        """Synthesize speech from text. Raises AudioGenerationError on failure."""
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Quick check (≤5s) if provider is operational."""
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: QuranAudioSource
# ════════════════════════════════════════════════════════════════
class QuranAudioSource(ABC):
    """Single CDN source for Quranic recitation."""

    name: str
    base_url: str

    @abstractmethod
    def fetch(self, request: QuranAudioRequest) -> QuranAudioResult:
        """Fetch ayah audio. Raises NetworkError if source unavailable."""
        ...

    @abstractmethod
    def supports(self, reciter: str) -> bool:
        """Does this source provide the requested reciter?"""
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: VisualRenderer
# ════════════════════════════════════════════════════════════════
class VisualRenderer(ABC):
    """Renders procedural scenes to video files."""

    @abstractmethod
    def warmup(self) -> None:
        """Pre-load expensive resources (e.g., browser pool)."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Release all resources."""
        ...

    @abstractmethod
    def render(
        self,
        request: SceneRenderRequest,
        audio_path: str,
    ) -> SceneRenderResult:
        """
        Render scene synced to audio_path duration.
        Output is a .mp4 file at request.output_path.
        """
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: VideoAssembler
# ════════════════════════════════════════════════════════════════
class VideoAssembler(ABC):
    """FFmpeg-based video operations."""

    @abstractmethod
    def concat(
        self,
        segments: List[str],
        output_path: str,
        *,
        re_encode: bool = False,
    ) -> str:
        """Concatenate segments. Stream-copy if re_encode=False."""
        ...

    @abstractmethod
    def get_duration(self, path: str) -> float:
        """Get duration of an audio/video file in seconds."""
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: EpisodeRepository
# ════════════════════════════════════════════════════════════════
class EpisodeRepository(ABC):
    """Persistence layer for episode lifecycle."""

    @abstractmethod
    def get_or_create(self, episode_number: int) -> Dict[str, Any]:
        """Return existing record or create new with status='pending'."""
        ...

    @abstractmethod
    def update_status(
        self,
        episode_id: str,
        status: str,
        **fields: Any,
    ) -> None:
        """Update episode status and arbitrary extra fields."""
        ...

    @abstractmethod
    def get_pending(self) -> Optional[Dict[str, Any]]:
        """Return the next pending episode (lowest episode_number)."""
        ...

    @abstractmethod
    def save_state(
        self,
        episode_id: str,
        stage: str,
        state: Dict[str, Any],
    ) -> None:
        """Persist intermediate stage state for resume."""
        ...

    @abstractmethod
    def get_state(
        self,
        episode_id: str,
        stage: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve stage state if exists."""
        ...

    @abstractmethod
    def list_episodes(self) -> List[Dict[str, Any]]:
        """Return all episodes (for status dashboard)."""
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: VideoUploader
# ════════════════════════════════════════════════════════════════
class VideoUploader(ABC):
    """Uploads videos to a platform (YouTube)."""

    @abstractmethod
    def upload(self, request: UploadRequest) -> UploadResult:
        """Upload + thumbnail. Raises UploadError on failure."""
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: QualityValidator
# ════════════════════════════════════════════════════════════════
class QualityValidator(ABC):
    """Validates artifacts (scripts, audio, video) against quality rules."""

    @abstractmethod
    def validate(self, artifact: Any) -> QualityReport:
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: IntroOutroBuilder
# ════════════════════════════════════════════════════════════════
class IntroOutroBuilder(ABC):
    """Builds and applies branded intro/outro."""

    @abstractmethod
    def build_intro(self) -> str:
        """Return path to (cached) intro video."""
        ...

    @abstractmethod
    def build_outro(self) -> str:
        """Return path to (cached) outro video."""
        ...

    @abstractmethod
    def wrap_episode(self, raw_video: str, output_path: str) -> str:
        """Concat intro + raw + outro. Returns output_path."""
        ...


# ════════════════════════════════════════════════════════════════
# Abstract: ThumbnailBuilder
# ════════════════════════════════════════════════════════════════
class ThumbnailBuilder(ABC):
    """Generates thumbnail image for an episode."""

    @abstractmethod
    def create(
        self,
        script: Any,
        episode_number: int,
        background_image: Optional[str] = None,
    ) -> str:
        """Returns path to generated thumbnail (.jpg)."""
        ...
