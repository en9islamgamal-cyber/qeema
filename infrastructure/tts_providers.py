"""
infrastructure/tts_providers.py — VALUE / QEEMA v18.0
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
from typing import Dict, Optional, Tuple

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
        """Return voice_settings dict, overridden by emotion if provided."""
        base = {
            "stability": self._stability,
            "similarity_boost": self._similarity,
            "style": self._style,
            "use_speaker_boost": self._speaker_boost,
            "speed": self._speed,
        }
        if not self._enable_adaptive or not emotion:
            return base

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
