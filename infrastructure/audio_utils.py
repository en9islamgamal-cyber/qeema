"""
infrastructure/audio_utils.py — VALUE / QEEMA v11.0 (Production)
====================================================================
Shared audio utilities (validation, duration probing, normalization).

Kept in a single module to avoid duplicate definitions and ensure
consistent behavior across TTS and Quran fetchers.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess as sp
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════
MIN_VALID_BYTES: Final[int] = 500
MIN_VALID_DURATION_SEC: Final[float] = 0.3
DEFAULT_FALLBACK_DURATION_SEC: Final[float] = 5.0


# ════════════════════════════════════════════════════════════════
# Arabic text normalization for TTS
# ════════════════════════════════════════════════════════════════
_TASHKEEL_END_RE = re.compile(r"[\u064B-\u0650]+(?=\s|$|[،.؟!:])")
_WS_RE = re.compile(r"\s+")


def normalize_arabic_for_tts(text: str) -> str:
    """
    Light normalization for TTS engines:
    - Strip word-final tashkeel (reduces artifacts)
    - Convert Arabic punctuation to ASCII (engines handle ASCII better)
    - Collapse whitespace
    """
    if not text:
        return ""
    text = _TASHKEEL_END_RE.sub("", text)
    text = (
        text
        .replace("،", ",")
        .replace("؟", "?")
        .replace("؛", ";")
    )
    text = _WS_RE.sub(" ", text).strip()
    return text


# ════════════════════════════════════════════════════════════════
# Cache key derivation
# ════════════════════════════════════════════════════════════════
def stable_cache_key(*parts: str) -> str:
    """SHA-256-based deterministic key. Truncated to 16 hex chars."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")  # separator avoids collision tricks
    return h.hexdigest()[:16]


# ════════════════════════════════════════════════════════════════
# ffprobe wrapper
# ════════════════════════════════════════════════════════════════
def get_audio_duration(path: str, *, fallback: float = DEFAULT_FALLBACK_DURATION_SEC) -> float:
    """
    Get media duration via ffprobe. Returns `fallback` on any failure.

    [Why not raise?] Duration probing is best-effort metadata; the
    pipeline should keep working with a sane default.
    """
    try:
        result = sp.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning(f"⚠️ ffprobe non-zero exit for {path}")
            return fallback
        text = result.stdout.strip()
        if not text:
            return fallback
        d = float(text)
        return d if d > 0.05 else fallback
    except (sp.TimeoutExpired, ValueError, FileNotFoundError) as e:
        logger.warning(f"⚠️ ffprobe failed for {path}: {e}")
        return fallback


def validate_audio_file(
    path: str,
    *,
    min_duration: float = MIN_VALID_DURATION_SEC,
    min_bytes: int = MIN_VALID_BYTES,
) -> bool:
    """Quick sanity check: file exists, has content, has detectable duration."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < min_bytes:
        return False
    return get_audio_duration(path, fallback=0.0) >= min_duration
