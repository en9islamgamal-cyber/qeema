"""
core/interfaces.py — VALUE / QEEMA v12.0 (High-Performance Enterprise)
======================================================================
Core Ports (Interfaces) defining the system boundary.
- Pydantic V2 for high-speed runtime validation.
- Async/Await native protocols for non-blocking I/O.
- Generic Result Wrappers for consistent error propagation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable, TypeVar, Generic
from pydantic import BaseModel, Field, ConfigDict, HttpUrl
from datetime import datetime

T = TypeVar("T")

# ════════════════════════════════════════════════════════════════
# 0. Core Models (Pydantic V2)
# ════════════════════════════════════════════════════════════════

class BaseDomainModel(BaseModel):
    """قاعدة البيانات الأساسية لضمان أداء عالي في الـ Serialization."""
    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        arbitrary_types_allowed=True,
        validate_assignment=True
    )

# ════════════════════════════════════════════════════════════════
# 1. LLM Port (Script Generation)
# ════════════════════════════════════════════════════════════════

@runtime_checkable
class LLMProvider(Protocol):
    """
    بروتوكول غير متزامن للتعامل مع النماذج اللغوية.
    يدعم الـ Structured Output لضمان توافق السكريبت مع الـ Schema.
    """
    name: str

    async def generate_json_async(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        *,
        schema: Optional[Dict[str, Any]] = None,
        temperature: float = 0.3, # قيم منخفضة لضمان الالتزام بالـ JSON
        max_tokens: int = 4000,
    ) -> Dict[str, Any]:
        """توليد JSON مع ضمان الالتزام بالهيكل المطلوب."""
        ...

# ════════════════════════════════════════════════════════════════
# 2. TTS Port (Speech Synthesis)
# ════════════════════════════════════════════════════════════════

class TTSRequest(BaseDomainModel):
    text: str = Field(..., min_length=1)
    output_path: str
    voice_id: Optional[str] = None
    language: str = "ar"
    extra_params: Dict[str, Any] = Field(default_factory=dict)

class TTSResult(BaseDomainModel):
    output_path: str
    duration_sec: float = Field(..., gt=0)
    provider: str
    voice_id: str
    cached: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)

class TTSProvider(ABC):
    """واجهة برمجية موحدة لمحركات الصوت (ElevenLabs, Edge, etc.)"""
    name: str

    @abstractmethod
    async def synthesize_async(self, request: TTSRequest) -> TTSResult:
        """تحويل النص إلى صوت بشكل غير متزامن."""
        ...

    @abstractmethod
    async def health_check_async(self) -> bool:
        ...

# ════════════════════════════════════════════════════════════════
# 3. Visual Rendering Port (Procedural)
# ════════════════════════════════════════════════════════════════

class SceneRenderRequest(BaseDomainModel):
    scene_id: int
    scene_type: str = Field(..., pattern=r"^(garden|sky|house|mosque|ocean|desert|mountains)$")
    palette: str
    text: Optional[str] = None
    duration_sec: float = Field(..., gt=0)
    is_ayah: bool = False
    keywords: List[str] = Field(default_factory=list)
    output_path: str

class SceneRenderResult(BaseDomainModel):
    output_path: str
    duration_sec: float
    width: int = 1920
    height: int = 1080
    render_time_ms: float

class VisualRenderer(ABC):
    """المحرك المسؤول عن تحويل الـ HTML/CSS إلى فيديو (Playwright/GPU)."""
    
    @abstractmethod
    async def render_async(self, request: SceneRenderRequest, audio_path: str) -> SceneRenderResult:
        ...

    @abstractmethod
    async def __aenter__(self) -> VisualRenderer:
        """إعداد الموارد (Browser Pool) بشكل غير متزامن."""
        ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """إغلاق نظيف للموارد."""
        ...

# ════════════════════════════════════════════════════════════════
# 4. Storage & State Port (Repository Pattern)
# ════════════════════════════════════════════════════════════════

class EpisodeRepository(ABC):
    """عزل منطق قاعدة البيانات (Supabase) عن منطق العمل."""
    
    @abstractmethod
    async def get_or_create_async(self, episode_number: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    async def update_status_async(
        self,
        episode_id: str,
        status: str,
        **fields: Any,
    ) -> None:
        ...

    @abstractmethod
    async def save_pipeline_state_async(self, episode_id: str, stage: str, data: Dict[str, Any]) -> None:
        """حفظ الحالة التفصيلية لكل مرحلة لتمكين الاستئناف (Resumability)."""
        ...

# ════════════════════════════════════════════════════════════════
# 5. Delivery Port (YouTube Uploader)
# ════════════════════════════════════════════════════════════════

class UploadRequest(BaseDomainModel):
    video_path: str
    title: str = Field(..., max_length=100)
    description: str
    tags: List[str] = Field(default_factory=list, max_length=15)
    thumbnail_path: Optional[str] = None

class VideoUploader(ABC):
    """واجهة رفع الفيديو (YouTube API)."""
    
    @abstractmethod
    async def upload_async(self, request: UploadRequest) -> str:
        """يرجع رابط الفيديو أو المعرف (Video ID)."""
        ...

# ════════════════════════════════════════════════════════════════
# 6. Quality Control Port (The Judge)
# ════════════════════════════════════════════════════════════════

class QualityReport(BaseDomainModel):
    passed: bool
    overall_score: float = Field(..., ge=0, le=100)
    field_scores: Dict[str, float]
    critiques: List[str]
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class QualityValidator(ABC):
    """فحص السكريبت أو الفيديو قبل المتابعة في الـ Pipeline."""
    
    @abstractmethod
    async def validate_async(self, artifact: Any) -> QualityReport:
        ...
