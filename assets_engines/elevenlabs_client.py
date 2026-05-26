"""
assets_engines/elevenlabs_client.py
====================================================================
ElevenLabs TTS client.

Generates Arabic narration with the agreed voice + settings:
  voice_id: UR972wNGq3zluze0LoIp (env-configurable)
  model: eleven_multilingual_v2
  stability: 0.35 (expressive, not monotone)
  similarity: 0.75
  style: 0.65 (warm storyteller)
  speed: 0.95 (slightly slower for kids)

Each call returns an MP3 file path. We cache by prompt hash.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from core.config import (
    ELEVENLABS_CACHE_DIR, get_api_keys, get_pipeline_config,
)


log = logging.getLogger(__name__)

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
HTTP_TIMEOUT_SEC = 60


class ElevenLabsError(Exception):
    pass


@dataclass
class TTSResult:
    text: str
    local_path: Path
    chars_billed: int


class ElevenLabsClient:
    """Single-purpose ElevenLabs TTS client."""

    def __init__(self) -> None:
        self.keys = get_api_keys()
        self.cfg = get_pipeline_config()
        ELEVENLABS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict:
        return {
            "xi-api-key": self.keys.elevenlabs_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        label: str = "narration",
    ) -> TTSResult:
        """
        Generate speech for `text`. Returns local path.

        `label` is used to name the cache file (e.g. "hook", "intro", "ayah_3").
        """
        text = text.strip()
        if not text:
            raise ElevenLabsError("Empty text passed to synthesize")

        vid = voice_id or self.keys.elevenlabs_voice_id

        # Cache lookup
        cache_key = hashlib.sha1(
            f"{vid}|{text}|{self.cfg.elevenlabs_stability}".encode("utf-8")
        ).hexdigest()[:16]
        cached = ELEVENLABS_CACHE_DIR / f"{label}_{cache_key}.mp3"
        if cached.exists() and cached.stat().st_size > 1024:
            log.info("✓ ElevenLabs cache hit: %s", cached.name)
            return TTSResult(text=text, local_path=cached, chars_billed=0)

        payload = {
            "text": text,
            "model_id": self.cfg.elevenlabs_model,
            "voice_settings": {
                "stability": self.cfg.elevenlabs_stability,
                "similarity_boost": self.cfg.elevenlabs_similarity,
                "style": self.cfg.elevenlabs_style,
                "use_speaker_boost": True,
            },
            # Speed control via the speed parameter (newer API)
            "voice_settings_speed": self.cfg.elevenlabs_speed,
        }

        url = f"{ELEVENLABS_API_BASE}/text-to-speech/{vid}"

        last_err = None
        for attempt in range(1, self.cfg.elevenlabs_max_retries + 2):
            try:
                log.info(
                    "TTS request (%s): %d chars, attempt %d",
                    label, len(text), attempt,
                )
                r = requests.post(
                    url, json=payload, headers=self._headers(),
                    timeout=HTTP_TIMEOUT_SEC, stream=True,
                )
                r.raise_for_status()

                tmp = cached.with_suffix(".tmp")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                tmp.rename(cached)
                size = cached.stat().st_size
                log.info("✓ TTS saved: %s (%d bytes)", cached.name, size)
                return TTSResult(text=text, local_path=cached, chars_billed=len(text))

            except Exception as e:
                last_err = e
                msg = str(e)[:300]
                if attempt > self.cfg.elevenlabs_max_retries:
                    raise ElevenLabsError(f"TTS failed: {msg}")
                wait = 2 ** attempt * 3
                log.warning(
                    "TTS attempt %d failed: %s; retry in %ds",
                    attempt, msg, wait,
                )
                time.sleep(wait)

        raise ElevenLabsError(f"TTS exhausted retries: {last_err}")
