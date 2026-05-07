"""
infrastructure/tts_providers.py — VALUE / QEEMA v22.5 — ElevenLabs + Google TTS providers
=====================================================================
[v18 changes]
- RE-TUNED EMOTION_VOICE_OVERRIDES for child engagement (6-12 yrs):
  - Higher style values (more expressive)
  - Lower stability (more variation, less monotonous)
  - Speed 0.95-1.05 (natural pace, not slow)
- Added synthesize_with_timestamps() for accurate subtitle timing
- Returns alignment data from /with-timestamps endpoint

[v16 changes kept]
- Adaptive voice: per-emotion stability/style/speed overrides
- Safe SSML extraction: keeps only <break> tags
- All tier-0 limits removed (assumes paid Starter+ plan)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from core.exceptions import (
    AudioGenerationError,
    AuthenticationError,
    NetworkError,
    RateLimitError,
    TransientError,
)
from core.interfaces import TTSProvider, TTSRequest, TTSResult
from core.resilience import RetryPolicy, retry_with_backoff
from infrastructure.audio_utils import (
    get_audio_duration,
    normalize_arabic_for_tts,
    validate_audio_file,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# v18 — RE-TUNED voice settings for child engagement (6-12 yrs)
# ════════════════════════════════════════════════════════════════
# Research basis: child-targeted YouTube channels with >1M subs
# show consistent patterns:
#   - Higher style (>0.45) → more expressive → kids stay engaged
#   - Lower stability (<0.55) → voice variation → not monotonous
#   - Speed 0.95-1.05 → natural pace (NOT slow — kids consume fast)
# v17 used 0.85 speed which is too slow for ages 6-12.
# Slower (0.78) is reserved for Quranic recitation only.
EMOTION_VOICE_OVERRIDES: Dict[str, Dict[str, float]] = {
    "warm": {
        # Default for explanations — engaging but not overstimulating
        "stability": 0.50, "style": 0.55, "speed": 1.00,
    },
    "playful": {
        # For analogies and fun moments — high energy, varied
        "stability": 0.40, "style": 0.65, "speed": 1.05,
    },
    "reverent": {
        # For Quranic recitation context only — calm, contemplative
        # Keep slow speed here — recitation should be slow
        "stability": 0.85, "style": 0.10, "speed": 0.80,
    },
    "peaceful": {
        # For takeaways — reflective but not slow
        "stability": 0.60, "style": 0.30, "speed": 0.95,
    },
    "excited": {
        # For hooks — MUST grab attention in first 3 seconds
        # Lowest stability + highest style + slight speed boost
        "stability": 0.35, "style": 0.70, "speed": 1.05,
    },
}


# Strip <speak> and <prosody> wrappers; keep <break> tags.
# eleven_multilingual_v2 understands <break time='Xms'/> only.
_SPEAK_TAG_RE = re.compile(r'</?speak>')
_PROSODY_TAG_RE = re.compile(r'<prosody[^>]*>|</prosody>')
_NESTED_TAG_RE = re.compile(r'<(?!break)[^>]+>')


def extract_safe_ssml(text: str) -> str:
    """
    v16: Strip wrappers but keep <break> tags.
    eleven_multilingual_v2 supports <break time='Xms'/> for explicit pauses.
    Other SSML tags (prosody, emphasis) are stripped to avoid 400 errors.
    """
    if not text:
        return ""
    text = _SPEAK_TAG_RE.sub("", text)
    text = _PROSODY_TAG_RE.sub("", text)
    text = _NESTED_TAG_RE.sub("", text)
    return text.strip()


# ════════════════════════════════════════════════════════════════
# ElevenLabsProvider
# ════════════════════════════════════════════════════════════════
class ElevenLabsProvider(TTSProvider):
    """ElevenLabs TTS — paid plan optimized."""

    name: str = "elevenlabs"
    BASE_URL: str = "https://api.elevenlabs.io/v1"

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        *,
        model: str = "eleven_multilingual_v2",
        stability: float = 0.68,
        similarity: float = 0.88,
        style: float = 0.30,
        speaker_boost: bool = True,
        speed: float = 0.85,
        enable_adaptive: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("ElevenLabsProvider requires non-empty api_key")
        if not voice_id:
            raise ValueError("ElevenLabsProvider requires non-empty voice_id")
        self._api_key: str = api_key
        self.voice_id: str = voice_id
        self._model: str = model
        # Default voice settings
        self._stability: float = stability
        self._similarity: float = similarity
        self._style: float = style
        self._speaker_boost: bool = speaker_boost
        self._speed: float = max(0.7, min(1.5, speed))
        # v16: enable per-emotion overrides
        self._enable_adaptive: bool = enable_adaptive
        logger.info(
            f"✅ ElevenLabs ready (voice={voice_id}, model={model}, "
            f"stability={stability}, style={style}, speed={speed}, "
            f"adaptive={'on' if enable_adaptive else 'off'})"
        )

    def _settings_for_emotion(self, emotion: Optional[str]) -> Dict[str, float]:
        """Return voice_settings dict, overridden by emotion if provided.

        v22.2: Supports compound emotion strings like "hook:excited" or
        "moral:peaceful" for per-segment-type voice mapping. Falls back to
        bare emotion string for backward compatibility.
        """
        base = {
            "stability": self._stability,
            "similarity_boost": self._similarity,
            "style": self._style,
            "use_speaker_boost": self._speaker_boost,
            "speed": self._speed,
        }
        if not self._enable_adaptive or not emotion:
            return base

        # v22.2: Try voice_emotion_mapper first (segment_type:emotion format)
        # e.g., "hook:excited" → highly variable hook voice settings
        if ":" in emotion:
            try:
                from engines.voice_emotion_mapper import get_voice_settings
                seg_type, emo = emotion.split(":", 1)
                settings = get_voice_settings(
                    segment_type=seg_type, emotion=emo, use_adaptive=True,
                )
                base["stability"] = settings.stability
                base["similarity_boost"] = settings.similarity
                base["style"] = settings.style
                base["speed"] = max(0.7, min(1.5, settings.speed))
                return base
            except (ImportError, Exception):
                # Fall through to legacy emotion-only mapping
                emotion = emotion.split(":", 1)[1]

        override = EMOTION_VOICE_OVERRIDES.get(emotion)
        if override:
            base["stability"] = override.get("stability", base["stability"])
            base["style"] = override.get("style", base["style"])
            base["speed"] = max(0.7, min(1.5, override.get("speed", base["speed"])))
        return base

    @retry_with_backoff(
        RetryPolicy(
            max_attempts=3,
            retry_on=(NetworkError, RateLimitError, TransientError),
        )
    )
    def synthesize(self, request: TTSRequest) -> TTSResult:
        url = f"{self.BASE_URL}/text-to-speech/{self.voice_id}"

        # v16: extract safe SSML if present, else normalize plain Arabic
        raw_text = request.text
        if "<break" in raw_text or "<speak" in raw_text:
            text = extract_safe_ssml(raw_text)
            text = normalize_arabic_for_tts_preserve_breaks(text)
        else:
            text = normalize_arabic_for_tts(raw_text)

        if not text:
            raise AudioGenerationError("Empty text for ElevenLabs synthesis")

        # v16: per-emotion settings if request carries emotion attribute
        emotion = getattr(request, "emotion", None)
        voice_settings = self._settings_for_emotion(emotion)

        payload = {
            "text": text,
            "model_id": self._model,
            "voice_settings": voice_settings,
        }
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
        except requests.Timeout as e:
            raise NetworkError(f"ElevenLabs timeout: {e}", cause=e) from e
        except requests.RequestException as e:
            raise NetworkError(f"ElevenLabs network error: {e}", cause=e) from e

        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"ElevenLabs auth failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "30"))
            raise RateLimitError(
                f"ElevenLabs rate-limited (retry in {retry_after:.0f}s)",
                retry_after=retry_after,
            )
        if resp.status_code in (502, 503, 504):
            raise NetworkError(f"ElevenLabs HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise AudioGenerationError(
                f"ElevenLabs HTTP {resp.status_code}: {resp.text[:200]}"
            )

        return self._write_atomic(resp.content, request.output_path)

    def _write_atomic(self, content: bytes, output_path: str) -> TTSResult:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(content)
        if not validate_audio_file(str(tmp)):
            tmp.unlink(missing_ok=True)
            raise AudioGenerationError("ElevenLabs returned invalid audio content")
        tmp.replace(out)
        return TTSResult(
            output_path=str(out),
            duration_sec=get_audio_duration(str(out)),
            provider=self.name,
            voice_id=self.voice_id,
            cached=False,
        )

    def health_check(self) -> bool:
        try:
            r = requests.get(
                f"{self.BASE_URL}/voices",
                headers={"xi-api-key": self._api_key},
                timeout=10,
            )
            return r.status_code == 200
        except Exception as e:
            logger.warning(f"⚠️ ElevenLabs health check failed: {e}")
            return False

    # ─── v18: Word-level timestamps for accurate subtitles ───────
    @retry_with_backoff(
        RetryPolicy(
            max_attempts=3,
            retry_on=(NetworkError, RateLimitError, TransientError),
        )
    )
    def synthesize_with_timestamps(
        self, request: TTSRequest
    ) -> Tuple[TTSResult, Optional[Dict]]:
        """
        v18: Synthesize audio + return character-level timing data.

        Used by subtitle_engine to build accurate ASS subtitles
        instead of estimating ~4.2 words/sec.

        Returns:
            (TTSResult, alignment) where alignment is:
            {
                "characters": ["أ", "ه", "ل", ...],
                "character_start_times_seconds": [0.0, 0.05, ...],
                "character_end_times_seconds": [0.05, 0.12, ...]
            }
            or None if endpoint unsupported.
        """
        url = f"{self.BASE_URL}/text-to-speech/{self.voice_id}/with-timestamps"

        raw_text = request.text
        if "<break" in raw_text or "<speak" in raw_text:
            text = extract_safe_ssml(raw_text)
            text = normalize_arabic_for_tts_preserve_breaks(text)
        else:
            text = normalize_arabic_for_tts(raw_text)
        if not text:
            raise AudioGenerationError("Empty text for TTS with timestamps")

        emotion = getattr(request, "emotion", None)
        voice_settings = self._settings_for_emotion(emotion)

        payload = {
            "text": text,
            "model_id": self._model,
            "voice_settings": voice_settings,
        }
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
        except requests.Timeout as e:
            raise NetworkError(f"ElevenLabs timestamps timeout: {e}", cause=e) from e
        except requests.RequestException as e:
            raise NetworkError(f"ElevenLabs timestamps network: {e}", cause=e) from e

        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"ElevenLabs auth failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "30"))
            raise RateLimitError(
                f"ElevenLabs rate-limited",
                retry_after=retry_after,
            )
        if resp.status_code in (502, 503, 504):
            raise NetworkError(f"ElevenLabs HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise AudioGenerationError(
                f"ElevenLabs timestamps HTTP {resp.status_code}: {resp.text[:200]}"
            )

        # Response is JSON: {audio_base64, alignment, normalized_alignment}
        try:
            data = resp.json()
        except ValueError as e:
            raise AudioGenerationError(f"Invalid JSON response: {e}") from e

        import base64
        audio_b64 = data.get("audio_base64", "")
        if not audio_b64:
            raise AudioGenerationError("No audio in timestamps response")
        audio_bytes = base64.b64decode(audio_b64)

        # Write audio
        out = Path(request.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(audio_bytes)
        if not validate_audio_file(str(tmp)):
            tmp.unlink(missing_ok=True)
            raise AudioGenerationError("ElevenLabs returned invalid audio")
        tmp.replace(out)

        result = TTSResult(
            output_path=str(out),
            duration_sec=get_audio_duration(str(out)),
            provider=self.name,
            voice_id=self.voice_id,
            cached=False,
        )

        alignment = data.get("normalized_alignment") or data.get("alignment")
        return result, alignment


def normalize_arabic_for_tts_preserve_breaks(text: str) -> str:
    """
    Normalize Arabic but keep <break> tags intact.
    Used when text contains SSML that shouldn't be stripped.
    """
    if not text:
        return ""
    # Temporarily replace <break> tags with placeholders
    breaks: list = []
    def _stash(m):
        breaks.append(m.group(0))
        return f"\x00BREAK{len(breaks)-1}\x00"
    text_protected = re.sub(r'<break\s+time=[\'"][^\'"]+[\'"]\s*/?>', _stash, text)

    # Normalize Arabic
    text_normalized = normalize_arabic_for_tts(text_protected)

    # Restore breaks
    for i, br in enumerate(breaks):
        text_normalized = text_normalized.replace(f"\x00BREAK{i}\x00", br)
    return text_normalized


# ════════════════════════════════════════════════════════════════
# GoogleTTSProvider (fallback)
# ════════════════════════════════════════════════════════════════
class GoogleTTSProvider(TTSProvider):
    name: str = "google_tts"

    def __init__(
        self,
        voice: str = "ar-XA-Wavenet-B",
        speaking_rate: float = 0.85,
        pitch: float = -1.0,
    ) -> None:
        try:
            from google.cloud import texttospeech  # type: ignore
        except ImportError as e:
            raise RuntimeError("google-cloud-texttospeech not installed") from e
        self._tts = texttospeech
        self._client = texttospeech.TextToSpeechClient()
        self.voice_id: str = voice
        self._rate: float = speaking_rate
        self._pitch: float = pitch
        logger.info(f"✅ Google TTS ready (voice={voice}, rate={speaking_rate})")

    def _to_ssml(self, text: str) -> str:
        text = normalize_arabic_for_tts(text)
        text = (
            text
            .replace("،", '<break time="380ms"/>')
            .replace(".", '<break time="700ms"/>')
            .replace("?", '<break time="700ms"/>')
            .replace("!", '<break time="500ms"/>')
        )
        return f"<speak>{text}</speak>"

    @retry_with_backoff(
        RetryPolicy(max_attempts=3, retry_on=(NetworkError, TransientError))
    )
    def synthesize(self, request: TTSRequest) -> TTSResult:
        text = normalize_arabic_for_tts(request.text)
        if not text:
            raise AudioGenerationError("Empty text for Google TTS")

        try:
            voice_params = self._tts.VoiceSelectionParams(
                language_code="ar-XA",
                name=self.voice_id,
            )
            audio_cfg = self._tts.AudioConfig(
                audio_encoding=self._tts.AudioEncoding.MP3,
                speaking_rate=self._rate,
                pitch=self._pitch,
            )
            response = self._client.synthesize_speech(
                input=self._tts.SynthesisInput(ssml=self._to_ssml(text)),
                voice=voice_params,
                audio_config=audio_cfg,
            )
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("permission", "401", "403", "credential")):
                raise AuthenticationError(f"Google TTS auth: {e}", cause=e) from e
            if any(k in msg for k in ("quota", "rate", "429")):
                raise RateLimitError(f"Google TTS quota: {e}", cause=e) from e
            if any(k in msg for k in ("connection", "timeout", "network")):
                raise NetworkError(f"Google TTS network: {e}", cause=e) from e
            raise TransientError(f"Google TTS error: {e}", cause=e) from e

        out = Path(request.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(response.audio_content)
        if not validate_audio_file(str(tmp)):
            tmp.unlink(missing_ok=True)
            raise AudioGenerationError("Google TTS returned invalid audio")
        tmp.replace(out)

        return TTSResult(
            output_path=str(out),
            duration_sec=get_audio_duration(str(out)),
            provider=self.name,
            voice_id=self.voice_id,
            cached=False,
        )

    def health_check(self) -> bool:
        try:
            self._client.list_voices(language_code="ar-XA", timeout=5)
            return True
        except Exception as e:
            logger.warning(f"⚠️ Google TTS health check failed: {e}")
            return False


# ════════════════════════════════════════════════════════════════
# CAMB AI Provider (v22.5 — TTS fallback after ElevenLabs)
# ════════════════════════════════════════════════════════════════
# Built against the OFFICIAL docs.camb.ai API spec (verified 2026-05-07):
#
#  1. POST  /apis/tts                    → {"task_id": "<string>"}
#  2. GET   /apis/tts/{task_id}          → {"status": "...", "run_id": <int>}
#  3. GET   /apis/tts-result/{run_id}    → audio/flac binary stream (default)
#                              ?output_type=file_url → {"output_url": "<url>"}
#  4. GET   /apis/source-languages       → [{"id": <int>, "language": "<name>"}]
#
# CRITICAL CORRECTNESS NOTES:
#   - run_id is INTEGER, not string
#   - tts-result returns RAW AUDIO BYTES by default, NOT JSON
#   - Audio is FLAC format (we let FFmpeg handle the format-agnostic save)
#   - language_id MUST be discovered at runtime via /source-languages
#     because we cannot hardcode an ID without verifying it exists
class CambAIProvider(TTSProvider):
    """CAMB.AI MARS8 TTS — fallback when ElevenLabs is unavailable.

    [Why exists]
    User has CAMB_AI_KEY in repo secrets. Acts as the second tier in the
    TTS fallback chain (ElevenLabs → CambAI → Google TTS). Provides
    production-grade Arabic synthesis when ElevenLabs is rate-limited,
    quota-exhausted, or auth-failed.

    [API workflow — 3 steps, asynchronous]
    1. POST text → returns task_id
    2. Poll task → eventually returns SUCCESS + run_id (integer)
    3. GET tts-result/{run_id} → STREAMS audio bytes (FLAC format)

    [Language discovery]
    The language_id for Arabic is NOT hardcoded. On first synthesize() call
    we hit /source-languages and find the first Arabic dialect available.
    The result is cached on the instance for the lifetime of the process.
    Override with CAMB_AI_LANGUAGE_ID env var if auto-discovery fails.

    [Output format]
    CAMB returns FLAC by default. We save the bytes verbatim to whatever
    extension the caller asked for (.mp3 typically). The downstream
    voice_engine.master_episode runs FFmpeg which handles format detection
    via libavformat regardless of the file extension. So a "FLAC body in
    a .mp3 file" works — but emits a confusing log line. Production-quality
    fix: rename output to .flac. Acceptable trade-off for a fallback path.
    """

    name: str = "camb_ai"
    BASE_URL: str = "https://client.camb.ai/apis"

    # Process-level cache: {api_key_hash: language_id}. Avoids re-hitting
    # /source-languages on every CambAIProvider instantiation in the same run.
    _LANGUAGE_CACHE: Dict[str, int] = {}

    def __init__(
        self,
        api_key: str,
        voice_id: int,
        speech_model: str = "mars-pro",
        language_id_override: Optional[int] = None,
        timeout_per_attempt_sec: float = 60.0,
        poll_interval_sec: float = 1.5,
        max_poll_attempts: int = 40,
    ) -> None:
        if not api_key:
            raise ValueError("CambAIProvider requires non-empty api_key")
        if not voice_id:
            raise ValueError(
                "CambAIProvider requires a voice_id (pick one from "
                "https://studio.camb.ai or call /list-voices)."
            )
        self._api_key = api_key
        self.voice_id = str(voice_id)
        self._voice_id_int = int(voice_id)
        self._speech_model = speech_model
        self._language_id_override = language_id_override
        self._timeout = timeout_per_attempt_sec
        self._poll_interval = poll_interval_sec
        self._max_polls = max_poll_attempts
        # Lazy-resolved on first synthesize() call
        self._resolved_language_id: Optional[int] = language_id_override

    # ─── Public API ──────────────────────────────────────────────
    @retry_with_backoff(
        RetryPolicy(
            max_attempts=3,
            initial_delay_sec=2.0,
            max_delay_sec=15.0,
            retry_on=(NetworkError, RateLimitError, TransientError),
        )
    )
    def synthesize(self, request: TTSRequest) -> TTSResult:
        """Synthesise Arabic speech via CAMB MARS8.

        Wrapped in @retry_with_backoff so the entire 3-step workflow
        (submit + poll + download) gets retried as one unit on transient
        errors. Permanent errors (auth, FAILED status, validation) raise
        immediately and skip retry per RetryPolicy.skip_on default.
        """
        text = normalize_arabic_for_tts(request.text)
        if not text.strip():
            raise AudioGenerationError("CambAI: empty text after normalization")

        # Resolve language_id lazily (once per process).
        language_id = self._get_language_id()

        task_id = self._submit_task(text, language_id)
        run_id = self._poll_until_complete(task_id)
        audio_bytes = self._download_audio_bytes(run_id)

        out = Path(request.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(audio_bytes)
        if not validate_audio_file(str(tmp)):
            tmp.unlink(missing_ok=True)
            raise AudioGenerationError(
                "CambAI returned data that ffprobe could not parse as audio"
            )
        tmp.replace(out)

        return TTSResult(
            output_path=str(out),
            duration_sec=get_audio_duration(str(out)),
            provider=self.name,
            voice_id=self.voice_id,
            cached=False,
        )

    def health_check(self) -> bool:
        """Quick auth check via /list-voices (cheap, no charge)."""
        try:
            r = requests.get(
                f"{self.BASE_URL}/list-voices",
                headers={"x-api-key": self._api_key},
                timeout=5,
            )
            if r.status_code in (401, 403):
                logger.warning(
                    f"⚠️ CambAI health check: HTTP {r.status_code} (key invalid)"
                )
                return False
            return r.status_code == 200
        except Exception as e:
            logger.warning(f"⚠️ CambAI health check failed: {e}")
            return False

    # ─── Internal: language discovery ───────────────────────────
    def _get_language_id(self) -> int:
        """Return the resolved Arabic language_id for the CAMB API.

        Priority:
          1. Explicit override from constructor / env var
          2. Process-level cache (per api_key)
          3. Live /source-languages query → cached on success
          4. Hard error if Arabic not found

        We MUST NOT silently fall back to a guessed integer — if Arabic
        is unsupported by the user's CAMB plan, raising loud is better
        than silently sending Spanish-language synthesis.
        """
        if self._resolved_language_id is not None:
            return self._resolved_language_id

        # Per-process cache lookup
        cache_key = self._api_key[:16]  # don't store full key as map key
        if cache_key in self._LANGUAGE_CACHE:
            self._resolved_language_id = self._LANGUAGE_CACHE[cache_key]
            return self._resolved_language_id

        # Live lookup
        try:
            r = requests.get(
                f"{self.BASE_URL}/source-languages",
                headers={"x-api-key": self._api_key},
                timeout=10,
            )
        except requests.RequestException as e:
            raise NetworkError(
                f"CambAI /source-languages network: {e}", cause=e,
            ) from e

        if r.status_code in (401, 403):
            raise AuthenticationError(
                f"CambAI /source-languages: HTTP {r.status_code}"
            )
        if r.status_code != 200:
            raise TransientError(
                f"CambAI /source-languages: HTTP {r.status_code}"
            )

        try:
            languages = r.json()
        except ValueError as e:
            raise AudioGenerationError(
                f"CambAI /source-languages bad JSON: {e}"
            ) from e

        if not isinstance(languages, list):
            raise AudioGenerationError(
                f"CambAI /source-languages expected list, got "
                f"{type(languages).__name__}"
            )

        # Find Arabic — case-insensitive substring match. Prefer:
        #   1. Egyptian Arabic (matches user's content)
        #   2. Modern Standard Arabic
        #   3. Any Arabic dialect (Gulf, Levantine, etc.)
        arabic_id = self._pick_arabic_language_id(languages)
        if arabic_id is None:
            available = [
                lang.get("language", "?") for lang in languages
                if isinstance(lang, dict)
            ][:20]
            raise AudioGenerationError(
                f"CambAI /source-languages: no Arabic dialect found. "
                f"First 20 languages available: {available}. "
                f"Set CAMB_AI_LANGUAGE_ID env var to force a specific ID."
            )

        self._LANGUAGE_CACHE[cache_key] = arabic_id
        self._resolved_language_id = arabic_id
        logger.info(f"📍 CambAI Arabic language_id resolved: {arabic_id}")
        return arabic_id

    @staticmethod
    def _pick_arabic_language_id(languages: List[Dict]) -> Optional[int]:
        """Pick the best Arabic match from the language list.

        Tries Egyptian first (matches user's audience), then MSA, then any
        Arabic dialect. Returns None if no Arabic at all.
        """
        # Field name not documented as fixed — try common candidates
        def lang_name(entry: Dict) -> str:
            return str(
                entry.get("language", "")
                or entry.get("name", "")
                or entry.get("display_name", "")
            ).lower()

        def lang_id(entry: Dict) -> Optional[int]:
            for key in ("id", "language_id", "source_language_id"):
                v = entry.get(key)
                if isinstance(v, int):
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)
            return None

        # Tier 1: Egyptian Arabic
        for entry in languages:
            if not isinstance(entry, dict):
                continue
            name = lang_name(entry)
            if "egypt" in name and "arab" in name:
                lid = lang_id(entry)
                if lid is not None:
                    return lid

        # Tier 2: Modern Standard Arabic
        for entry in languages:
            if not isinstance(entry, dict):
                continue
            name = lang_name(entry)
            if ("modern standard" in name or "msa" in name) and "arab" in name:
                lid = lang_id(entry)
                if lid is not None:
                    return lid

        # Tier 3: any Arabic
        for entry in languages:
            if not isinstance(entry, dict):
                continue
            if "arab" in lang_name(entry):
                lid = lang_id(entry)
                if lid is not None:
                    return lid

        return None

    # ─── Internal: API workflow ─────────────────────────────────
    def _submit_task(self, text: str, language_id: int) -> str:
        """POST /apis/tts → returns task_id (string).

        NOTE: The legacy /apis/tts endpoint does NOT accept a `speech_model`
        parameter — that's a feature of the new Python SDK with string language
        codes ("en-us"), not the integer-language REST API. The actual model
        used is determined by the studio's default (typically MARS-Pro for
        production-grade voices). Configure model selection in the CAMB studio
        dashboard for now.
        """
        url = f"{self.BASE_URL}/tts"
        # project_name has 3-255 char minimum per CAMB schema
        payload = {
            "text": text,
            "voice_id": self._voice_id_int,
            "language": language_id,
            "project_name": "VALUE-QEEMA-fallback",
            "project_description": (
                "Arabic Quran-for-children fallback TTS — used when "
                "ElevenLabs primary path is unavailable"
            ),
        }
        try:
            r = requests.post(
                url,
                json=payload,
                headers={
                    "x-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        except requests.Timeout as e:
            raise NetworkError(f"CambAI submit timeout: {e}", cause=e) from e
        except requests.RequestException as e:
            raise NetworkError(f"CambAI submit network: {e}", cause=e) from e

        return self._parse_submit_response(r)

    def _parse_submit_response(self, r: requests.Response) -> str:
        """Parse submit response. CAMB returns 200 with {"task_id": str}."""
        if r.status_code in (401, 403):
            raise AuthenticationError(
                f"CambAI submit auth: HTTP {r.status_code}"
            )
        if r.status_code == 422:
            # Validation error — most likely bad voice_id or language_id.
            # NOT transient, raise immediately
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:300] if r.text else "(no body)"
            raise AudioGenerationError(
                f"CambAI submit validation (422): {detail}"
            )
        if r.status_code == 429:
            raise RateLimitError("CambAI rate limit: HTTP 429")
        if r.status_code >= 500:
            raise TransientError(f"CambAI server: HTTP {r.status_code}")
        if r.status_code != 200:
            raise AudioGenerationError(
                f"CambAI submit unexpected HTTP {r.status_code}: "
                f"{r.text[:300] if r.text else '(no body)'}"
            )

        try:
            data = r.json()
        except ValueError as e:
            raise AudioGenerationError(
                f"CambAI submit returned invalid JSON: {e}"
            ) from e

        # Per official docs: response shape is exactly {"task_id": "<string>"}
        task_id = data.get("task_id")
        if not task_id:
            raise AudioGenerationError(
                f"CambAI submit response missing task_id: {data}"
            )
        return str(task_id)

    def _poll_until_complete(self, task_id: str) -> int:
        """Poll GET /apis/tts/{task_id} until SUCCESS. Returns run_id (int)."""
        import time as _time
        url = f"{self.BASE_URL}/tts/{task_id}"
        for attempt in range(self._max_polls):
            try:
                r = requests.get(
                    url,
                    headers={"x-api-key": self._api_key},
                    timeout=15,
                )
            except requests.RequestException as e:
                logger.warning(
                    f"⚠️ CambAI poll attempt {attempt+1}/{self._max_polls} "
                    f"network err: {e}"
                )
                _time.sleep(self._poll_interval)
                continue

            if r.status_code in (401, 403):
                raise AuthenticationError(
                    f"CambAI poll auth: HTTP {r.status_code}"
                )
            if r.status_code != 200:
                logger.warning(
                    f"⚠️ CambAI poll attempt {attempt+1}/{self._max_polls} "
                    f"HTTP {r.status_code}"
                )
                _time.sleep(self._poll_interval)
                continue

            try:
                data = r.json()
            except ValueError:
                _time.sleep(self._poll_interval)
                continue

            status = (data.get("status") or "").upper()
            if status == "SUCCESS":
                run_id = data.get("run_id")
                if run_id is None:
                    raise AudioGenerationError(
                        f"CambAI poll SUCCESS but no run_id: {data}"
                    )
                # Per docs: run_id is integer — coerce defensively
                try:
                    return int(run_id)
                except (TypeError, ValueError):
                    raise AudioGenerationError(
                        f"CambAI run_id not coercible to int: {run_id!r}"
                    )
            if status in ("FAILED", "ERROR", "CANCELLED"):
                err = (
                    data.get("error")
                    or data.get("message")
                    or "(no error detail)"
                )
                raise AudioGenerationError(
                    f"CambAI task {task_id} terminal status={status}: {err}"
                )

            # PENDING / RUNNING → wait
            _time.sleep(self._poll_interval)

        raise AudioGenerationError(
            f"CambAI task {task_id} did not complete after "
            f"{self._max_polls} polls "
            f"(~{self._max_polls * self._poll_interval:.0f}s)"
        )

    def _download_audio_bytes(self, run_id: int) -> bytes:
        """GET /apis/tts-result/{run_id} → returns audio bytes directly.

        Per official docs, this endpoint by default returns audio/flac as
        a streaming binary response. We do NOT try to parse JSON unless
        we explicitly use ?output_type=file_url. Stream with chunks to
        handle larger files efficiently.
        """
        url = f"{self.BASE_URL}/tts-result/{run_id}"
        try:
            r = requests.get(
                url,
                headers={"x-api-key": self._api_key},
                timeout=60,
                stream=True,
            )
        except requests.RequestException as e:
            raise NetworkError(
                f"CambAI tts-result network: {e}", cause=e,
            ) from e

        if r.status_code in (401, 403):
            raise AuthenticationError(
                f"CambAI tts-result auth: HTTP {r.status_code}"
            )
        if r.status_code == 404:
            raise AudioGenerationError(
                f"CambAI run_id {run_id} not found (HTTP 404). "
                f"The run may have expired or never existed."
            )
        if r.status_code != 200:
            raise AudioGenerationError(
                f"CambAI tts-result HTTP {r.status_code}: "
                f"{(r.text[:300] if r.text else '(no body)')}"
            )

        # Defensive: check Content-Type. JSON here would mean we accidentally
        # got an error response disguised as 200, OR the server changed format.
        content_type = (r.headers.get("Content-Type") or "").lower()
        if "json" in content_type:
            # Server returned JSON when we expected audio — surface it
            try:
                err_body = r.json()
            except ValueError:
                err_body = r.text[:300]
            raise AudioGenerationError(
                f"CambAI tts-result returned JSON when audio was expected: "
                f"{err_body}"
            )

        # Stream + accumulate
        chunks: List[bytes] = []
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                chunks.append(chunk)
        content = b"".join(chunks)

        if len(content) < 1024:
            # Anything under 1KB is too small to be valid audio — likely
            # an HTML error page or empty response
            raise AudioGenerationError(
                f"CambAI tts-result returned only {len(content)} bytes "
                f"(probably an error response, not audio). "
                f"Content-Type was: {content_type or '(none)'}"
            )
        return content
