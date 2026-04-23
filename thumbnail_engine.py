"""
thumbnail_engine.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
═══════════════════════════════════════════════════════
محرك الغلاف المصغر (Thumbnail) الموجه لرفع نسبة النقر (CTR Optimized)
• تدرج لوني ذكي (Gradient Mask) للحفاظ على سطوع الإنفوجرافيك.
• خوارزمية Stroke & Shadow لنصوص بارزة كقنوات المليونيات.
• توافق تام مع الاتجاه العربي (RTL) والهوية البصرية للقناة.
═══════════════════════════════════════════════════════
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional
from config import Paths
from models import EpisodeScript

logger = logging.getLogger(__name__)

def _get_font(size: int, bold: bool = True):
    """يجلب أفضل خط عربي متاح، مع تفضيل Amiri-Bold"""
    try:
        from PIL import ImageFont
        
        primary = Paths.FONTS / "Amiri-Bold.ttf"
        if primary.exists():
            return ImageFont.truetype(str(primary), size)
            
        for d in Paths.FONTS.glob("*.ttf"):
            try: return ImageFont.truetype(str(d), size)
            except: pass
            
        for p in [
            "/usr/share/fonts/truetype/arabic/Amiri-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
        ]:
            if Path(p).exists():
                try: return ImageFont.truetype(p, size)
                except: pass
        return ImageFont.load_default()
    except ImportError:
        return None

class ThumbnailEngine:
    SIZE = (1280, 720)
    
    def _draw_text_with_stroke(self, draw, pos, text, font, text_color, stroke_color, stroke_width=3):
        """خوارزمية رسم النص مع حواف (Stroke) قوية لبروز سينمائي"""
        x, y = pos
        # رسم الحواف في كل الاتجاهات
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx*dx + dy*dy <= stroke_width*stroke_width:
                    draw.text((x + dx, y + dy), text, font=font, fill=stroke_color)
        # رسم النص الرئيسي
        draw.text((x, y), text, font=font, fill=text_color)

    def create(
        self,
        script: EpisodeScript,
        episode_num: int,
        scene_image: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageEnhance
            import arabic_reshaper
            from bidi.algorithm import get_display
        except ImportError:
            logger.warning("⚠️ مكتبة PIL غير مثبتة — سيتم تجاوز توليد الغلاف")
            return ""

        out = output_path or str(Paths.THUMBNAILS / f"ep_{episode_num:03d}.jpg")
        Path(out).parent.mkdir(parents=True, exist_ok=True)

        W, H = self.SIZE

        # ── 1. معالجة الخلفية بذكاء ────────────────────────
        if scene_image and Path(scene_image).exists():
            bg = Image.open(scene_image).convert("RGBA").resize(self.SIZE, Image.LANCZOS)
            # تعزيز طفيف للألوان بدلاً من التعتيم القاتل القديم
            bg = ImageEnhance.Color(bg).enhance(1.2)
            bg = ImageEnhance.Contrast(bg).enhance(1.1)
        else:
            bg = Image.new("RGBA", self.SIZE, (20, 30, 48, 255))

        # ── 2. التدرج اللوني (Gradient Mask) ───────────────
        # وضع تدرج أسود من الأسفل للأعلى لنجعل النص بارزاً مع بقاء أعلى الصورة ساطعاً
        gradient = Image.new('RGBA', self.SIZE, color=(0, 0, 0, 0))
        draw_grad = ImageDraw.Draw(gradient)
        for y in range(H):
            # الشفافية تزيد تدريجياً: من 0 أعلى الشاشة إلى 220 أسفلها
            alpha = int((y / H) ** 1.5 * 220) 
            draw_grad.line([(0, y), (W, y)], fill=(10, 15, 25, alpha))
        
        bg = Image.alpha_composite(bg, gradient)
        draw = ImageDraw.Draw(bg, "RGBA")

        # ── 3. شارة السورة (ذهبية - أعلى اليسار) ───────────
        badge_w, badge_h = 350, 75
        badge_x, badge_y = 30, 30
        draw.rounded_rectangle([badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], radius=15, fill=(255, 215, 0, 230))
        draw.rounded_rectangle([badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], radius=15, outline=(255, 255, 255, 200), width=2)

        f_badge = _get_font(42)
        if f_badge:
            try:
                raw_txt = f"سورة {script.surah_name}"
                t = get_display(arabic_reshaper.reshape(raw_txt))
            except: t = f"سورة {script.surah_name}"
            
            tb = draw.textbbox((0, 0), t, font=f_badge)
            tx = badge_x + (badge_w - (tb[2] - tb[0])) // 2
            draw.text((tx, badge_y + 8), t, font=f_badge, fill=(20, 20, 40, 255))

        # ── 4. شعار القناة (اللوجو - أعلى اليمين) ─────────
        logo_p = Paths.LOGO_PRIMARY # 👈 الارتباط السليم بـ config.py
        if logo_p.exists():
            try:
                logo = Image.open(str(logo_p)).convert("RGBA")
                # تصغير اللوجو مع الحفاظ على أبعاده
                logo.thumbnail((120, 120), Image.LANCZOS)
                bg.paste(logo, (W - logo.width - 30, 30), logo)
            except Exception as e:
                logger.warning(f"⚠️ فشل دمج اللوجو في الثامبنييل: {e}")

        # ── 5. العنوان الرئيسي (ضخم، وسط أسفل الشاشة) ─────
        f_title = _get_font(85) # خط ضخم للفت الانتباه
        if f_title:
            try:
                title_t = get_display(arabic_reshaper.reshape(script.youtube_title))
            except: title_t = script.youtube_title

            tb = draw.textbbox((0,0), title_t, font=f_title)
            tw = tb[2] - tb[0]
            
            # محاذاة في المنتصف تماماً، في الثلث السفلي
            tx_x = (W - tw) // 2
            tx_y = H - 180
            
            # رسم النص مع إطار أسود سميك (Stroke) لبروز أسطوري
            self._draw_text_with_stroke(draw, (tx_x, tx_y), title_t, font=f_title, text_color=(255, 255, 255, 255), stroke_color=(0, 0, 0, 255), stroke_width=6)

        # ── 6. شارة رقم الحلقة (ذهبية صغيرة) ──────────────
        f_ep = _get_font(36)
        if f_ep:
            try:
                ep_t = get_display(arabic_reshaper.reshape(f"الحلقة {episode_num}"))
            except: ep_t = f"الحلقة {episode_num}"
            
            tb = draw.textbbox((0,0), ep_t, font=f_ep)
            ep_w = tb[2] - tb[0] + 40
            ep_x = (W - ep_w) // 2
            ep_y = tx_y - 65
            
            draw.rounded_rectangle([ep_x, ep_y, ep_x + ep_w, ep_y + 50], radius=10, fill=(255, 200, 0, 255))
            self._draw_text_with_stroke(draw, (ep_x + 20, ep_y + 2), ep_t, font=f_ep, text_color=(20, 20, 30, 255), stroke_color=(255, 255, 255, 100), stroke_width=1)

        # تحويل النهاية وحفظ
        final_img = bg.convert("RGB")
        final_img.save(out, "JPEG", quality=98, optimize=True)
        logger.info(f"✅ تم تصميم الغلاف المصغر الاحترافي: {out}")
        return out
