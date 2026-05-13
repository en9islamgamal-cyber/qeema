"""
video/thumbnail_builder.py
====================================================================
Builds YouTube thumbnails from Leonardo images using a fixed template.

Template design:
  ┌─────────────────────────────────────┐
  │                                     │
  │       (Leonardo background          │
  │        image, scaled to fill)       │
  │                                     │
  │   ╔═════════════════════════════╗   │
  │   ║  Dark gradient overlay       ║   │  <- bottom 40% darker
  │   ║   ┌───┐                      ║   │
  │   ║   │LOG│  TITLE TEXT          ║   │  <- logo + title
  │   ║   │ O │  (large Arabic       ║   │     bottom-aligned
  │   ║   └───┘  bold font)          ║   │
  │   ╚═════════════════════════════╝   │
  └─────────────────────────────────────┘

YouTube thumbnail spec:
  - Resolution: 1280 × 720 (16:9)
  - Format: JPG (smaller, better for upload)
  - File size: < 2MB
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import List, Optional

from core.config import LOGO_PATH, FONT_PATH, TEMP_DIR, get_pipeline_config


log = logging.getLogger(__name__)


class ThumbnailError(Exception):
    pass


# Thumbnail dimensions
THUMB_WIDTH = 1280
THUMB_HEIGHT = 720

# Logo settings on thumbnail
THUMB_LOGO_WIDTH = 400  # was 240 — much bigger
THUMB_LOGO_MARGIN_X = 60
THUMB_LOGO_MARGIN_Y = 60

# Title text settings
TITLE_MAX_CHARS = 60     # truncate longer titles
TITLE_FONT_SIZE = 56     # large but readable
TITLE_COLOR = "white"
TITLE_BORDER_COLOR = "black"
TITLE_BORDER_WIDTH = 4   # thick outline for visibility


def build_thumbnail(
    base_image: Path,
    output: Path,
    title: Optional[str] = None,
    add_logo: bool = True,
) -> Path:
    """
    Build a single thumbnail from a Leonardo image with template design.

    Steps:
      1. Resize base image to 1280×720 (cover, no distortion)
      2. Add gradient overlay (darker bottom half) for text visibility
      3. Overlay channel logo (bottom-left, large)
      4. Add title text (bottom-right of logo, large white with black outline)
    """
    cfg = get_pipeline_config()
    output.parent.mkdir(parents=True, exist_ok=True)

    work_dir = TEMP_DIR / "thumbnails"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize title for FFmpeg (escape special characters)
    safe_title = ""
    if title:
        safe_title = title[:TITLE_MAX_CHARS]
        # Escape characters that break drawtext filter
        # FFmpeg drawtext is finicky with Arabic; we use a textfile approach
        title_file = work_dir / f"title_{output.stem}.txt"
        title_file.write_text(safe_title, encoding="utf-8")

    # ─── Build the FFmpeg filter chain ──────────────────────────
    # Layers (bottom to top):
    #   [0:v] = base image
    #   [1:v] = logo PNG
    #   Output combines all layers

    has_logo = add_logo and LOGO_PATH.exists()
    has_font = FONT_PATH.exists()

    inputs = ["-i", str(base_image)]
    if has_logo:
        inputs.extend(["-i", str(LOGO_PATH)])

    # Build filter steps
    filter_parts = []

    # Step 1: scale + crop base image to 1280×720
    filter_parts.append(
        f"[0:v]scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}[base]"
    )

    # Step 2: add bottom gradient overlay (dark) for text readability
    # Create a vertical gradient from transparent (top) to dark (bottom)
    filter_parts.append(
        f"[base]drawbox=x=0:y={THUMB_HEIGHT//2}:w={THUMB_WIDTH}:h={THUMB_HEIGHT//2}:"
        f"color=black@0.55:t=fill[withgradient]"
    )

    last_label = "withgradient"

    # Step 3: overlay logo (if present)
    if has_logo:
        filter_parts.append(
            f"[1:v]scale={THUMB_LOGO_WIDTH}:-1[logo]"
        )
        # Place logo in bottom-LEFT corner
        logo_x = THUMB_LOGO_MARGIN_X
        logo_y = THUMB_HEIGHT - THUMB_LOGO_WIDTH - THUMB_LOGO_MARGIN_Y
        filter_parts.append(
            f"[{last_label}][logo]overlay=x={logo_x}:y={logo_y}[withlogo]"
        )
        last_label = "withlogo"

    # Step 4: add title text (if present and font available)
    if safe_title and has_font:
        # Position text to the RIGHT of the logo, vertically centered with logo
        text_x = THUMB_LOGO_MARGIN_X + THUMB_LOGO_WIDTH + 40
        text_y = THUMB_HEIGHT - THUMB_LOGO_WIDTH // 2 - TITLE_FONT_SIZE
        text_max_width = THUMB_WIDTH - text_x - THUMB_LOGO_MARGIN_X

        # Use textfile to handle Arabic correctly
        title_path = (work_dir / f"title_{output.stem}.txt").resolve()
        # Escape backslashes for FFmpeg
        title_path_escaped = str(title_path).replace("\\", "/")

        filter_parts.append(
            f"[{last_label}]drawtext="
            f"fontfile='{FONT_PATH}':"
            f"textfile='{title_path_escaped}':"
            f"fontcolor={TITLE_COLOR}:"
            f"fontsize={TITLE_FONT_SIZE}:"
            f"bordercolor={TITLE_BORDER_COLOR}:"
            f"borderw={TITLE_BORDER_WIDTH}:"
            f"x={text_x}:y={text_y}:"
            f"line_spacing=10[final]"
        )
        last_label = "final"

    filter_complex = ";".join(filter_parts)
    output_label = f"[{last_label}]"

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", output_label,
        "-q:v", "3",
        "-frames:v", "1",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning(
            "Template thumbnail failed (%s); falling back to simple version",
            r.stderr[:200],
        )
        return _build_thumbnail_simple(base_image, output, add_logo)

    log.info("✓ Thumbnail built (template): %s", output.name)
    return output


def _build_thumbnail_simple(
    base_image: Path, output: Path, add_logo: bool,
) -> Path:
    """
    Fallback: simple resize + logo overlay, no text.
    Used when font is unavailable or text overlay fails.
    """
    if add_logo and LOGO_PATH.exists():
        filter_complex = (
            f"[0:v]scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}[main];"
            f"[1:v]scale={THUMB_LOGO_WIDTH}:-1,format=rgba,"
            f"colorchannelmixer=aa=0.95[logo];"
            f"[main][logo]overlay=x={THUMB_LOGO_MARGIN_X}:"
            f"y=H-h-{THUMB_LOGO_MARGIN_Y}"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(base_image),
            "-i", str(LOGO_PATH),
            "-filter_complex", filter_complex,
            "-q:v", "3",
            "-frames:v", "1",
            str(output),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(base_image),
            "-vf",
            f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}",
            "-q:v", "3",
            "-frames:v", "1",
            str(output),
        ]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ThumbnailError(f"Simple thumbnail failed: {r.stderr[:400]}")

    log.info("✓ Thumbnail built (simple): %s", output.name)
    return output


def build_thumbnails_batch(
    base_images: List[Path],
    output_dir: Path,
    title: Optional[str] = None,
) -> List[Path]:
    """Build multiple thumbnails (one per Leonardo variant)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for i, img in enumerate(base_images, start=1):
        out = output_dir / f"thumbnail_v{i}.jpg"
        paths.append(build_thumbnail(img, out, title=title))
    return paths
