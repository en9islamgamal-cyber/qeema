"""
core/interfaces.py — VALUE / QEEMA v11.0 (Production)
=======================================================
Abstract interfaces (Ports) for all engines.
Following Hexagonal Architecture: domain doesn't know about implementations.

Why:
- Testability: نقدر نـ inject mock implementations
- Flexibility: نقدر نغير TTS provider بدون لمس orchestrator
- Documentation: العقد بين الطبقات واضح
- Type safety: mypy يقدر يكشف الأخطاء قبل runtime
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


# ════════════════════════════════════════════════════════════════
# 1. LLM (Script Generation)
# ════════════════════════════════════════════════════════════════
class LLMProvider(Protocol):
    """Any LLM that can generate JSON responses."""

    name: str

    def generate_json(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        ...


# ════════════════════════════════════════════════════════════════
# 2. TTS (Speech Synthesis)
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TTSRequest:
    text: str
    output_path: str
    voice_id: Optional[str] = None
    language: str = "ar"


@dataclass(frozen=True)
class TTSResult:
    output_path: str
    duration_sec: float
    provider: str
    voice_id: str
    cached: bool = False


class TTSProvider(ABC):
    """Abstract TTS provider."""

    name: str
    supports_arabic: bool = True

    @abstractmethod
    def synthesize(self, request: TTSRequest) -> TTSResult:
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Quick check if provider is operational."""
        ...


# ════════════════════════════════════════════════════════════════
# 3. Quran Audio (CDN sources)
# ════════════════════════════════════════════════════════════════
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


class QuranAudioSource(ABC):
    """Single CDN source for Quranic recitation."""

    name: str
    base_url: str

    @abstractmethod
    def fetch(self, request: QuranAudioRequest) -> QuranAudioResult:
        ...

    @abstractmethod
    def supports(self, reciter: str) -> bool:
        ...


# ════════════════════════════════════════════════════════════════
# 4. Visual Renderer (Procedural)
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SceneRenderRequest:
    scene_type: str            # garden, sky, mosque, ...
    palette: str               # warm_sunset, calm_blue, ...
    text: Optional[str]        # narrator text (optional for ayah)
    duration_sec: float
    is_ayah: bool = False
    keywords: List[str] = None  # type: ignore
    output_path: str = ""


@dataclass(frozen=True)
class SceneRenderResult:
    output_path: str
    duration_sec: float
    width: int
    height: int


class VisualRenderer(ABC):
    """Renders a scene to a video file."""

    @abstractmethod
    def render(self, request: SceneRenderRequest, audio_path: str) -> SceneRenderResult:
        ...

    @abstractmethod
    def warmup(self) -> None:
        """Pre-load any expensive resources (e.g., browser pool)."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Clean up resources."""
        ...


# ════════════════════════════════════════════════════════════════
# 5. Video Assembly
# ════════════════════════════════════════════════════════════════
class VideoAssembler(ABC):
    """Concatenates segments into final video."""

    @abstractmethod
    def concat(
        self,
        segments: List[str],
        output_path: str,
        *,
        re_encode: bool = False,
    ) -> str:
        ...

    @abstractmethod
    def get_duration(self, video_path: str) -> float:
        ...


# ════════════════════════════════════════════════════════════════
# 6. Storage / Persistence
# ════════════════════════════════════════════════════════════════
class EpisodeRepository(ABC):
    """CRUD for episodes."""

    @abstractmethod
    def get_or_create(self, episode_number: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    def update_status(
        self,
        episode_id: str,
        status: str,
        **fields: Any,
    ) -> None:
        ...

    @abstractmethod
    def get_pending(self) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def save_state(self, episode_id: str, stage: str, state: Dict[str, Any]) -> None:
        ...

    @abstractmethod
    def get_state(self, episode_id: str, stage: str) -> Optional[Dict[str, Any]]:
        ...


# ════════════════════════════════════════════════════════════════
# 7. Uploader
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class UploadRequest:
    video_path: str
    title: str
    description: str
    tags: List[str]
    thumbnail_path: Optional[str] = None
    privacy: str = "public"
    made_for_kids: bool = True


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    video_url: str
    thumbnail_uploaded: bool


class VideoUploader(ABC):
    """Uploads to a video platform."""

    @abstractmethod
    def upload(self, request: UploadRequest) -> UploadResult:
        ...


# ════════════════════════════════════════════════════════════════
# 8. Quality Gate
# ════════════════════════════════════════════════════════════════
@dataclass
class QualityReport:
    passed: bool
    overall_score: float
    field_scores: Dict[str, float]
    critiques: List[str]
    details: Dict[str, Any] = None  # type: ignore


class QualityValidator(ABC):
    """Validates artifacts against quality rules."""

    @abstractmethod
    def validate(self, artifact: Any) -> QualityReport:
        ...
