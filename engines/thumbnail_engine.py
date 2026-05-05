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
from typing import Any, List, Optional, Tuple

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

    SIZE: Tuple[int, int] = (1920, 1080)  # v18: matches video, allowed by YouTube
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
        """Create primary thumbnail (variant A: title-focused)."""
        return self._create_variant(
            script, episode_number, background_image,
            variant="A",
        )

    def create_variants(
        self,
        script: Any,
        episode_number: int,
        background_image: Optional[str] = None,
    ) -> List[str]:
        """
        v18 NEW: Create 3 thumbnail variants for YouTube Test & Compare.

        YouTube allows uploading 3 thumbnails per video; YouTube auto-tests
        which gets highest CTR and uses it.

        Variants:
            A. Title-dominant (large arabic typography)
            B. Question-hook (intro question text + accent)
            C. Visual-dominant (image + minimal text)
        """
        return [
            self._create_variant(script, episode_number, background_image, "A"),
            self._create_variant(script, episode_number, background_image, "B"),
            self._create_variant(script, episode_number, background_image, "C"),
        ]

    def _create_variant(
        self,
        script: Any,
        episode_number: int,
        background_image: Optional[str],
        variant: str,
    ) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageFilter  # type: ignore
        except ImportError as e:
            raise VisualRenderError(
                "Pillow not installed; cannot create thumbnails", cause=e
            ) from e

        suffix = f"_{variant}" if variant != "A" else ""
        out_path = self._paths.thumbnails / f"ep_{episode_number:03d}{suffix}.jpg"

        # ── 1. Background
        img = self._build_background(background_image)
        # ── 2. Vignette overlay
        img = self._apply_vignette(img, strength=variant)
        # ── 3. Variant-specific composition
        draw = ImageDraw.Draw(img, mode="RGBA")
        surah_name = getattr(script, "surah_name", "") or "القرآن"
        title_ar = f"سورة {surah_name}"
        ep_label = f"الحلقة {episode_number}"

        if variant == "A":
            self._compose_title_dominant(
                draw, surah_name, title_ar, ep_label,
            )
        elif variant == "B":
            # Question-hook: pull intro_text first sentence
            intro = ""
            try:
                intro = getattr(script.intro_scene, "narrator_text", "")
            except Exception:
                pass
            # Take first ~6 words ending with question mark or period
            hook_short = self._extract_hook_phrase(intro)
            self._compose_question_hook(
                draw, hook_short, ep_label, surah_name,
            )
        elif variant == "C":
            self._compose_visual_dominant(
                draw, surah_name, ep_label,
            )
        else:
            raise ValueError(f"Unknown variant: {variant}")

        # ── 4. Save
        img.convert("RGB").save(
            out_path, "JPEG", quality=95, optimize=True
        )
        logger.info(f"✅ Thumbnail variant {variant} saved: {out_path.name}")
        return str(out_path)

    @staticmethod
    def _extract_hook_phrase(text: str, max_words: int = 7) -> str:
        """Extract a short curiosity phrase from intro for thumbnail."""
        if not text:
            return "تدبر القرآن"
        # Find first sentence
        for delim in ['؟', '.', '!', '،']:
            if delim in text:
                first = text.split(delim, 1)[0]
                if delim == '؟':
                    first += '؟'
                words = first.split()
                if len(words) <= max_words:
                    return first
                return ' '.join(words[:max_words]) + ('؟' if delim == '؟' else '...')
        # No delim — take first N words
        words = text.split()
        return ' '.join(words[:max_words]) + ('...' if len(words) > max_words else '')

    def _compose_title_dominant(
        self, draw, surah_name: str, title_ar: str, ep_label: str,
    ) -> None:
        """Variant A: Large title, traditional layout."""
        title_font = self._load_font(140)
        ep_font = self._load_font(72)
        brand_font = self._load_font(64)
        margin_x = 80
        margin_y = 150

        title_shaped = _shape_arabic(title_ar)
        ep_shaped = _shape_arabic(ep_label)
        brand_ar = _shape_arabic(self._branding.channel_name_ar)

        # Title (huge, gold)
        self._draw_with_shadow(
            draw, title_shaped, (margin_x, margin_y),
            title_font, "#FFD700", "right",
        )
        # Episode label
        self._draw_with_shadow(
            draw, ep_shaped, (margin_x, margin_y + 200),
            ep_font, "#FFFFFF", "right",
        )
        # Channel brand bottom-left
        self._draw_with_shadow(
            draw, brand_ar, (margin_x, 1080 - 130),
            brand_font, "#FFD700", "left",
        )

    def _compose_question_hook(
        self, draw, hook_phrase: str, ep_label: str, surah_name: str,
    ) -> None:
        """Variant B: Question-driven, big curiosity text."""
        hook_font = self._load_font(110)
        meta_font = self._load_font(58)

        hook_shaped = _shape_arabic(hook_phrase)
        meta_shaped = _shape_arabic(f"{ep_label} • سورة {surah_name}")

        # Hook centered vertically, large
        self._draw_with_shadow(
            draw, hook_shaped, (1920 // 2, 1080 // 2 - 60),
            hook_font, "#FFEB99", "center",
        )
        # Meta line below
        self._draw_with_shadow(
            draw, meta_shaped, (1920 // 2, 1080 // 2 + 80),
            meta_font, "#FFFFFF", "center",
        )

    def _compose_visual_dominant(
        self, draw, surah_name: str, ep_label: str,
    ) -> None:
        """Variant C: Minimal text, image gets focus."""
        small_title = self._load_font(80)
        tiny_font = self._load_font(48)

        title_shaped = _shape_arabic(surah_name)
        ep_shaped = _shape_arabic(ep_label)

        # Small title bottom-right
        self._draw_with_shadow(
            draw, title_shaped, (1920 - 80, 1080 - 200),
            small_title, "#FFD700", "right",
        )
        self._draw_with_shadow(
            draw, ep_shaped, (1920 - 80, 1080 - 110),
            tiny_font, "#FFFFFF", "right",
        )

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

    def _apply_vignette(self, img, strength: str = "A"):
        """Apply vignette overlay. Strength varies per variant."""
        from PIL import Image, ImageDraw  # type: ignore
        w, h = img.size
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        if strength == "B":
            # Variant B: stronger center spotlight (text is centered)
            for y in range(h):
                # Distance from vertical center (normalized)
                dy = abs(y - h // 2) / (h // 2)
                # Apply darkening proportional to distance
                alpha = int(min(180, dy * 200))
                draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
        elif strength == "C":
            # Variant C: minimal vignette (image dominant)
            for y in range(h - 250, h):
                alpha = int(((y - (h - 250)) / 250) * 100)
                draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
        else:
            # Variant A (default): bottom-only vignette
            for y in range(h - 200, h):
                alpha = int(((y - (h - 200)) / 200) * 140)
                draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))

        img = img.convert("RGBA")
        img.alpha_composite(overlay)
        return img

    def _draw_with_shadow(
        self,
        draw,
        text: str,
        pos,
        font,
        color,
        anchor: str = "left",
    ) -> None:
        """Draw text with shadow.

        v18: accepts hex color string OR RGB tuple, supports alignment.
        """
        # Convert hex to RGB if needed
        if isinstance(color, str):
            color = self._hex_to_rgb(color)

        x, y = pos
        # Determine anchor offset
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = font.getsize(text)[0] if hasattr(font, 'getsize') else 200

        if anchor == "right":
            x = x - tw
        elif anchor == "center":
            x = x - tw // 2

        # Shadow (slightly offset, semi-transparent black)
        for dx, dy in ((3, 3), (-1, 3), (3, -1)):
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 200))
        # Main text
        draw.text((x, y), text, font=font, fill=color)

    @staticmethod
    def _hex_to_rgb(hex_color: str):
        """Convert #RRGGBB to (R, G, B) tuple."""
        h = hex_color.lstrip('#')
        if len(h) == 6:
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
        return (255, 255, 255)
