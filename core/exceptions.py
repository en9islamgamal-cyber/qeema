"""
core/exceptions.py — VALUE / QEEMA v12.0 (Enterprise Intelligence)
==================================================================
Advanced Exception System with:
  ✅ Observability: Integrated Trace ID and Timestamps.
  ✅ Recovery Metadata: Suggestions for the Orchestrator on how to recover.
  ✅ Saga Support: Tracking which stage needs compensation or rollback.
  ✅ Semantic Classification: Granular categorization for AI-driven logs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ErrorSeverity(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"  # يتطلب تدخل بشري فوري أو إغلاق النظام


class RecoveryStrategy(Enum):
    RETRY_IMMEDIATE = "retry_immediate"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    SWITCH_PROVIDER = "switch_provider"  # الانتقال من Gemini لـ Groq مثلاً
    ABORT_EPISODE = "abort_episode"     # فشل الحلقة الحالية فقط
    SHUTDOWN_SYSTEM = "shutdown_system" # فشل بنيوي يمنع عمل النظام


class QeemaError(Exception):
    """
    النواة الذكية لكافة الأخطاء في النظام.
    كل خطأ هو عبارة عن 'صندوق معلومات' كامل للمهندس المسؤول.
    """
    def __init__(
        self,
        message: str,
        *,
        episode_number: Optional[int] = None,
        stage: Optional[str] = None,
        severity: ErrorSeverity = ErrorSeverity.ERROR,
        recovery: RecoveryStrategy = RecoveryStrategy.RETRY_WITH_BACKOFF,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message
        self.episode_number = episode_number
        self.stage = stage
        self.severity = severity
        self.recovery = recovery
        self.context = context or {}
        self.cause = cause
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.trace_id = str(uuid.uuid4()) # معرف فريد لتتبع الخطأ في الـ Logs

    def to_dict(self) -> Dict[str, Any]:
        """توليد مخرجات JSON جاهزة لأنظمة المراقبة مثل ELK أو Datadog."""
        return {
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "error_type": self.__class__.__name__,
            "severity": self.severity.value,
            "recovery_strategy": self.recovery.value,
            "message": self.message,
            "episode_number": self.episode_number,
            "stage": self.stage,
            "context": self.context,
            "cause": {
                "type": type(self.cause).__name__,
                "message": str(self.cause)
            } if self.cause else None,
        }


# ════════════════════════════════════════════════════════════════
# 1. Transient Errors (Retryable - الطبقة المرنة)
# ════════════════════════════════════════════════════════════════
class TransientError(QeemaError):
    """أخطاء مؤقتة ناتجة عن الشبكة أو ضغط الـ APIs."""
    pass


class RateLimitError(TransientError):
    """تجاوز الكوتة: يتم استخدامه بواسطة الـ TokenBucket في resilience.py."""
    def __init__(self, message: str, retry_after: Optional[float] = None, **kwargs):
        super().__init__(message, recovery=RecoveryStrategy.RETRY_WITH_BACKOFF, **kwargs)
        self.retry_after = retry_after


class ProviderUnavailableError(TransientError):
    """تعطل أحد المزودين: يرسل إشارة للـ Orchestrator لتغيير الـ Adapter."""
    def __init__(self, provider: str, message: str = "", **kwargs):
        full_msg = f"Provider '{provider}' is DOWN. {message}"
        super().__init__(
            full_msg, 
            recovery=RecoveryStrategy.SWITCH_PROVIDER, 
            severity=ErrorSeverity.WARNING,
            **kwargs
        )
        self.provider = provider


# ════════════════════════════════════════════════════════════════
# 2. Permanent Errors (Fatal - الأخطاء القاتلة)
# ════════════════════════════════════════════════════════════════
class PermanentError(QeemaError):
    """أخطاء تتطلب تدخل يدوي أو تصحيح كود ولا ينفع معها الـ Retry."""
    def __init__(self, message: str, **kwargs):
        super().__init__(message, recovery=RecoveryStrategy.ABORT_EPISODE, **kwargs)


class AuthenticationError(PermanentError):
    """خطأ في الـ API Keys: يرفع درجة الخطورة لـ CRITICAL."""
    def __init__(self, message: str, **kwargs):
        super().__init__(message, severity=ErrorSeverity.CRITICAL, **kwargs)


class DependencyError(PermanentError):
    """فشل في المتطلبات البرمجية (مثل عدم وجود FFmpeg أو Playwright)."""
    def __init__(self, tool: str, **kwargs):
        super().__init__(f"System dependency missing: {tool}", severity=ErrorSeverity.CRITICAL, **kwargs)


# ════════════════════════════════════════════════════════════════
# 3. Pipeline & Logic Errors (المراحل الإنتاجية)
# ════════════════════════════════════════════════════════════════
class PipelineError(QeemaError):
    """أخطاء منطقية داخل مراحل الـ Pipeline."""
    pass


class ScriptGenerationError(PipelineError):
    """فشل الـ LLM في إنتاج سكريبت صالح (JSON Error مثلاً)."""
    pass


class VisualRenderError(PipelineError):
    """فشل محرك الرندرة (Playwright/Three.js)."""
    pass


class QualityGateError(PipelineError):
    """فشل بوابة الجودة: يحمل تفاصيل الانتقادات لإرسالها للـ LLM للتصحيح."""
    def __init__(self, message: str, score: float, critiques: List[str], **kwargs):
        super().__init__(message, recovery=RecoveryStrategy.RETRY_IMMEDIATE, **kwargs)
        self.score = score
        self.critiques = critiques


class UploadError(PipelineError):
    """فشل الرفع ليوتيوب (قد يكون توكن منتهي أو فيديو مخالف)."""
    def __init__(self, message: str, video_path: Optional[str] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.video_path = video_path
