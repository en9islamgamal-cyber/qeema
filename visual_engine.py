"""
visual_engine.py — VALUE / QEEMA v4.0
محرك توليد الصور والإنفوجرافيك.
"""

import logging
from pathlib import Path

from config import VisualConfig, Paths

logger = logging.getLogger(__name__)


class VisualEngine:
    def __init__(self):
        self.width = VisualConfig.WIDTH
        self.height = VisualConfig.HEIGHT

    def generate_episode_visuals(self, script, ep_dir: str):
        logger.info(f"🎨 Generating visuals for episode {script.episode_number}")
        
        # intro
        intro_img = self._generate_scene_image(
            script.intro_scene.visual_prompt,
            script.intro_scene.narrator_text,
            "intro"
        )
        script.intro_scene.image_path = intro_img
        
        for scene in script.ayah_scenes:
            img = self._generate_scene_image(
                scene.visual_prompt,
                scene.intro_text + " " + scene.explain_text,
                f"ayah_{scene.scene_id}"
            )
            scene.image_path = img
        
        outro_img = self._generate_scene_image(
            script.outro_scene.visual_prompt,
            script.outro_scene.narrator_text,
            "outro"
        )
        script.outro_scene.image_path = outro_img
        
        logger.info("✅ Visuals generated")

    def _generate_scene_image(self, prompt: str, text: str, scene_id: str) -> str:
        output_dir = Paths.TEMP_EPISODES / "visuals"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{scene_id}.jpg"
        
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (self.width, self.height), color=VisualConfig.BACKGROUND_COLOR)
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype(VisualConfig.FONT_PATH, 40) if VisualConfig.FONT_PATH and Path(VisualConfig.FONT_PATH).exists() else ImageFont.load_default()
            except:
                font = ImageFont.load_default()
            draw.text((50, 50), text[:200], fill=VisualConfig.TEXT_COLOR, font=font)
            img.save(output_path, quality=VisualConfig.OUTPUT_QUALITY)
        except Exception as e:
            logger.warning(f"Could not generate image via PIL: {e}")
            output_path.touch()
        return str(output_path)