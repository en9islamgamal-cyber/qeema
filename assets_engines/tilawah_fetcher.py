"""
assets_engines/tilawah_fetcher.py
====================================================================
Downloads authentic Quranic recitation (Husary) from everyayah.com.

We NEVER use AI/TTS for Quran recitation — that would be a religious
error. Recitation must come from a real, certified Qari.

The CDN at everyayah.com hosts hundreds of reciters; we use
Mahmoud Khalil Al-Husary (الحصري) by default — slow, clear,
mushaf-style, ideal for kids learning to memorize.

URL format:
   https://everyayah.com/data/Husary_64kbps/{surah:03d}{ayah:03d}.mp3
Example:
   surah 113, ayah 1 → 113001.mp3
   surah 1,   ayah 7 → 001007.mp3

We cache downloaded files in TEMP_DIR/tilawah to avoid re-downloading.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import List

import requests

from core.config import TILAWAH_CACHE_DIR, get_pipeline_config


log = logging.getLogger(__name__)

HTTP_TIMEOUT_SEC = 30
MAX_RETRIES = 3
BASMALA_SURAH = 1
BASMALA_AYAH = 1


class TilawahFetchError(Exception):
    """Raised when recitation audio can't be downloaded."""


def fetch_tilawah_for_episode(
    surah_number: int,
    start_ayah: int,
    end_ayah: int,
    include_basmala: bool = True,
) -> List[Path]:
    """
    Download all recitation MP3s for the ayah range.

    If include_basmala=True AND start_ayah=1, prepends the basmala
    (000001 isn't a real ayah — we use 001001, the first ayah of
    Al-Fatiha which IS the basmala in script).

    Actually we use a dedicated basmala recording:
       Husary's basmala = Al-Fatiha ayah 1 = 001001.mp3
    For surahs that don't start with basmala in mushaf script
    (e.g. At-Tawba), we skip it. But for kids' channel, it's
    safer to always include basmala UNLESS start_ayah > 1.

    Logic:
      - If start_ayah == 1 → include basmala first (because the
        surah starts from its beginning; the recitation natively
        includes basmala for most surahs anyway).
      - If start_ayah > 1 → skip basmala (we're mid-surah).

    Returns: list of local file paths in order.
    """
    TILAWAH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = get_pipeline_config()

    files: List[Path] = []

    # Optionally prepend basmala (Al-Fatiha 001:001) for surah-openings
    # NOTE: most recitations of surahs (other than At-Tawba) on
    # everyayah.com include basmala in their FIRST ayah file natively.
    # So we don't need a separate basmala file — fetching 113001 will
    # include "بسم الله الرحمن الرحيم قل أعوذ برب الفلق".
    # We just need to honor start_ayah=1 vs start_ayah>1.

    for ayah_num in range(start_ayah, end_ayah + 1):
        url = (
            f"{cfg.tilawah_base_url}/"
            f"{surah_number:03d}{ayah_num:03d}.mp3"
        )
        path = _download_with_cache(url)
        files.append(path)

    log.info(
        "✓ Tilawah ready: %d files (surah=%d, ayahs %d-%d)",
        len(files), surah_number, start_ayah, end_ayah,
    )
    return files


def concat_tilawah_files(files: List[Path], output: Path) -> Path:
    """
    Concatenate the per-ayah tilawah MP3s into ONE big file
    for the opening and closing recitations.

    Uses ffmpeg's concat demuxer (most reliable for MP3 chunks).
    """
    if not files:
        raise TilawahFetchError("No files to concatenate")

    output.parent.mkdir(parents=True, exist_ok=True)

    # Build concat list file
    list_path = output.parent / f"{output.stem}_concat.txt"
    with list_path.open("w") as f:
        for p in files:
            f.write(f"file '{p.resolve()}'\n")

    import subprocess
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(output),
    ]
    log.info("Concatenating %d tilawah files → %s", len(files), output.name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TilawahFetchError(
            f"ffmpeg concat failed: {result.stderr[:500]}"
        )

    list_path.unlink(missing_ok=True)
    return output


# ════════════════════════════════════════════════════════════════════
# Internal: download with disk cache
# ════════════════════════════════════════════════════════════════════

def _download_with_cache(url: str) -> Path:
    """
    Download a file if not already cached. Cache key = URL hash.
    Returns the local cached path.
    """
    cache_name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".mp3"
    local_path = TILAWAH_CACHE_DIR / cache_name

    if local_path.exists() and local_path.stat().st_size > 1024:
        log.debug("Cache hit: %s", local_path.name)
        return local_path

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.debug("Downloading: %s", url)
            r = requests.get(url, timeout=HTTP_TIMEOUT_SEC, stream=True)
            r.raise_for_status()

            tmp = local_path.with_suffix(".tmp")
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            tmp.rename(local_path)

            size = local_path.stat().st_size
            log.debug("✓ Downloaded %d bytes: %s", size, local_path.name)
            return local_path

        except Exception as e:
            last_err = e
            sleep_sec = min(2 ** (attempt - 1), 10)
            log.warning(
                "Download attempt %d/%d failed: %s; retry in %ds",
                attempt, MAX_RETRIES, e, sleep_sec,
            )
            time.sleep(sleep_sec)

    raise TilawahFetchError(
        f"Failed to download {url} after {MAX_RETRIES} attempts: {last_err}"
    )
