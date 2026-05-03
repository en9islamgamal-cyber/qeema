"""
engines/thumbnail_engine.py — VALUE / QEEMA v11.0 (Production)
====================================================================
Thumbnail generation using Pillow.

[Strategy]
- Programmatic (not LLM-generated): consistent branding across episodes
- Right-aligned title for RTL aesthetic
- Optional background_image (procedural fallback if missing)
- Output: high-quality JPEG at 1280×720 (YouTube standard)

[Arabic shaping]
We use arabic-reshaper + python-bidi where available (PIL doesn't shape
Arabic by default — letters appear isolated otherwise).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Tuple

from core.config import BrandingConfig, PathsConfig
from core.exceptions import VisualRenderError
from core.interfaces import ThumbnailBuilder

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Arabic shaping (graceful fallback if libs missing)
# ════════════════════════════════════════════════════════════════
def _shape_arabic(text: str) -> str:
    """Reshape and bidi-correct Arabic for PIL drawing."""
    try:
        import arabic_reshaper  # type: ignore
        from bidi.algorithm import get_display  # type: ignore
        return get_display(arabic_reshaper.reshape(text))
    except ImportError:
        # Fallback: return as-is (will look slightly off but not crash)
        logger.warning(
            "⚠️ arabic-reshaper / python-bidi not installed; thumbnails "
            "will use unshaped Arabic. Run: pip install arabic-reshaper python-bidi"
        )
        return text


# ════════════════════════════════════════════════════════════════
# ThumbnailEngine
# ════════════════════════════════════════════════════════════════
class ThumbnailEngine(ThumbnailBuilder):
    """Generates branded YouTube thumbnails."""

    SIZE: Tuple[int, int] = (1280, 720)
    GRADIENT_TOP = (255, 179, 71)     # warm sunset top
    GRADIENT_BOTTOM = (26, 26, 46)    # deep navy bottom
    GOLD = (255, 215, 0)
    WHITE = (255, 255, 255)
    SHADOW = (0, 0, 0)

    def __init__(
        self,
        *,
        paths: PathsConfig,
        branding: BrandingConfig,
    ) -> None:
        self._paths: PathsConfig = paths
        self._branding: BrandingConfig = branding
        paths.thumbnails.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        script: Any,
        episode_number: int,
        background_image: Optional[str] = None,
    ) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageFilter  # type: ignore
        except ImportError as e:
            raise VisualRenderError(
                "Pillow not installed; cannot create thumbnails", cause=e
            ) from e

        out_path = self._paths.thumbnails / f"ep_{episode_number:03d}.jpg"

        # ── 1. Background
        img = self._build_background(background_image)

        # ── 2. Vignette overlay (darken edges so text pops)
        img = self._apply_vignette(img)

        # ── 3. Text
        draw = ImageDraw.Draw(img, mode="RGBA")
        surah_name = getattr(script, "surah_name", "") or "القرآن"
        ep_label = f"الحلقة {episode_number}"
        title_ar = f"سورة {surah_name}"

        title_font = self._load_font(140)
        ep_font = self._load_font(72)
        brand_font = self._load_font(64)

        # Right-aligned for RTL aesthetic
        margin_x = 80
        margin_y = 150

        title_shaped = _shape_arabic(title_ar)
        ep_shaped = _shape_arabic(ep_label)
        brand_ar = _shape_arabic(self._branding.channel_name_ar)

        # Title (huge, gold)
        self._draw_with_shadow(
            draw, (margin_x, margin_y), title_shaped,
            title_font, self.GOLD,
        )

        # Episode label (white)
        self._draw_with_shadow(
            draw, (margin_x, margin_y + 180), ep_shaped,
            ep_font, self.WHITE,
        )

        # Brand at bottom-right
        bw, bh = draw.textbbox((0, 0), brand_ar, font=brand_font)[2:]
        self._draw_with_shadow(
            draw,
            (self.SIZE[0] - bw - margin_x, self.SIZE[1] - bh - 70),
            brand_ar, brand_font, self.GOLD,
        )

        # ── 4. Save (JPEG quality 92, optimized)
        img.convert("RGB").save(out_path, "JPEG", quality=92, optimize=True)
        logger.info(f"✅ thumbnail: {out_path}")
        return str(out_path)

    # ───────────────────────────────────────────────────────────
    # Helpers
    # ───────────────────────────────────────────────────────────
    def _load_font(self, size: int):
        from PIL import ImageFont  # type: ignore
        font_path = self._paths.amiri_font
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except Exception as e:
                logger.warning(f"⚠️ Custom font load failed: {e}")
        # Try system Arabic fonts
        for candidate in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ):
            if Path(candidate).exists():
                try:
                    return ImageFont.truetype(candidate, size=size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def _build_background(self, source: Optional[str]):
        from PIL import Image, ImageFilter  # type: ignore
        if source and Path(source).exists():
            try:
                src = Image.open(source).convert("RGB")
                # Cover-fit
                src = src.resize(self.SIZE, Image.LANCZOS)
                src = src.filter(ImageFilter.GaussianBlur(radius=3))
                return src
            except Exception as e:
                logger.warning(f"⚠️ background load failed, using gradient: {e}")
        return self._gradient_background()

    def _gradient_background(self):
        from PIL import Image  # type: ignore
        img = Image.new("RGB", self.SIZE)
        w, h = self.SIZE
        # Vertical linear gradient
        for y in range(h):
            t = y / h
            r = int(self.GRADIENT_TOP[0] * (1 - t) + self.GRADIENT_BOTTOM[0] * t)
            g = int(self.GRADIENT_TOP[1] * (1 - t) + self.GRADIENT_BOTTOM[1] * t)
            b = int(self.GRADIENT_TOP[2] * (1 - t) + self.GRADIENT_BOTTOM[2] * t)
            for x in range(w):
                img.putpixel((x, y), (r, g, b))
        return img

    def _apply_vignette(self, img):
        from PIL import Image, ImageDraw  # type: ignore
        # Soft radial darkening at edges
        w, h = img.size
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        # Strong darkening at bottom (where brand text goes)
        for y in range(h - 200, h):
            alpha = int(((y - (h - 200)) / 200) * 140)
            draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
        img = img.convert("RGBA")
        img.alpha_composite(overlay)
        return img

    def _draw_with_shadow(
        self,
        draw,
        pos: Tuple[int, int],
        text: str,
        font,
        color: Tuple[int, int, int],
    ) -> None:
        x, y = pos
        # Shadow
        for dx, dy in ((3, 3), (-1, 3), (3, -1)):
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 200))
        # Main text
        draw.text((x, y), text, font=font, fill=color)
