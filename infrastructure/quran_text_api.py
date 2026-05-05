"""
infrastructure/quran_text_api.py — VALUE / QEEMA v11.0 (Production)
=====================================================================
Fetches verified Quranic ayah text from a trusted API.

[Source]
api.qurancdn.com (QuranFoundation) — provides Uthmani text.

[Why an entire module?]
- Crucial that ayah text is NEVER hallucinated by an LLM
- Single source of truth + retry logic
- Could be swapped (e.g., to API.AlQuran.cloud) without changing engines
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

from core.exceptions import NetworkError, ValidationError
from core.resilience import RetryPolicy, retry_with_backoff

logger = logging.getLogger(__name__)


QURAN_API_BASE: str = "https://api.qurancdn.com/api/qdc/verses/by_chapter"


@retry_with_backoff(
    RetryPolicy(max_attempts=3, retry_on=(NetworkError,))
)
def fetch_verified_ayahs(
    surah: int,
    start: int,
    end: int,
) -> List[Dict[str, Any]]:
    """
    Fetch ayahs[start..end] for the given surah.

    Returns a list of dicts:
        [{"surah": 1, "number": 1, "text": "بسم الله ..."}, ...]

    Raises:
        NetworkError    : on connectivity issues (will retry)
        ValidationError : on count mismatch or empty payload
    """
    if not (1 <= surah <= 114):
        raise ValidationError(f"Invalid surah: {surah}")
    if not (1 <= start <= end):
        raise ValidationError(
            f"Invalid range: start={start}, end={end}"
        )

    url = (
        f"{QURAN_API_BASE}/{surah}"
        f"?words=false&fields=text_uthmani&per_page=300"
    )
    try:
        resp = requests.get(url, timeout=15)
    except requests.Timeout as e:
        raise NetworkError(f"Quran API timeout: {e}", cause=e) from e
    except requests.RequestException as e:
        raise NetworkError(f"Quran API network: {e}", cause=e) from e

    if resp.status_code != 200:
        raise NetworkError(
            f"Quran API HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        raise NetworkError(f"Quran API returned non-JSON: {e}", cause=e) from e

    ayahs: List[Dict[str, Any]] = []
    for verse in data.get("verses", []):
        try:
            num = int(verse["verse_key"].split(":")[1])
        except (KeyError, ValueError, IndexError):
            continue
        if start <= num <= end:
            text = (verse.get("text_uthmani") or "").strip()
            if text:
                ayahs.append({
                    "surah": surah,
                    "number": num,
                    "text": text,
                })

    expected = end - start + 1
    if len(ayahs) != expected:
        raise ValidationError(
            f"Quran API count mismatch for surah {surah}: "
            f"expected {expected} ayahs, got {len(ayahs)}"
        )

    return ayahs
