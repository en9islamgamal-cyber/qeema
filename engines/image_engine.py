"""
engines/image_engine.py — VALUE / QEEMA v22.5 — Leonardo image generation
=========================================================================
Leonardo.ai REST API integration with locked style template (v18).

[v18 changes]
- Integrated VisualPromptEngineer for STRICT style consistency
- Locked positive + negative prompts override per-call additions
- Added emotion parameter for per-scene lighting selection
- Improved error categorization (auth vs content vs rate vs network)
- Added cost tracking integration

[v17 fix kept]
- Image dimensions 1536x864 (Leonardo API max is 1536)
- Defensive clamping in _submit_generation
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from core.exceptions import (
    AuthenticationError,
    NetworkError,
    PermanentError,
    PipelineError,
    RateLimitError,
    TransientError,
)
from engines.visual_prompt_engineer import VisualPromptEngineer

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Leonardo model UUIDs
# ════════════════════════════════════════════════════════════════
LEONARDO_PHOENIX = "6b645e3a-d64f-4341-a6d8-7a3690fbf042"
LEONARDO_LIGHTNING_XL = "b24e16ff-06e3-43eb-8d33-4416c2d75876"
LEONARDO_FLUX_DEV = "b2614463-296c-462a-9586-aafdb8f00e36"


# NOTE: v18 — Style constants moved to VisualPromptEngineer.
# All prompts go through VisualPromptEngineer.build_legacy() for consistency.


@dataclass
class LeonardoConfig:
    """Configuration for Leonardo image generation.

    [v17 fix] Image dimensions limited to 1536 max (Leonardo API constraint).
    Native generation: 1536x864 (16:9 aspect matches video).
    HTML uses background-size:cover to scale to 1920x1080 with imperceptible
    quality loss (4% upscale on a vignetted background).
    """
    api_key: str
    cache_dir: Path
    # Model selection
    hero_model_id: str = LEONARDO_PHOENIX           # for intro/outro
    scene_model_id: str = LEONARDO_LIGHTNING_XL     # for ayah scenes
    # v17: Image dimensions — Leonardo max is 1536 per dimension
    # 1536x864 = 16:9 ratio matches video output 1920x1080
    width: int = 1536
    height: int = 864
    # Generation params
    num_images: int = 1
    guidance_scale: int = 7
    # Quality settings
    enable_alchemy: bool = False     # premium feature, +cost
    enable_photoreal: bool = False   # off — we want illustration style
    enable_high_resolution: bool = True
    # Polling
    poll_interval_sec: float = 3.0
    max_poll_attempts: int = 40      # 40 × 3s = 120s max wait
    # Optional Character Reference (set via env LEONARDO_CHARACTER_REF)
    character_ref_id: Optional[str] = None
    # Optional ControlNet for layout consistency
    init_strength: float = 0.45      # how much to follow reference (if any)


class LeonardoImageEngine:
    """
    Leonardo.ai image generator with paid-plan optimizations.

    Usage:
        engine = LeonardoImageEngine(config)
        path = engine.generate(
            prompt="cosmic scale starry night with golden particles",
            output_path="/path/to/scene.png",
            is_hero=False,  # True for intro/outro
        )
        # Returns path on success; raises on failure.
    """

    BASE_URL = "https://cloud.leonardo.ai/api/rest/v1"

    def __init__(self, config: LeonardoConfig, quota_manager=None) -> None:
        if not config.api_key:
            raise ValueError("LeonardoImageEngine requires non-empty api_key")
        self._cfg = config
        self._quota_manager = quota_manager  # v19
        self._cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self._headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {config.api_key}",
        }
        logger.info(
            f"✅ Leonardo image engine ready "
            f"(hero={config.hero_model_id[:8]}..., scene={config.scene_model_id[:8]}...)"
        )

    # ─── Public API ──────────────────────────────────────────────
    def generate(
        self,
        prompt: str,
        output_path: str,
        *,
        is_hero: bool = False,
        episode_number: Optional[int] = None,
        emotion: str = "warm",  # v18: drives lighting selection
    ) -> str:
        """
        Generate an image and save to output_path.
        Returns path on success. Fails loudly on any generation problem.

        [v18] Uses VisualPromptEngineer for locked style.
        The 'prompt' parameter is treated as the 'subject' field;
        lighting/composition/style are added by the engineer.
        """
        if not prompt or not prompt.strip():
            raise PipelineError("Leonardo image generation requires a non-empty visual_prompt")

        # v19: Quota check BEFORE expensive Leonardo API call
        # Lightning XL = 3 tokens, Phoenix = 10 tokens (estimate)
        is_phoenix = self._cfg.hero_model_id.startswith("6b645e3a") if is_hero else \
                     self._cfg.scene_model_id.startswith("6b645e3a")
        token_estimate = 10 if is_phoenix else 3
        if self._cfg.enable_alchemy:
            token_estimate *= 2  # Alchemy doubles cost
        if self._cfg.enable_high_resolution:
            token_estimate += 2  # HD adds 2 tokens

        if self._quota_manager is not None:
            if not self._quota_manager.can_consume_leonardo(token_estimate):
                remaining = self._quota_manager.leonardo_remaining()
                raise PipelineError(
                    f"Leonardo quota too low ({remaining} < {token_estimate}); "
                    f"AI images are mandatory, so CSS fallback is disabled"
                )

        # v18: Build locked prompt via engineer (consistent style)
        full_prompt, negative_prompt = VisualPromptEngineer.build_legacy(
            llm_visual_prompt=prompt,
            emotion=emotion,
        )

        # Cache check (deterministic key)
        cache_key = self._cache_key(full_prompt, is_hero)
        cache_path = self._cfg.cache_dir / f"{cache_key}.png"
        if cache_path.exists() and cache_path.stat().st_size > 5000:
            logger.info(f"♻️ Leonardo cache hit: {cache_key}")
            try:
                import shutil
                shutil.copy(cache_path, output_path)
                return output_path
            except OSError as e:
                logger.warning(f"⚠️ Cache copy failed: {e}")

        # Generate fresh
        for attempt in range(1, 4):
            try:
                gen_id = self._submit_generation(
                    full_prompt, negative_prompt=negative_prompt,
                    is_hero=is_hero,
                )
                if not gen_id:
                    logger.warning(f"⚠️ Submit returned no gen_id (attempt {attempt})")
                    continue

                image_url = self._poll_until_ready(gen_id)
                if not image_url:
                    logger.warning(f"⚠️ Poll timeout for gen_id={gen_id} (attempt {attempt})")
                    continue

                # Download + save
                ok = self._download_image(image_url, output_path)
                if ok:
                    # Update cache (atomic copy)
                    try:
                        import shutil
                        shutil.copy(output_path, cache_path)
                    except OSError:
                        pass
                    # v19: Record quota consumption
                    if self._quota_manager is not None:
                        self._quota_manager.consume_leonardo(token_estimate)
                    logger.info(f"✅ Leonardo image saved: {Path(output_path).name}")
                    return output_path
            except PermanentError as e:
                logger.error(f"❌ Leonardo permanent error: {e}")
                raise PipelineError(f"Leonardo permanent error: {e}") from e
            except (NetworkError, TransientError, RateLimitError) as e:
                logger.warning(f"⚠️ Leonardo attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    time.sleep(2.0 * attempt)
            except Exception as e:
                logger.error(f"❌ Leonardo unexpected error: {e}")
                if attempt < 3:
                    time.sleep(2.0 * attempt)

        raise PipelineError("Leonardo failed after 3 attempts; CSS fallback is disabled")

    # ─── Internal ────────────────────────────────────────────────
    def _cache_key(self, prompt: str, is_hero: bool) -> str:
        h = hashlib.sha256()
        h.update(prompt.encode("utf-8"))
        h.update(b"|hero=" if is_hero else b"|scene=")
        h.update(self._cfg.hero_model_id.encode() if is_hero else self._cfg.scene_model_id.encode())
        return h.hexdigest()[:20]

    def _submit_generation(
        self, prompt: str, *,
        negative_prompt: str = "",
        is_hero: bool,
    ) -> Optional[str]:
        """Submit a generation job. Returns generation_id on success."""
        model_id = self._cfg.hero_model_id if is_hero else self._cfg.scene_model_id

        # v17 defensive: clamp dimensions to Leonardo API limits (32-1536)
        width = max(32, min(1536, self._cfg.width))
        height = max(32, min(1536, self._cfg.height))
        # Round to multiples of 8 (SDXL requirement)
        width = (width // 8) * 8
        height = (height // 8) * 8

        payload: Dict[str, Any] = {
            "prompt": prompt[:1000],  # Leonardo limit
            "negative_prompt": negative_prompt or "low quality, blurry",
            "modelId": model_id,
            "width": width,
            "height": height,
            "num_images": self._cfg.num_images,
            "guidance_scale": self._cfg.guidance_scale,
            "public": False,  # private generation
        }

        # Phoenix-specific
        if model_id == LEONARDO_PHOENIX:
            payload["alchemy"] = self._cfg.enable_alchemy
            payload["highResolution"] = self._cfg.enable_high_resolution

        # Optional: Character Reference for figure consistency
        if self._cfg.character_ref_id:
            payload["controlnets"] = [{
                "initImageId": self._cfg.character_ref_id,
                "initImageType": "UPLOADED",
                "preprocessorId": 133,  # Character Reference preset
                "strengthType": "Mid",
            }]

        try:
            resp = requests.post(
                f"{self.BASE_URL}/generations",
                json=payload,
                headers=self._headers,
                timeout=30,
            )
        except requests.Timeout as e:
            raise NetworkError(f"Leonardo submit timeout: {e}", cause=e) from e
        except requests.RequestException as e:
            raise NetworkError(f"Leonardo submit network error: {e}", cause=e) from e

        if resp.status_code in (401, 403):
            raise AuthenticationError(f"Leonardo auth failed: HTTP {resp.status_code}")
        if resp.status_code == 429:
            retry = float(resp.headers.get("Retry-After", "30"))
            raise RateLimitError(f"Leonardo rate limited", retry_after=retry)
        if resp.status_code == 400:
            # Bad prompt or content policy — don't retry
            raise PermanentError(
                f"Leonardo rejected request: {resp.text[:200]}"
            )
        if resp.status_code >= 500:
            raise NetworkError(f"Leonardo server error: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TransientError(
                f"Leonardo unexpected status {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        gen_id = (
            data.get("sdGenerationJob", {}).get("generationId")
            or data.get("generationId")
        )
        if not gen_id:
            raise TransientError(f"Leonardo response missing generationId: {data}")

        logger.debug(f"📤 Leonardo job submitted: {gen_id}")
        return gen_id

    def _poll_until_ready(self, gen_id: str) -> Optional[str]:
        """Poll generation status. Returns first image URL when COMPLETE."""
        url = f"{self.BASE_URL}/generations/{gen_id}"

        for attempt in range(self._cfg.max_poll_attempts):
            time.sleep(self._cfg.poll_interval_sec)

            try:
                resp = requests.get(url, headers=self._headers, timeout=20)
            except requests.RequestException as e:
                logger.debug(f"⚠️ Poll attempt {attempt}: {e}")
                continue

            if resp.status_code != 200:
                logger.debug(f"⚠️ Poll status {resp.status_code}")
                continue

            data = resp.json()
            job = data.get("generations_by_pk") or {}
            status = job.get("status", "")

            if status == "COMPLETE":
                images = job.get("generated_images", [])
                if images and images[0].get("url"):
                    return images[0]["url"]
                logger.warning(f"⚠️ Leonardo COMPLETE but no images: {job}")
                return None
            elif status == "FAILED":
                logger.error(f"❌ Leonardo generation FAILED: {job}")
                return None
            # PENDING — keep polling

        logger.warning(f"⚠️ Leonardo poll exhausted ({self._cfg.max_poll_attempts} attempts)")
        return None

    def _download_image(self, image_url: str, output_path: str) -> bool:
        """Download image from URL atomically."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")

        try:
            resp = requests.get(image_url, timeout=60, stream=True)
            if resp.status_code != 200:
                logger.warning(f"⚠️ Image download HTTP {resp.status_code}")
                return False

            with tmp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)

            if tmp.stat().st_size < 5000:
                tmp.unlink(missing_ok=True)
                logger.warning(f"⚠️ Downloaded image too small: {tmp.stat().st_size} bytes")
                return False

            tmp.replace(out)
            return True
        except requests.RequestException as e:
            tmp.unlink(missing_ok=True)
            logger.warning(f"⚠️ Image download failed: {e}")
            return False

    def health_check(self) -> bool:
        """Quick health probe."""
        try:
            resp = requests.get(
                f"{self.BASE_URL}/me",
                headers=self._headers,
                timeout=10,
            )
            if resp.status_code == 200:
                me = resp.json().get("user_details", [{}])[0]
                logger.info(
                    f"✅ Leonardo OK — user={me.get('user', {}).get('username', '?')}, "
                    f"tokens_remaining={me.get('tokenRenewalDate', '?')}"
                )
                return True
            logger.warning(f"⚠️ Leonardo health HTTP {resp.status_code}")
            return False
        except Exception as e:
            logger.warning(f"⚠️ Leonardo health check failed: {e}")
            return False
