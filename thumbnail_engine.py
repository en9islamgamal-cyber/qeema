"""
thumbnail_engine.py — VALUE / QEEMA v4.0
محرك تصميم الغلاف المصغر (Thumbnail) للفيديو.
"""

import logging
import os
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logging.warning("PIL not installed, thumbnail generation disabled")

logger = logging.getLogger(__name__)


class ThumbnailEngine:
    """
    تصميم غلاف مصغر جذاب باستخدام صورة الخلفية ونص العنوان.
    """

    def __init__(self, font_path: Optional[str] = None):
        self.font_path = font_path
        self.default_font_size = 60
        self.title_font_size = 80
        self.output_size = (1280, 720)  # YouTube thumbnail dimensions

    def create(self, script, episode_number: int, background_image: str) -> str:
        """
        إنشاء الغلاف المصغر.
        """
        if not HAS_PIL:
            logger.warning("PIL not available, using fallback thumbnail")
            return self._create_fallback_thumbnail(episode_number, script.surah_name)

        logger.info(f"🖼️ Creating thumbnail for episode {episode_number}")
        
        output_path = Path("assets/thumbnails") / f"ep_{episode_number:03d}.jpg"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        bg_path = background_image if background_image and Path(background_image).exists() else self._get_default_background()
        
        try:
            img = Image.open(bg_path).convert("RGB")
            img = img.resize(self.output_size, Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(img)
            
            # شريط علوي شفاف
            overlay = Image.new("RGBA", self.output_size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle([(0, 0), (self.output_size[0], 150)], fill=(0, 0, 0, 180))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)
            
            # ✅ التصحيح: السطر 60 أصبح سطراً واحداً
            title = f"تفسير سورة {script.surah_name}"
            parts = title.replace(" ", "\n")   # فصل الكلمات بأسطر جديدة
            
            font = self._load_font(self.title_font_size)
            y_offset = 40
            for line in parts.split("\n"):
                bbox = draw.textbbox((0, 0), line, font=font)
                text_width = bbox[2] - bbox[0]
                x = (self.output_size[0] - text_width) // 2
                draw.text((x, y_offset), line, fill=(255, 255, 255), font=font)
                y_offset += self.title_font_size + 10
            
            # رقم الحلقة
            ep_font = self._load_font(self.default_font_size)
            ep_text = f"الحلقة {episode_number}"
            bbox = draw.textbbox((0, 0), ep_text, font=ep_font)
            text_width = bbox[2] - bbox[0]
            x = (self.output_size[0] - text_width) // 2
            draw.text((x, self.output_size[1] - 80), ep_text, fill=(255, 215, 0), font=ep_font)
            
            img.save(output_path, "JPEG", quality=90)
            logger.info(f"✅ Thumbnail saved: {output_path}")
            return str(output_path)
            
        except Exception as e:
            logger.error(f"❌ Failed to create thumbnail: {e}")
            return self._create_fallback_thumbnail(episode_number, script.surah_name)

    def _get_default_background(self) -> str:
        default = Path("assets/thumbnails/default_bg.jpg")
        if default.exists():
            return str(default)
        if HAS_PIL:
            img = Image.new("RGB", self.output_size, color=(34, 139, 34))
            default.parent.mkdir(parents=True, exist_ok=True)
            img.save(default)
        return str(default)

    def _load_font(self, size: int):
        try:
            if self.font_path and Path(self.font_path).exists():
                return ImageFont.truetype(self.font_path, size)
            # مسارات خطوط شائعة
            common = [
                "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "C:\\Windows\\Fonts\\Arial.ttf"
            ]
            for f in common:
                if Path(f).exists():
                    return ImageFont.truetype(f, size)
        except Exception:
            pass
        return ImageFont.load_default()

    def _create_fallback_thumbnail(self, episode_number: int, surah_name: str) -> str:
        output_path = Path("assets/thumbnails") / f"ep_{episode_number:03d}_fallback.jpg"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if HAS_PIL:
            img = Image.new("RGB", self.output_size, color=(50, 50, 150))
            draw = ImageDraw.Draw(img)
            font = self._load_font(60)
            text = f"Episode {episode_number}\n{surah_name}"
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x = (self.output_size[0] - text_width) // 2
            y = (self.output_size[1] - text_height) // 2
            draw.text((x, y), text, fill=(255,255,255), font=font)
            img.save(output_path)
        else:
            # في حالة عدم وجود PIL، ننشئ ملفاً وهمياً
            output_path.write_bytes(b"")
        return str(output_path)