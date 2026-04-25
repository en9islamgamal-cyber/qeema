"""
visual_engine.py — VALUE / QEEMA v5.0
======================================
محرك توليد الصور الاحترافي عبر Leonardo AI Phoenix.
- يستخدم Leonardo API بدلاً من PIL drawing
- ينتج إنفوجرافيك جذاب للأطفال
- Fallback على PIL لو Leonardo غير متاح
"""

import os
import time
import logging
import requests
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from tenacity import retry, stop_after_attempt, wait_exponential

from config import VisualConfig, Paths

if TYPE_CHECKING:
    from models import EpisodeScript

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Leonardo AI Adapter
# ════════════════════════════════════════════════════════════════
class LeonardoAdapter:
    """
    Leonardo Phoenix — أحدث model للصور التعليمية للأطفال.
    Docs: https://docs.leonardo.ai/reference/creategeneration
    """
    BASE_URL = "https://cloud.leonardo.ai/api/rest/v1"

    # Phoenix 1.0: أحدث model مناسب لإنفوجرافيك جذاب
    MODEL_PHOENIX = "6b645e3a-d64f-4341-a6d8-7a3690fbf042"
    # Leonardo Diffusion XL: نسخة بديلة
    MODEL_DIFFUSION_XL = "1e60896f-3c26-4296-8ecc-53e2afecc132"

    NEGATIVE_PROMPT = (
        "realistic, photo, photograph, 3d render, scary, dark, "
        "violence, weapons, text, watermark, signature, low quality, blurry"
    )

    STYLE_BOOST = (
        ", flat 2d vector illustration, children's book art style, "
        "warm pastel colors, soft lighting, friendly cute characters, "
        "educational infographic, clean composition, high quality"
    )

    def __init__(self, api_key: str, model_id: Optional[str] = None):
        self.api_key = api_key
        self.model_id = model_id or self.MODEL_PHOENIX
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=3, max=15))
    def _start_generation(self, prompt: str) -> str:
        full_prompt = prompt.strip().rstrip(",") + self.STYLE_BOOST
        payload = {
            "prompt": full_prompt[:1500],
            "negative_prompt": self.NEGATIVE_PROMPT,
            "modelId": self.model_id,
            "width": 1024,
            "height": 768,
            "num_images": 1,
            "guidance_scale": 7,
            "presetStyle": "ILLUSTRATION",
            "public": False,
        }
        resp = requests.post(
            f"{self.BASE_URL}/generations",
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Leonardo start HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        gen_id = data.get("sdGenerationJob", {}).get("generationId")
        if not gen_id:
            raise RuntimeError(f"No generation ID returned: {data}")
        return gen_id

    def _poll_generation(self, gen_id: str, max_wait_sec: int = 120) -> Optional[str]:
        """انتظار اكتمال الجيل واستخراج URL."""
        url = f"{self.BASE_URL}/generations/{gen_id}"
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            time.sleep(4)
            try:
                resp = requests.get(url, headers=self.headers, timeout=15)
                data = resp.json()
                gen = data.get("generations_by_pk") or {}
                status = gen.get("status", "")
                if status == "COMPLETE":
                    images = gen.get("generated_images", [])
                    if images:
                        return images[0].get("url")
                if status == "FAILED":
                    raise RuntimeError(f"Leonardo generation failed: {gen}")
            except Exception as e:
                logger.warning(f"⚠️ Poll error: {e}")
        return None

    def generate(self, prompt: str, output_path: str) -> bool:
        try:
            gen_id = self._start_generation(prompt)
            logger.info(f"🎨 Leonardo generation started: {gen_id[:8]}...")
            img_url = self._poll_generation(gen_id)
            if not img_url:
                logger.error("❌ Leonardo polling timed out")
                return False
            img_resp = requests.get(img_url, timeout=60)
            if img_resp.status_code != 200:
                logger.error(f"❌ Leonardo image download failed: {img_resp.status_code}")
                return False
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(img_resp.content)
            logger.info(f"✅ Image saved: {Path(output_path).name}")
            return True
        except Exception as e:
            logger.error(f"❌ Leonardo failed: {e}")
            return False


# ════════════════════════════════════════════════════════════════
# Fallback: PIL background generator
# ════════════════════════════════════════════════════════════════
def _pil_fallback(output_path: str, text: str = "") -> bool:
    """Fallback صورة بسيطة لو Leonardo فشل."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        # خلفية متدرجة لطيفة بدل الأخضر الفاضي
        w, h = VisualConfig.WIDTH, VisualConfig.HEIGHT
        img = Image.new("RGB", (w, h), color=(245, 222, 179))  # wheat
        draw = ImageDraw.Draw(img)
        # دوائر ديكورية
        for i, (cx, cy, r, c) in enumerate([
            (w*0.15, h*0.25, 200, (135, 206, 235)),  # sky blue
            (w*0.85, h*0.75, 250, (255, 182, 193)),  # pink
            (w*0.5, h*0.5, 150, (255, 215, 0)),      # gold
        ]):
            draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=c)
        # نص في المنتصف
        try:
            font = ImageFont.truetype(VisualConfig.FONT_PATH, 60) if VisualConfig.FONT_PATH else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        draw.text((w//2 - 200, h//2 - 30), text[:30] if text else "VALUE", fill=(34, 34, 34), font=font)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, quality=VisualConfig.OUTPUT_QUALITY)
        return True
    except Exception as e:
        logger.error(f"❌ PIL fallback failed: {e}")
        Path(output_path).touch()
        return False


# ════════════════════════════════════════════════════════════════
# VisualEngine — الموحّد
# ════════════════════════════════════════════════════════════════
class VisualEngine:
    def __init__(self):
        self.width = VisualConfig.WIDTH
        self.height = VisualConfig.HEIGHT
        self.leonardo: Optional[LeonardoAdapter] = None

        leo_key = os.getenv("LEONARDO_API_KEY", "")
        if leo_key:
            self.leonardo = LeonardoAdapter(leo_key)
            logger.info("✅ Leonardo adapter ready (Phoenix model)")
        else:
            logger.warning("⚠️ LEONARDO_API_KEY missing — falling back to PIL")

    def _generate_scene_image(self, prompt: str, fallback_text: str, scene_id: str) -> str:
        output_dir = Paths.TEMP_EPISODES / "visuals"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{scene_id}.jpg"

        # Try Leonardo first
        if self.leonardo:
            ok = self.leonardo.generate(prompt, str(output_path))
            if ok and output_path.exists() and output_path.stat().st_size > 5000:
                return str(output_path)
            logger.warning(f"⚠️ Leonardo failed for {scene_id}, using PIL fallback")

        # PIL fallback
        _pil_fallback(str(output_path), fallback_text)
        return str(output_path)

    def generate_episode_visuals(self, script: "EpisodeScript", ep_dir: str) -> None:
        logger.info(f"🎨 Generating visuals for episode {script.episode_number}...")

        # Intro
        script.intro_scene.image_path = self._generate_scene_image(
            script.intro_scene.visual_prompt,
            script.intro_scene.narrator_text,
            "intro",
        )

        # Ayah scenes
        for scene in script.ayah_scenes:
            scene.image_path = self._generate_scene_image(
                scene.visual_prompt,
                scene.intro_text,
                f"ayah_{scene.scene_id}",
            )

        # Mid scenes
        for sc in script.mid_scenes:
            sc.image_path = self._generate_scene_image(
                sc.visual_prompt,
                sc.narrator_text,
                f"mid_{sc.scene_id}",
            )

        # Outro
        script.outro_scene.image_path = self._generate_scene_image(
            script.outro_scene.visual_prompt,
            script.outro_scene.narrator_text,
            "outro",
        )

        logger.info("✅ All visuals ready")
