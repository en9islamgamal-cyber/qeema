"""
thumbnail_engine.py — VALUE / QEEMA v4.0
محرك تصميم الغلاف المصغر.
"""

import logging
from pathlib import Path

from config import Paths, VisualConfig

logger = logging.getLogger(__name__)


class ThumbnailEngine:
    def create(self, script, episode_number: int, background_image: str) -> str:
        output_path = Paths.THUMBNAILS / f"ep_{episode_number:03d}.jpg"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image, ImageDraw, ImageFont
            if background_image and Path(background_image).exists():
                img = Image.open(background_image).resize((VisualConfig.WIDTH, VisualConfig.HEIGHT))
            else:
                img = Image.new("RGB", (VisualConfig.WIDTH, VisualConfig.HEIGHT), color=VisualConfig.BACKGROUND_COLOR)
            draw = ImageDraw.Draw(img)
            title = f"سورة {script.surah_name}\nالحلقة {episode_number}"
            font = ImageFont.load_default()
            draw.text((50, 50), title, fill=VisualConfig.TEXT_COLOR, font=font)
            img.save(output_path, quality=VisualConfig.OUTPUT_QUALITY)
        except Exception as e:
            logger.error(f"Thumbnail generation failed: {e}")
            output_path.touch()
        return str(output_path)
