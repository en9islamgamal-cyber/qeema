"""
infrastructure/quran_sources.py — VALUE / QEEMA v22.5 — Quran audio CDN sources
=====================================================================
Quran audio CDN sources.

[Sources]
- everyayah.com    : multiple reciters (Alafasy, Husary, ...)
- islamic.network  : Alafasy variant
- quran.com        : Alafasy variant

[Strategy]
Each source is independent. The QuranAudioFetcher (in voice_engine.py)
wires them into a ProviderPool with circuit breakers, so a failing
CDN won't slow down subsequent calls.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Set

import requests

from core.exceptions import NetworkError, TransientError
from core.interfaces import (
    QuranAudioRequest,
    QuranAudioResult,
    QuranAudioSource,
)
from core.resilience import RetryPolicy, retry_with_backoff
from infrastructure.audio_utils import (
    get_audio_duration,
    validate_audio_file,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Base implementation
# ════════════════════════════════════════════════════════════════
class _UrlTemplateSource(QuranAudioSource):
    """
    Generic source backed by a URL template.

    Template variables: {surah}, {ayah}.
    Supported reciters declared in __init__.
    """

    def __init__(
        self,
        name: str,
        url_template: str,
        reciters: Set[str],
    ) -> None:
        self.name: str = name
        self.base_url: str = url_template
        self._reciters: Set[str] = {r.lower() for r in reciters}

    def supports(self, reciter: str) -> bool:
        return reciter.lower() in self._reciters

    @retry_with_backoff(
        RetryPolicy(max_attempts=2, retry_on=(NetworkError, TransientError))
    )
    def fetch(self, request: QuranAudioRequest) -> QuranAudioResult:
        if not self.supports(request.reciter):
            raise TransientError(
                f"{self.name} doesn't support reciter '{request.reciter}'"
            )

        url = self.base_url.format(surah=request.surah, ayah=request.ayah)
        try:
            resp = requests.get(
                url,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 QeemaPipeline/11"},
            )
        except requests.Timeout as e:
            raise NetworkError(f"{self.name} timeout: {e}", cause=e) from e
        except requests.RequestException as e:
            raise NetworkError(f"{self.name} network: {e}", cause=e) from e

        if resp.status_code != 200:
            raise NetworkError(f"{self.name} HTTP {resp.status_code}")
        if len(resp.content) < 1000:
            raise NetworkError(
                f"{self.name} returned tiny payload ({len(resp.content)} bytes)"
            )

        out = Path(request.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(resp.content)

        if not validate_audio_file(str(tmp), min_duration=0.3):
            tmp.unlink(missing_ok=True)
            raise NetworkError(f"{self.name} returned invalid audio")

        tmp.replace(out)
        return QuranAudioResult(
            output_path=str(out),
            duration_sec=get_audio_duration(str(out)),
            source=self.name,
            cached=False,
        )


# ════════════════════════════════════════════════════════════════
# Concrete sources
# ════════════════════════════════════════════════════════════════
def default_sources() -> list[QuranAudioSource]:
    """
    Standard set of CDN sources, ordered by reliability/quality.

    Note: Alafasy is the default reciter for QEEMA channel.
    """
    return [
        _UrlTemplateSource(
            name="everyayah_alafasy",
            url_template=(
                "https://everyayah.com/data/Alafasy_128kbps/"
                "{surah:03d}{ayah:03d}.mp3"
            ),
            reciters={"alafasy"},
        ),
        _UrlTemplateSource(
            name="islamic_network_alafasy",
            url_template=(
                "https://cdn.islamic.network/quran/audio/128/ar.alafasy/"
                "{surah}_{ayah}.mp3"
            ),
            reciters={"alafasy"},
        ),
        _UrlTemplateSource(
            name="quran_com_alafasy",
            url_template=(
                "https://verses.quran.com/Alafasy/mp3/"
                "{surah:03d}{ayah:03d}.mp3"
            ),
            reciters={"alafasy"},
        ),
        _UrlTemplateSource(
            name="everyayah_husary",
            url_template=(
                "https://everyayah.com/data/Husary_128kbps/"
                "{surah:03d}{ayah:03d}.mp3"
            ),
            reciters={"husary"},
        ),
    ]
