"""
assets_engines/leonardo_client.py
====================================================================
Leonardo.ai client. Generates images, polls for completion, downloads.

Workflow:
  1. POST /generations with the prompt → returns generation_id
  2. Poll GET /generations/{id} until status=COMPLETE
  3. Download the generated image to disk

Uses Phoenix model by default (b24e16ff-06e3-43eb-8d33-4416c2d75876)
because it handles atmospheric/illustration styles well.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

from core.config import (
    LEONARDO_CACHE_DIR, get_api_keys, get_pipeline_config,
)


log = logging.getLogger(__name__)

LEONARDO_API_BASE = "https://cloud.leonardo.ai/api/rest/v1"
HTTP_TIMEOUT_SEC = 30


class LeonardoError(Exception):
    pass


@dataclass
class LeonardoImageResult:
    """Result of one image generation."""
    prompt: str
    image_url: str
    local_path: Path
    generation_id: str


class LeonardoClient:
    """Single Leonardo image generation pipeline."""

    def __init__(self) -> None:
        self.keys = get_api_keys()
        self.cfg = get_pipeline_config()
        LEONARDO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.keys.leonardo_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> LeonardoImageResult:
        """
        Generate one image. Blocks until ready or fails.
        Caches by prompt hash to avoid re-generating identical prompts.
        """
        w = width or self.cfg.leonardo_width
        h = height or self.cfg.leonardo_height

        # Check cache
        cache_key = hashlib.sha1(
            f"{prompt}|{negative_prompt}|{w}x{h}".encode("utf-8")
        ).hexdigest()[:16]
        cached_img = LEONARDO_CACHE_DIR / f"{cache_key}.png"
        if cached_img.exists() and cached_img.stat().st_size > 1024:
            log.info("✓ Leonardo cache hit: %s", cached_img.name)
            return LeonardoImageResult(
                prompt=prompt,
                image_url="<cached>",
                local_path=cached_img,
                generation_id="<cached>",
            )

        # Submit generation
        gen_id = self._submit(prompt, negative_prompt, w, h)
        log.info("Leonardo generation %s submitted", gen_id)

        # Poll
        image_url = self._poll(gen_id)

        # Download
        local_path = self._download(image_url, cached_img)

        return LeonardoImageResult(
            prompt=prompt,
            image_url=image_url,
            local_path=local_path,
            generation_id=gen_id,
        )

    def generate_batch(
        self,
        prompts: List[str],
        negative_prompt: str = "",
    ) -> List[LeonardoImageResult]:
        """Generate multiple images sequentially (Leonardo API has rate limits)."""
        results: List[LeonardoImageResult] = []
        for i, p in enumerate(prompts, start=1):
            log.info("Leonardo image %d/%d", i, len(prompts))
            results.append(self.generate(p, negative_prompt=negative_prompt))
            # tiny delay to be polite
            if i < len(prompts):
                time.sleep(2.0)
        return results

    # ─────────────────────────────────────────────────────────────
    # Internal: submit + poll + download
    # ─────────────────────────────────────────────────────────────

    def _submit(
        self, prompt: str, negative_prompt: str, w: int, h: int,
    ) -> str:
        payload = {
            "prompt": prompt[:1490],  # Leonardo cap
            "modelId": self.cfg.leonardo_model_id,
            "width": w,
            "height": h,
            "num_images": self.cfg.leonardo_num_images,
            "guidance_scale": self.cfg.leonardo_guidance,
            "alchemy": self.cfg.leonardo_alchemy,
            "public": False,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt[:990]

        for attempt in range(1, self.cfg.leonardo_max_retries + 2):
            try:
                r = requests.post(
                    f"{LEONARDO_API_BASE}/generations",
                    json=payload,
                    headers=self._headers(),
                    timeout=HTTP_TIMEOUT_SEC,
                )
                r.raise_for_status()
                data = r.json()
                gen_id = (
                    data.get("sdGenerationJob", {}).get("generationId")
                    or data.get("generationId")
                )
                if not gen_id:
                    raise LeonardoError(f"No generation_id in response: {data}")
                return gen_id

            except Exception as e:
                msg = str(e)[:200]
                if attempt > self.cfg.leonardo_max_retries:
                    raise LeonardoError(f"Submit failed: {msg}")
                log.warning(
                    "Leonardo submit attempt %d failed: %s; retrying",
                    attempt, msg,
                )
                time.sleep(2 ** attempt)

        raise LeonardoError("Unreachable")

    def _poll(self, generation_id: str) -> str:
        """Poll until the generation completes. Returns image URL."""
        max_attempts = self.cfg.leonardo_max_poll_attempts
        interval = self.cfg.leonardo_poll_interval_sec

        for attempt in range(1, max_attempts + 1):
            time.sleep(interval)
            try:
                r = requests.get(
                    f"{LEONARDO_API_BASE}/generations/{generation_id}",
                    headers=self._headers(),
                    timeout=HTTP_TIMEOUT_SEC,
                )
                r.raise_for_status()
                data = r.json()
                gen = data.get("generations_by_pk", {})
                status = (gen.get("status") or "").upper()

                if status == "COMPLETE":
                    images = gen.get("generated_images", [])
                    if not images:
                        raise LeonardoError("Complete but no images")
                    return images[0]["url"]

                if status == "FAILED":
                    raise LeonardoError(f"Generation {generation_id} FAILED")

                log.debug(
                    "Poll %d/%d: status=%s",
                    attempt, max_attempts, status or "<empty>",
                )

            except LeonardoError:
                raise
            except Exception as e:
                log.warning(
                    "Poll attempt %d error: %s", attempt, str(e)[:200],
                )

        raise LeonardoError(
            f"Generation {generation_id} didn't complete in "
            f"{max_attempts * interval:.0f}s"
        )

    def _download(self, image_url: str, dest: Path) -> Path:
        for attempt in range(1, 4):
            try:
                r = requests.get(image_url, timeout=HTTP_TIMEOUT_SEC, stream=True)
                r.raise_for_status()
                tmp = dest.with_suffix(".tmp")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                tmp.rename(dest)
                size = dest.stat().st_size
                log.info("✓ Downloaded %d bytes → %s", size, dest.name)
                return dest
            except Exception as e:
                if attempt == 3:
                    raise LeonardoError(f"Download failed: {e}")
                log.warning("Download attempt %d failed: %s", attempt, e)
                time.sleep(2 ** attempt)
        raise LeonardoError("Unreachable")
