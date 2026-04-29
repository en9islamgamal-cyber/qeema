"""
infrastructure/tts_providers.py — VALUE / QEEMA v11.0 (Production)
=====================================================================
Concrete TTS implementations.

[Providers]
- ElevenLabsProvider : primary (Egyptian storyteller voice "Haytham")
- GoogleTTSProvider  : fallback (Wavenet Arabic)

Both implement core.interfaces.TTSProvider.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

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
# ElevenLabsProvider
# ════════════════════════════════════════════════════════════════
class ElevenLabsProvider(TTSProvider):
    """ElevenLabs TTS (HTTP API). Primary provider."""

    name: str = "elevenlabs"
    BASE_URL: str = "https://api.elevenlabs.io/v1"

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        *,
        model: str = "eleven_multilingual_v2",
        stability: float = 0.50,
        similarity: float = 0.85,
        style: float = 0.50,
        speaker_boost: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("ElevenLabsProvider requires non-empty api_key")
        if not voice_id:
            raise ValueError("ElevenLabsProvider requires non-empty voice_id")
        self._api_key: str = api_key
        self.voice_id: str = voice_id
        self._model: str = model
        self._stability: float = stability
        self._similarity: float = similarity
        self._style: float = style
        self._speaker_boost: bool = speaker_boost
        logger.info(
            f"✅ ElevenLabs ready (voice={voice_id}, model={model})"
        )

    @retry_with_backoff(
        RetryPolicy(
            max_attempts=3,
            retry_on=(NetworkError, RateLimitError, TransientError),
        )
    )
    def synthesize(self, request: TTSRequest) -> TTSResult:
        url = f"{self.BASE_URL}/text-to-speech/{self.voice_id}"
        text = normalize_arabic_for_tts(request.text)
        if not text:
            raise AudioGenerationError("Empty text for ElevenLabs synthesis")

        payload = {
            "text": text,
            "model_id": self._model,
            "voice_settings": {
                "stability": self._stability,
                "similarity_boost": self._similarity,
                "style": self._style,
                "use_speaker_boost": self._speaker_boost,
            },
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

        if resp.status_code == 401 or resp.status_code == 403:
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


# ════════════════════════════════════════════════════════════════
# GoogleTTSProvider
# ════════════════════════════════════════════════════════════════
class GoogleTTSProvider(TTSProvider):
    """Google Cloud Text-to-Speech (fallback)."""

    name: str = "google_tts"

    def __init__(
        self,
        voice: str = "ar-XA-Wavenet-B",
        speaking_rate: float = 0.95,
        pitch: float = -1.0,
    ) -> None:
        try:
            from google.cloud import texttospeech  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-cloud-texttospeech not installed"
            ) from e
        self._tts = texttospeech
        self._client = texttospeech.TextToSpeechClient()
        self.voice_id: str = voice
        self._rate: float = speaking_rate
        self._pitch: float = pitch
        logger.info(f"✅ Google TTS ready (voice={voice})")

    def _to_ssml(self, text: str) -> str:
        text = normalize_arabic_for_tts(text)
        text = (
            text
            .replace(",", '<break time="300ms"/>')
            .replace(".", '<break time="700ms"/>')
            .replace("?", '<break time="700ms"/>')
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
