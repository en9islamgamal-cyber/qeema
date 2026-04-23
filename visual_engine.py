from __future__ import annotations

import logging
import time
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import APIKeys, Paths, VisualConfig
from models import EpisodeScript

logger = logging.getLogger(__name__)

class LeonardoAPIError(RuntimeError):
    pass

class VisualEngine:
    API = "https://cloud.leonardo.ai/api/rest/v1"

    BASE_STYLE = (
        "flat vector infographic, 2D educational illustration for children, "
        "clean solid pastel background, simple shapes, geometric composition, "
        "high readability, no text, no letters, no gradients, no photo realism, "
        "minimalist, polished, premium editorial design"
    )

    STYLE_BY_SCENE = {
        "intro": "warm welcoming educational hero illustration, balanced symmetry, cheerful icons",
        "outro": "closing illustration, calm joyful ending, gentle composition",
        "narrator": "explanatory icon-driven infographic, structured layout, clear visual hierarchy",
        "ayah": "respectful symbolic educational illustration, serene palette, abstract sacred motifs"
    }

    NEGATIVE_OVERRIDES = (
        "text, captions, watermark, logo, blurry, cluttered layout, noisy background, "
        "photograph, realistic skin, 3D render, extra limbs, distorted objects, low contrast"
    )

    def __init__(self):
        if not APIKeys.LEONARDO:
            raise ValueError("LEONARDO_API_KEY missing")

        self.headers = {
            "authorization": f"Bearer {APIKeys.LEONARDO}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        Paths.ensure_all()

    def _normalize_prompt(self, text: str) -> str:
        text = re.sub(r"s+", " ", text).strip()
        text = text.replace("،،", "،").replace("..", ".")
        return text

    def _scene_style(self, scene_type: str) -> str:
        return self.STYLE_BY_SCENE.get(scene_type, self.STYLE_BY_SCENE["narrator"])

    def _build_infographic_prompt(self, base_concept: str, scene_type: str) -> str:
        base_concept = self._normalize_prompt(base_concept)
        scene_style = self._scene_style(scene_type)

        prompt = (
            f"{base_concept}. "
            f"{scene_style}. "
            f"{self.BASE_STYLE}. "
            f"composition focused, centered subject, strong silhouettes, soft pastel palette, "
            f"storybook clarity, premium children's educational design"
        )
        return self._normalize_prompt(prompt)

    def _build_fallback_prompts(self, original_prompt: str, scene_type: str) -> List[str]:
        original_prompt = self._normalize_prompt(original_prompt)

        return [
            self._build_infographic_prompt(original_prompt, scene_type),
            self._normalize_prompt(
                f"{self._scene_style(scene_type)}, {self.BASE_STYLE}, "
                f"simple symbolic composition, clean iconography, pastel background"
            ),
            self._normalize_prompt(
                f"{self.BASE_STYLE}, abstract symbolic educational art, "
                f"children's infographic style, minimal, calm, clear"
            ),
        ]

    def _payload_for_prompt(self, prompt: str) -> dict:
        return {
            "prompt": prompt,
            "negative_prompt": f"{VisualConfig.NEGATIVE_PROMPT}, {self.NEGATIVE_OVERRIDES}",
            "modelId": VisualConfig.MODEL_ANIME,
            "num_images": VisualConfig.NUM_IMAGES,
            "width": VisualConfig.WIDTH,
            "height": VisualConfig.HEIGHT,
            "guidance_scale": VisualConfig.GUIDANCE_SCALE,
            "num_inference_steps": VisualConfig.STEPS,
            "presetStyle": "ILLUSTRATION",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _request(self, prompt: str) -> str:
        payload = self._payload_for_prompt(prompt)
        r = requests.post(f"{self.API}/generations", headers=self.headers, json=payload, timeout=45)
        if r.status_code not in (200, 201):
            raise LeonardoAPIError(f"Request failed: {r.status_code} | {r.text}")

        data = r.json()
        gen_id = (
            data.get("sdGenerationJob", {}).get("generationId")
            or data.get("generationId")
        )
        if not gen_id:
            raise LeonardoAPIError(f"Missing generationId: {data}")
        return gen_id

    def _poll_once(self, gen_id: str) -> Tuple[str, dict]:
        r = requests.get(f"{self.API}/generations/{gen_id}", headers=self.headers, timeout=20)
        r.raise_for_status()
        data = r.json().get("generations_by_pk", {})
        status = data.get("status", "")
        return status, data

    @retry(
        stop=stop_after_attempt(18),
        wait=wait_exponential(min=2, max=12),
        retry=retry_if_exception_type(LeonardoAPIError),
    )
    def _poll_until_complete(self, gen_id: str) -> str:
        status, data = self._poll_once(gen_id)

        if status == "COMPLETE":
            images = data.get("generated_images", [])
            if not images:
                raise LeonardoAPIError("Complete but no generated_images returned")
            return images[0]["url"]

        if status == "FAILED":
            raise LeonardoAPIError(data.get("failed_reason") or "Generation failed")

        raise LeonardoAPIError(f"Generation not ready: {status}")

    def _download(self, url: str, path: str) -> str:
        r = requests.get(url, timeout=45)
        r.raise_for_status()
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        return str(p)

    def _quality_gate(self, image_path: str) -> bool:
        p = Path(image_path)
        return p.exists() and p.stat().st_size > 10_000

    def _generate_with_prompt(self, prompt: str, output_path: str) -> str:
        gen_id = self._request(prompt)
        url = self._poll_until_complete(gen_id)
        saved = self._download(url, output_path)

        if not self._quality_gate(saved):
            raise LeonardoAPIError("Quality gate failed")

        return saved

    def generate_scene_image(
        self, original_prompt: str, output_path: str, scene_type: str = "narrator"
    ) -> str:
        prompts = self._build_fallback_prompts(original_prompt, scene_type)
        last_error = None

        for idx, prompt in enumerate(prompts, start=1):
            try:
                logger.info(f"Generating image attempt {idx}: {prompt[:90]}...")
                return self._generate_with_prompt(prompt, output_path)
            except Exception as e:
                last_error = e
                logger.warning(f"Attempt {idx} failed: {e}")

        raise RuntimeError(f"Failed to generate image: {output_path}") from last_error

    def generate_episode_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        vis_dir = Path(ep_dir) / "visuals"
        vis_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Starting visuals for episode {script.episode_number}")

        items = [
            (script.intro_scene, "intro", "intro.png"),
            *[(sc, "ayah", f"ayah_{sc.scene_id:03d}.png") for sc in script.ayah_scenes],
            *[(sc, "narrator", f"mid_{sc.scene_id:03d}.png") for sc in script.mid_scenes],
            (script.outro_scene, "outro", "outro.png"),
        ]

        for scene, scene_type, filename in items:
            output_path = str(vis_dir / filename)
            self.generate_scene_image(scene.visual_prompt, output_path, scene_type)
            scene.image_path = output_path
            time.sleep(1.0)

        logger.info("All visuals generated successfully")