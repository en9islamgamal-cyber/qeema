"""
video/thumbnail_builder.py
====================================================================
Builds YouTube thumbnails from Leonardo images + the channel logo.

Per spec: 3 thumbnail variants are generated (for A/B testing).
Each gets the channel logo overlaid in a corner.

YouTube thumbnail spec:
  - Resolution: 1280 × 720 minimum
  - Aspect: 16:9
  - File size: < 2MB
  - Format: JPG, PNG, or GIF
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import List

from core.config import LOGO_PATH, TEMP_DIR, get_pipeline_config


log = logging.getLogger(__name__)


class ThumbnailError(Exception):
    pass


def build_thumbnail(
    base_image: Path,
    output: Path,
    add_logo: bool = True,
) -> Path:
    """
    Build a single thumbnail from a Leonardo image.

    Resizes to 1280×720, optionally overlays logo, exports as JPG.
    """
    cfg = get_pipeline_config()
    output.parent.mkdir(parents=True, exist_ok=True)

    if add_logo and LOGO_PATH.exists():
        # Logo in bottom-right corner, slightly larger than video watermark
        logo_w = 240
        margin = 40

        filter_complex = (
            f"[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
            f"crop=1280:720[main];"
            f"[1:v]scale={logo_w}:-1,format=rgba,"
            f"colorchannelmixer=aa=0.85[logo];"
            f"[main][logo]overlay=x=W-w-{margin}:y=H-h-{margin}"
        )

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(base_image),
            "-i", str(LOGO_PATH),
            "-filter_complex", filter_complex,
            "-q:v", "3",  # high quality JPG
            "-frames:v", "1",
            str(output),
        ]
    else:
        # Just resize, no logo
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(base_image),
            "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
            "-q:v", "3",
            "-frames:v", "1",
            str(output),
        ]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ThumbnailError(f"Thumbnail build failed: {r.stderr[:400]}")

    log.info("✓ Thumbnail built: %s", output.name)
    return output


def build_thumbnails_batch(
    base_images: List[Path], output_dir: Path,
) -> List[Path]:
    """Build multiple thumbnails (one per Leonardo variant)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for i, img in enumerate(base_images, start=1):
        out = output_dir / f"thumbnail_v{i}.jpg"
        paths.append(build_thumbnail(img, out))
    return paths
