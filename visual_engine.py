"""
visual_engine.py — VALUE / QEEMA v2
محرك توليد الصور — Leonardo.ai
"""
from __future__ import annotations
import logging, time, os, requests
from pathlib import Path
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from config import APIKeys, Paths, VisualConfig
from models import AyahScene, EpisodeScript, NarratorScene

logger = logging.getLogger(__name__)

class VisualEngine:
    API   = "https://cloud.leonardo.ai/api/rest/v1"
    STYLE = VisualConfig.STYLE_SUFFIX

    def __init__(self):
        if not APIKeys.LEONARDO:
            raise ValueError("LEONARDO_API_KEY غير موجود")
        self.headers = {
            "authorization": f"Bearer {APIKeys.LEONARDO}",
            "content-type": "application/json",
        }

    def _prompt(self, base: str, scene_type: str = "narrator") -> str:
        moods = {
            "intro":   "warm welcome scene, grandfather opening arms, golden morning light, ",
            "ayah":    "divine light rays, Arabic calligraphy golden glow, peaceful sacred atmosphere, ",
            "outro":   "cheerful goodbye, animated stars flying, celebration warm colors, ",
            "default": "",
        }
        return f"{moods.get(scene_type,'')}{base}, {self.STYLE}"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
    def _request(self, prompt: str) -> str:
        r = requests.post(
            f"{self.API}/generations",
            headers=self.headers,
            json={
                "prompt": prompt,
                "negative_prompt": VisualConfig.NEGATIVE_PROMPT,
                "modelId": VisualConfig.MODEL_ANIME,
                "num_images": 1,
                "width": VisualConfig.WIDTH,
                "height": VisualConfig.HEIGHT,
                "guidance_scale": VisualConfig.GUIDANCE_SCALE,
                "num_inference_steps": VisualConfig.STEPS,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["sdGenerationJob"]["generationId"]

    @retry(stop=stop_after_attempt(12), wait=wait_exponential(min=4, max=15))
    def _poll(self, gen_id: str) -> str:
        r = requests.get(f"{self.API}/generations/{gen_id}", headers=self.headers, timeout=15)
        r.raise_for_status()
        data = r.json().get("generations_by_pk", {})
        if data.get("status") == "COMPLETE":
            return data["generated_images"][0]["url"]
        if data.get("status") == "FAILED":
            raise RuntimeError("Leonardo generation failed")
        raise Exception("still processing")

    def _download(self, url: str, path: str) -> str:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(r.content)
        return path

    def generate_scene_image(
        self, prompt: str, output_path: str, scene_type: str = "narrator"
    ) -> str:
        full_prompt = self._prompt(prompt, scene_type)
        logger.info(f"🎨 توليد صورة: {prompt[:45]}…")
        gen_id = self._request(full_prompt)
        time.sleep(8)
        url    = self._poll(gen_id)
        return self._download(url, output_path)

    def generate_episode_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        vis_dir = Path(ep_dir) / "visuals"
        vis_dir.mkdir(parents=True, exist_ok=True)

        # Intro
        p = str(vis_dir / "intro.png")
        try:
            self.generate_scene_image(script.intro_scene.visual_prompt, p, "intro")
            script.intro_scene.image_path = p
        except Exception as e:
            logger.error(f"❌ intro صورة: {e}")
        time.sleep(3)

        # Ayah scenes
        for sc in script.ayah_scenes:
            p = str(vis_dir / f"ayah_{sc.scene_id:03d}.png")
            try:
                self.generate_scene_image(sc.visual_prompt, p, "ayah")
                sc.image_path = p
            except Exception as e:
                logger.error(f"❌ ayah {sc.scene_id} صورة: {e}")
            time.sleep(3)

        # Mid scenes
        for sc in script.mid_scenes:
            p = str(vis_dir / f"mid_{sc.scene_id:03d}.png")
            try:
                self.generate_scene_image(sc.visual_prompt, p, "narrator")
                sc.image_path = p
            except Exception as e:
                logger.error(f"❌ mid {sc.scene_id} صورة: {e}")
            time.sleep(3)

        # Outro
        p = str(vis_dir / "outro.png")
        try:
            self.generate_scene_image(script.outro_scene.visual_prompt, p, "outro")
            script.outro_scene.image_path = p
        except Exception as e:
            logger.error(f"❌ outro صورة: {e}")

        logger.info("✅ تم توليد جميع الصور")
