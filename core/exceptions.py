"""
core/exceptions.py — VALUE / QEEMA v11.0 (Production)
======================================================
Custom exception hierarchy for clean error handling.

Why:
- التمييز بين الأخطاء المؤقتة (transient) والدائمة (permanent)
- Retry logic ذكي بناءً على نوع الخطأ
- Logging مهيكل (structured) مع context

Hierarchy:
  QeemaError (base)
   ├── TransientError (retry-able)
   │    ├── RateLimitError
   │    ├── NetworkError
   │    └── TimeoutError
   ├── PermanentError (don't retry)
   │    ├── AuthenticationError
   │    ├── ValidationError
   │    └── ConfigurationError
   └── PipelineError
        ├── ScriptGenerationError
        ├── AudioGenerationError
        ├── VisualRenderError
        └── UploadError
"""
from __future__ import annotations
from typing import Optional, Dict, Any


class QeemaError(Exception):
    """Base exception for all QEEMA pipeline errors."""

    def __init__(
        self,
        message: str,
        *,
        episode_number: Optional[int] = None,
        stage: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message
        self.episode_number = episode_number
        self.stage = stage
        self.context = context or {}
        self.cause = cause

    def to_dict(self) -> Dict[str, Any]:
        """For structured logging."""
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "episode_number": self.episode_number,
            "stage": self.stage,
            "context": self.context,
            "cause": str(self.cause) if self.cause else None,
        }


# ─── Transient errors (retry-able) ────────────────────────────────
class TransientError(QeemaError):
    """Errors that may resolve themselves; safe to retry."""
    pass


class RateLimitError(TransientError):
    """API rate limit hit; retry with backoff."""
    def __init__(self, message: str, retry_after: Optional[float] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class NetworkError(TransientError):
    """Network connectivity issue."""
    pass


class TimeoutError(TransientError):
    """Operation timed out."""
    pass


class ProviderUnavailableError(TransientError):
    """External service is down; switch to fallback."""
    def __init__(self, provider: str, message: str = "", **kwargs):
        full_msg = f"Provider '{provider}' unavailable: {message}"
        super().__init__(full_msg, **kwargs)
        self.provider = provider


# ─── Permanent errors (don't retry) ───────────────────────────────
class PermanentError(QeemaError):
    """Errors that won't resolve by retrying."""
    pass


class AuthenticationError(PermanentError):
    """Invalid credentials; manual intervention needed."""
    pass


class ValidationError(PermanentError):
    """Data validation failed."""
    def __init__(self, message: str, field: Optional[str] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.field = field


class ConfigurationError(PermanentError):
    """System misconfiguration."""
    pass


class QuotaExceededError(PermanentError):
    """Account quota exhausted."""
    pass


# ─── Pipeline-specific errors ─────────────────────────────────────
class PipelineError(QeemaError):
    """Errors specific to pipeline stages."""
    pass


class ScriptGenerationError(PipelineError):
    """LLM failed to produce valid script."""
    pass


class AudioGenerationError(PipelineError):
    """TTS or Quran fetch failed."""
    pass


class QuranFetchError(AudioGenerationError):
    """Could not fetch Quranic recitation from any CDN."""
    def __init__(self, surah: int, ayah: int, sources_tried: list, **kwargs):
        msg = f"Failed to fetch Quran {surah}:{ayah} from {len(sources_tried)} sources"
        super().__init__(msg, **kwargs)
        self.surah = surah
        self.ayah = ayah
        self.sources_tried = sources_tried


class VisualRenderError(PipelineError):
    """Procedural rendering failed."""
    pass


class VideoAssemblyError(PipelineError):
    """FFmpeg assembly failed."""
    pass


class UploadError(PipelineError):
    """YouTube upload failed."""
    def __init__(self, message: str, *, video_path: Optional[str] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.video_path = video_path


# ─── Quality gate errors ──────────────────────────────────────────
class QualityGateError(PipelineError):
    """Output failed quality validation."""
    def __init__(self, message: str, *, score: float = 0.0, critiques: list = None, **kwargs):
        super().__init__(message, **kwargs)
        self.score = score
        self.critiques = critiques or []
