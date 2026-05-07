"""
infrastructure/audio_utils.py — VALUE / QEEMA v22.5 — audio normalization helpers
====================================================================
[Changes v15]
- normalize_arabic_for_tts now strips ALL tashkeel (was only word-final)
- Arabic comma ، is preserved — NOT converted to ASCII ,
  (ElevenLabs v2 multilingual understands Arabic punctuation natively
   and gives a better prosodic pause with ، than with ,)
- ASCII ? and ; conversion kept (engines handle these better)
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess as sp
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


MIN_VALID_BYTES: Final[int] = 500
MIN_VALID_DURATION_SEC: Final[float] = 0.3
DEFAULT_FALLBACK_DURATION_SEC: Final[float] = 5.0

# v15: Strip ALL tashkeel (U+064B–U+0652), not just word-final
_ALL_TASHKEEL_RE = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_WS_RE = re.compile(r"\s+")


def normalize_arabic_for_tts(text: str) -> str:
    """
    Normalization for TTS engines:
    - Strip ALL tashkeel (removes robotic over-pronounced endings)
    - Preserve Arabic comma ، (ElevenLabs reads it with correct prosody)
    - Convert ؟ → ? and ؛ → ; (ASCII equivalents engines handle better)
    - Collapse whitespace
    """
    if not text:
        return ""
    # Strip all harakat/tashkeel
    text = _ALL_TASHKEEL_RE.sub("", text)
    # Keep ، as-is; only convert question mark and semicolon
    text = text.replace("؟", "?").replace("؛", ";")
    text = _WS_RE.sub(" ", text).strip()
    return text


def stable_cache_key(*parts: str) -> str:
    """SHA-256-based deterministic key. Truncated to 16 hex chars."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def get_audio_duration(path: str, *, fallback: float = DEFAULT_FALLBACK_DURATION_SEC) -> float:
    """Get media duration via ffprobe. Returns `fallback` on any failure."""
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
