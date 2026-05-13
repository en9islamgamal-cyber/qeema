"""
assets_engines/ayah_text_fetcher.py
====================================================================
Fetches authentic Quranic ayah text from quran.com API.

CRITICAL: This is the ONLY way ayah text enters the pipeline.
We NEVER let any LLM (Gemini, Claude, etc.) generate or modify
ayah text. Every character of every verse comes from a verified
API call to a trusted source.

Why so strict? Because hallucination on Quranic text would be a
catastrophic error: not just a bug, but a religious harm. Even
if Gemini gets the verse right 99.99% of the time, that 0.01%
of mangled verses going out as videos is unacceptable.

Source: quran.com API (api.quran.com/api/v4)
Fallback: alquran.cloud API
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

from core.models import VerifiedAyah


log = logging.getLogger(__name__)

QURAN_COM_BASE = "https://api.quran.com/api/v4"
ALQURAN_CLOUD_BASE = "https://api.alquran.cloud/v1"
HTTP_TIMEOUT_SEC = 15
MAX_RETRIES = 3


class AyahTextFetchError(Exception):
    """Raised when we can't get verified ayah text from any source."""


def fetch_ayahs(
    surah_number: int,
    start_ayah: int,
    end_ayah: int,
) -> List[VerifiedAyah]:
    """
    Fetch a contiguous range of ayahs from a surah.

    Tries quran.com first, falls back to alquran.cloud.
    Returns VerifiedAyah objects in ayah-number order.

    Raises AyahTextFetchError if all sources fail.
    """
    if end_ayah < start_ayah:
        raise ValueError(
            f"end_ayah ({end_ayah}) must be >= start_ayah ({start_ayah})"
        )

    log.info(
        "Fetching ayahs: surah=%d, ayahs=%d-%d",
        surah_number, start_ayah, end_ayah,
    )

    # Try quran.com first
    try:
        ayahs = _fetch_from_quran_com(surah_number, start_ayah, end_ayah)
        log.info("✓ Fetched %d ayahs from quran.com", len(ayahs))
        return ayahs
    except Exception as e:
        log.warning("quran.com failed: %s. Trying fallback...", e)

    # Fallback: alquran.cloud
    try:
        ayahs = _fetch_from_alquran_cloud(surah_number, start_ayah, end_ayah)
        log.info("✓ Fetched %d ayahs from alquran.cloud", len(ayahs))
        return ayahs
    except Exception as e:
        log.error("alquran.cloud also failed: %s", e)

    raise AyahTextFetchError(
        f"All sources failed for surah {surah_number}, "
        f"ayahs {start_ayah}-{end_ayah}"
    )


# ════════════════════════════════════════════════════════════════════
# quran.com — primary source
# ════════════════════════════════════════════════════════════════════

def _fetch_from_quran_com(
    surah_number: int, start_ayah: int, end_ayah: int,
) -> List[VerifiedAyah]:
    """
    Use quran.com /verses/by_chapter endpoint.
    Returns the Uthmani script (script_id=1).
    """
    url = f"{QURAN_COM_BASE}/verses/by_chapter/{surah_number}"
    params = {
        "from": start_ayah,
        "to": end_ayah,
        "fields": "text_uthmani",
        "per_page": end_ayah - start_ayah + 1,
        "language": "en",  # for the wrapper, the text is Arabic
    }

    response = _http_get_with_retry(url, params)
    data = response.json()

    verses = data.get("verses", [])
    if not verses:
        raise AyahTextFetchError(f"No verses returned for surah {surah_number}")

    result: List[VerifiedAyah] = []
    for v in verses:
        verse_key = v.get("verse_key", "")  # e.g. "113:1"
        try:
            s_num, a_num = map(int, verse_key.split(":"))
        except (ValueError, AttributeError):
            log.warning("Bad verse_key from quran.com: %r", verse_key)
            continue

        text = v.get("text_uthmani", "").strip()
        if not text:
            raise AyahTextFetchError(
                f"Empty text for verse {verse_key} from quran.com"
            )

        result.append(VerifiedAyah(
            surah=s_num, number=a_num, text=text,
            audio_url=_build_husary_audio_url(s_num, a_num),
        ))

    # Validate we got the expected range
    expected = list(range(start_ayah, end_ayah + 1))
    got = [a.number for a in result]
    if got != expected:
        raise AyahTextFetchError(
            f"Expected ayahs {expected}, got {got}"
        )

    return result


# ════════════════════════════════════════════════════════════════════
# alquran.cloud — fallback
# ════════════════════════════════════════════════════════════════════

def _fetch_from_alquran_cloud(
    surah_number: int, start_ayah: int, end_ayah: int,
) -> List[VerifiedAyah]:
    """
    Use alquran.cloud /surah/{n}/quran-uthmani endpoint.
    Filters the ayah range client-side.
    """
    url = f"{ALQURAN_CLOUD_BASE}/surah/{surah_number}/quran-uthmani"
    response = _http_get_with_retry(url, params=None)
    data = response.json()

    if data.get("code") != 200:
        raise AyahTextFetchError(
            f"alquran.cloud returned code {data.get('code')}"
        )

    all_ayahs = data["data"]["ayahs"]
    filtered = [
        ay for ay in all_ayahs
        if start_ayah <= ay["numberInSurah"] <= end_ayah
    ]

    if len(filtered) != (end_ayah - start_ayah + 1):
        raise AyahTextFetchError(
            f"Expected {end_ayah - start_ayah + 1} ayahs from alquran.cloud, "
            f"got {len(filtered)}"
        )

    result: List[VerifiedAyah] = []
    for ay in filtered:
        text = ay.get("text", "").strip()
        if not text:
            raise AyahTextFetchError(
                f"Empty text from alquran.cloud at ayah {ay.get('numberInSurah')}"
            )
        n = ay["numberInSurah"]
        result.append(VerifiedAyah(
            surah=surah_number, number=n, text=text,
            audio_url=_build_husary_audio_url(surah_number, n),
        ))

    return result


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _build_husary_audio_url(surah_number: int, ayah_number: int) -> str:
    """Construct the everyayah.com URL for Husary's recitation."""
    return (
        f"https://everyayah.com/data/Husary_64kbps/"
        f"{surah_number:03d}{ayah_number:03d}.mp3"
    )


def _http_get_with_retry(url: str, params: Optional[dict]) -> requests.Response:
    """GET with up to MAX_RETRIES, exponential backoff."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, timeout=HTTP_TIMEOUT_SEC,
                headers={"User-Agent": "QEEMA-v2/1.0"},
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            sleep_sec = min(2 ** (attempt - 1), 10)
            log.warning(
                "HTTP attempt %d/%d failed (%s); retrying in %ds",
                attempt, MAX_RETRIES, e, sleep_sec,
            )
            time.sleep(sleep_sec)

    raise AyahTextFetchError(f"HTTP failed after {MAX_RETRIES} retries: {last_err}")
