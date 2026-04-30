"""
core/exceptions.py — VALUE / QEEMA v11.0 (Production)
========================================================
Custom exception hierarchy for clean error handling and routing.

[Design Goals]
1. Distinguish transient vs permanent errors (drives retry decisions)
2. Carry structured context (episode_id, stage, provider, ...) for logging
3. Preserve causal chain (cause= preserves original exception)
4. Be cheap to construct (no heavy IO/serialization in __init__)

[Hierarchy]
    QeemaError
    ├── TransientError          (safe to retry)
    │   ├── NetworkError
    │   ├── TimeoutError
    │   ├── RateLimitError
    │   └── ProviderUnavailableError
    ├── PermanentError          (don't retry — manual intervention)
    │   ├── AuthenticationError
    │   ├── ConfigurationError
    │   ├── ValidationError
    │   └── QuotaExceededError
    └── PipelineError           (stage-specific failures)
        ├── ScriptGenerationError
        ├── AudioGenerationError
        │   └── QuranFetchError
        ├── VisualRenderError
        ├── VideoAssemblyError
        ├── UploadError
        └── QualityGateError
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# ════════════════════════════════════════════════════════════════
# Base
# ════════════════════════════════════════════════════════════════
class QeemaError(Exception):
    """
    Base exception for all QEEMA errors.

    Carries structured context for observability without forcing
    inheritors to redefine __init__ for common fields.
    """

    def __init__(
        self,
        message: str,
        *,
        episode_number: Optional[int] = None,
        stage: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.episode_number: Optional[int] = episode_number
        self.stage: Optional[str] = stage
        self.context: Dict[str, Any] = context or {}
        self.cause: Optional[BaseException] = cause

    def to_dict(self) -> Dict[str, Any]:
        """For structured (JSON) logging."""
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "episode_number": self.episode_number,
            "stage": self.stage,
            "context": self.context,
            "cause": repr(self.cause) if self.cause else None,
        }

    def __str__(self) -> str:
        parts = [self.message]
        if self.stage:
            parts.append(f"stage={self.stage}")
        if self.episode_number is not None:
            parts.append(f"episode={self.episode_number}")
        if self.context:
            # Render context inline so e.g. f"{e}" shows the diagnostic detail.
            # Without this, log.error(f"❌ Fatal error: {e}") drops the context
            # and operators see only the headline message.
            ctx_parts = []
            for k, v in self.context.items():
                v_str = repr(v) if not isinstance(v, str) else v
                # Truncate long values (like stderr_tail) to keep one log line readable
                if len(v_str) > 1500:
                    v_str = v_str[:1500] + "...(truncated)"
                ctx_parts.append(f"{k}={v_str}")
            parts.append("context={" + ", ".join(ctx_parts) + "}")
        if self.cause is not None:
            parts.append(f"cause={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


# ════════════════════════════════════════════════════════════════
# Transient (retry-able)
# ════════════════════════════════════════════════════════════════
class TransientError(QeemaError):
    """Errors that may resolve themselves; safe to retry."""
    pass


class NetworkError(TransientError):
    """Network connectivity issue (DNS, socket, HTTP 5xx)."""
    pass


class TimeoutError(TransientError):
    """Operation exceeded its time budget."""
    pass


class RateLimitError(TransientError):
    """API rate limit reached. Honor retry_after if provided."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after: Optional[float] = retry_after


class ProviderUnavailableError(TransientError):
    """A specific provider is down (circuit-breaker open or 503)."""

    def __init__(self, provider: str, message: str = "", **kwargs: Any) -> None:
        full_msg = f"Provider '{provider}' unavailable: {message}".strip(": ")
        super().__init__(full_msg, **kwargs)
        self.provider: str = provider


# ════════════════════════════════════════════════════════════════
# Permanent (don't retry)
# ════════════════════════════════════════════════════════════════
class PermanentError(QeemaError):
    """Errors that won't resolve by retrying."""
    pass


class AuthenticationError(PermanentError):
    """Invalid credentials. Manual intervention required."""
    pass


class ConfigurationError(PermanentError):
    """System misconfiguration (missing env, invalid settings)."""
    pass


class ValidationError(PermanentError):
    """Input or output failed validation."""

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.field: Optional[str] = field


class QuotaExceededError(PermanentError):
    """Account quota exhausted. Won't recover within retry window."""
    pass


# ════════════════════════════════════════════════════════════════
# Pipeline-specific
# ════════════════════════════════════════════════════════════════
class PipelineError(QeemaError):
    """Errors raised by pipeline stages (intermediate severity)."""
    pass


class ScriptGenerationError(PipelineError):
    """LLM failed to produce valid script."""
    pass


class AudioGenerationError(PipelineError):
    """TTS or audio fetch failed."""
    pass


class QuranFetchError(AudioGenerationError):
    """Could not fetch Quran recitation from any CDN."""

    def __init__(
        self,
        surah: int,
        ayah: int,
        *,
        sources_tried: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        sources_tried = sources_tried or []
        msg = f"Failed to fetch Quran {surah}:{ayah} from {len(sources_tried)} sources"
        super().__init__(msg, **kwargs)
        self.surah: int = surah
        self.ayah: int = ayah
        self.sources_tried: List[str] = sources_tried


class VisualRenderError(PipelineError):
    """Procedural rendering failed (Playwright, FFmpeg encode)."""
    pass


class VideoAssemblyError(PipelineError):
    """FFmpeg concat or final assembly failed."""
    pass


class UploadError(PipelineError):
    """YouTube (or other platform) upload failed."""

    def __init__(
        self,
        message: str,
        *,
        video_path: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.video_path: Optional[str] = video_path


class QualityGateError(PipelineError):
    """Output failed quality validation."""

    def __init__(
        self,
        message: str,
        *,
        score: float = 0.0,
        critiques: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.score: float = score
        self.critiques: List[str] = critiques or []
