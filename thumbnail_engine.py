from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Optional, Set

from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from config import Paths
from models import EpisodeScript

logger = logging.getLogger(__name__)


def _get_font(size: int, bold: bool = True, preferred: str = "Amiri-Bold"):
    try:
        import arabic_reshaper  # noqa
        from bidi.algorithm import get_display  # noqa

        base_path = Paths.FONTS / preferred
        if base_path.exists():
            return ImageFont.truetype(str(base_path), size)

        for ext in ["*.ttf", "*.otf"]:
            for p in Paths.FONTS.glob(ext):
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass

        system_fonts = [
            "/usr/share/fonts/truetype/arabic/Amiri-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
        ]
        for p in system_fonts:
            if Path(p).exists():
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
        return ImageFont.load_default()
    except Exception:
        return None


def _shape_arabic(text: str) -> str:
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _smart_truncate(title: str, max_words: int = 6) -> str:
    parts = title.replace("
", " ").strip().split()
    if len(parts) > max_words:
        return " ".join(parts[:max_words]) + "…"
    return title


def _fit_text_width(draw, text: str, max_width: int, start_size: int, font_name: Optional[str] = None) -> Tuple[ImageFont, int]:
    size = start_size
    font = None
    while size > 24:
        try:
            font = _get_font(size, bold=True, preferred=font_name)
            if font is None:
                break
            bbox = draw.textbbox((0, 0), text, font=font)
            if bbox[2] - bbox[0] <= max_width:
                return font, size
        except Exception:
            pass
        size -= 4
    return font or ImageFont.load_default(), size


_COLOR_PALETTE = {
    "bg_dark_blue":  (20,  30,  48),
    "bg_gradient_dark": (10, 15, 25),
    "badge_gold":   (255, 215,  0, 230),
    "badge_gold_border": (255, 255, 255, 200),
    "badge_gold_text_bg": (20, 20, 40, 255),
    "title_text":   (255, 255, 255, 255),
    "title_stroke": (0,   0,   0,   230),
    "ep_badge_bg":  (255, 200,  0, 255),
    "ep_badge_text": (20, 20, 30, 255),
    "ep_badge_stroke": (255, 255, 255, 80),
}


class ThumbnailEngine:
    SIZE = (1280, 720)  # 16:9 YouTube, safe for mobile [web:43][web:46]

    def _draw_text_stroke(
        self,
        draw: ImageDraw,
        pos: tuple[float, float],
        text: str,
        font: ImageFont,
        text_color: tuple[int, ...],
        stroke_color: tuple[int, ...],
        stroke_width: int = 4,
        offset: int = 1,
    ):
        x, y = pos
        # فقط الإطارات الأساسية حول النص (أعلى/أسفل/يمين/يسار) لتقليل الفراغات بين الحروف [web:38]
        directions = [(-offset, 0), (offset, 0), (0, -offset), (0, offset)]
        for dx, dy in directions:
            draw.text((x + dx, y + dy), text, font=font, fill=stroke_color)
        draw.text((x, y), text, font=font, fill=text_color)

    def _build_gradient_mask(self, size: tuple[int, int]) -> Image.Image:
        W, H = size
        gradient = Image.new("RGBA", size, color=_COLOR_PALETTE["bg_gradient_dark"])
        draw = ImageDraw.Draw(gradient)
        for y in range(H):
            alpha = int((y / H) ** 1.5 * 220)
            draw.line([(0, y), (W, y)], fill=(*_COLOR_PALETTE["bg_gradient_dark"][:3], 255 * (y / H)))
        return gradient

    def create(
        self,
        script: EpisodeScript,
        episode_num: int,
        scene_image: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> str:
        try:
            from PIL.Image import Image
            import arabic_reshaper
            from bidi.algorithm import get_display
        except ImportError:
            logger.warning("PIL, arabic_reshaper or bidi not available — skipping thumbnail")
            return ""

        W, H = self.SIZE
        out = output_path or str(Paths.THUMBNAILS / f"ep_{episode_num:03d}.jpg")
        Path(out).parent.mkdir(parents=True, exist_ok=True)

        # 1. خلفية الإنفوجرافيك
        if scene_image and Path(scene_image).exists():
            bg = Image.open(scene_image).convert("RGBA").resize(self.SIZE, Image.LANCZOS)
            bg = ImageEnhance.Color(bg).enhance(1.2)
            bg = ImageEnhance.Contrast(bg).enhance(1.1)
        else:
            bg = Image.new("RGBA", self.SIZE, color=_COLOR_PALETTE["bg_dark_blue"])

        # 2. تدرج أسود من الأسفل للأعلى للحفاظ على سطوع الربع العلوي
        gradient = self._build_gradient_mask(self.SIZE)
        bg = Image.alpha_composite(bg, gradient)
        draw = ImageDraw.Draw(bg, "RGBA")

        # 3. شارة السورة (البادج الذهبي الأعلى اليسار – موضة القنوات الكبيرة)
        badge_w, badge_h = 320, 68
        badge_x, badge_y = 30, 20
        draw.rounded_rectangle(
            [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
            radius=16,
            fill=_COLOR_PALETTE["badge_gold"],
            outline=_COLOR_PALETTE["badge_gold_border"],
            width=2,
        )

        font_badge = _get_font(40)
        if font_badge:
            t = _shape_arabic(f"سورة {script.surah_name}")
            bbox = draw.textbbox((0, 0), t, font=font_badge)
            tw = bbox[2] - bbox[0]
            tx = badge_x + (badge_w - tw) // 2
            draw.text((tx, badge_y + 8), t, font=font_badge, fill=_COLOR_PALETTE["badge_gold_text_bg"])

        # 4. شعار القناة (أعلى اليمين – هوية ثابتة)
        logo_p = Paths.LOGO_PRIMARY
        if logo_p.exists():
            try:
                logo = Image.open(logo_p).convert("RGBA")
                logo.thumbnail((120, 120), Image.LANCZOS)  # تحافظ على التناسب
                bg.paste(logo, (W - logo.width - 30, 20), logo)
            except Exception as e:
                logger.warning("Failed to paste logo: %s", e)

        # 5. العنوان الرئيسي (CTR‑Centric)
        title = _smart_truncate(script.youtube_title, max_words=4)
        title = title.replace(" sura ", " سورة ").replace(" surah ", " سورة ")
        display_title = _shape_arabic(title)

        font_size = 70
        font_title, font_size = _fit_text_width(draw, display_title, int(W * 0.85), font_size)

        bbox = draw.textbbox((0, 0), display_title, font=font_title)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

        margin_bottom = 140
        tx = (W - tw) // 2
        ty = H - margin_bottom - th

        # استخدام Stroke ذكي (حدود خفيفة لا تُفرّق الحروف) مع تقوية اللون والشفافية
        self._draw_text_stroke(
            draw,
            (tx, ty),
            display_title,
            font=font_title,
            text_color=_COLOR_PALETTE["title_text"],
            stroke_color=_COLOR_PALETTE["title_stroke"],
            stroke_width=4,
            offset=1,
        )

        # 6. شارة رقم الحلقة (تصميم محاكٍ لقناة كبيرة)
        eps = f"الحلقة {episode_num}"
        font_ep = _get_font(34)
        if font_ep:
            ep_text = _shape_arabic(eps)
            bbox = draw.textbbox((0, 0), ep_text, font=font_ep)
            ew = bbox[2] - bbox[0] + 40
            eh = 46
            ep_x = (W - ew) // 2
            ep_y = ty - 60

            draw.rounded_rectangle(
                [ep_x, ep_y, ep_x + ew, ep_y + eh],
                radius=10,
                fill=_COLOR_PALETTE["ep_badge_bg"],
            )

            self._draw_text_stroke(
                draw,
                (ep_x + 20, ep_y + 2),
                ep_text,
                font=font_ep,
                text_color=_COLOR_PALETTE["ep_badge_text"],
                stroke_color=_COLOR_PALETTE["ep_badge_stroke"],
                stroke_width=1,
                offset=0,
            )

        # 7. حفظ بصيغة جودة عالية وملائمة لـ CTR
        bg = bg.convert("RGB")
        bg.save(out, "JPEG", quality=98, optimize=True, progressive=True)
        logger.info("✅ Thumbnail created for CTR: %s", out)
        return out