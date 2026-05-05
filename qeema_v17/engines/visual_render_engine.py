"""
engines/visual_render_engine.py — VALUE / QEEMA v11.0 (Production)
=====================================================================
Procedural visual rendering using BrowserPool + scene templates + FFmpeg.

[Pipeline per scene]
  1. Probe audio duration (set scene length)
  2. Build HTML from scene template
  3. Acquire browser from pool
  4. Open new BrowserContext with video recording enabled
  5. Navigate to file://...html
  6. Wait warmup_ms + (audio_duration × 1000) ms
  7. Close context (writes .webm)
  8. FFmpeg encode webm + audio → mp4
  9. Atomic rename + cache copy

[Cache strategy]
  hash = sha256(scene_type + palette + text + audio_size + audio_mtime)
  → /scene_cache/{hash}.mp4

  If hit: copy from cache; skip render entirely.

[Failure model]
  Any per-scene failure raises VisualRenderError.
  The orchestrator decides whether to retry the whole episode.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.config import (
    BrandingConfig,
    PathsConfig,
    ProceduralConfig,
    VideoConfig,
)
from core.exceptions import VisualRenderError
from core.interfaces import (
    SceneRenderRequest,
    SceneRenderResult,
    VideoAssembler,
    VisualRenderer,
)
from engines.scene_templates import build_scene_html
from infrastructure.audio_utils import get_audio_duration
from infrastructure.browser_pool import BrowserPool

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Cache key derivation
# ════════════════════════════════════════════════════════════════
def _scene_cache_key(
    *,
    scene_type: str,
    palette: str,
    text: str,
    is_ayah: bool,
    audio_path: str,
    width: int,
    height: int,
    background_image: Optional[str] = None,  # v16
    color_grade_filter: Optional[str] = None,  # v17
) -> str:
    """SHA-256 cache key combining all render-affecting inputs."""
    h = hashlib.sha256()
    h.update(scene_type.encode("utf-8")); h.update(b"\x00")
    h.update(palette.encode("utf-8")); h.update(b"\x00")
    h.update(text.encode("utf-8")); h.update(b"\x00")
    h.update(b"ayah" if is_ayah else b"narr"); h.update(b"\x00")
    h.update(f"{width}x{height}".encode("utf-8")); h.update(b"\x00")
    # v16: background image affects render output
    if background_image:
        h.update(b"bg:"); h.update(background_image.encode("utf-8")); h.update(b"\x00")
    # v17: color grade is baked-in, so it's part of the cache key
    if color_grade_filter:
        h.update(b"cg:"); h.update(color_grade_filter.encode("utf-8")); h.update(b"\x00")
    try:
        st = Path(audio_path).stat()
        h.update(str(st.st_size).encode("utf-8")); h.update(b"\x00")
        # Truncate mtime to seconds (avoid sub-second key churn)
        h.update(str(int(st.st_mtime)).encode("utf-8"))
    except OSError:
        h.update(b"unknown_audio")
    return h.hexdigest()[:24]


# ════════════════════════════════════════════════════════════════
# ProceduralRenderer
# ════════════════════════════════════════════════════════════════
class ProceduralRenderer(VisualRenderer):
    """
    Procedural renderer: HTML/Three.js scene → MP4 segment.

    [Lifecycle]
        renderer = ProceduralRenderer(...)
        renderer.warmup()           # launch browser pool once
        try:
            for scene in scenes:
                renderer.render(req, audio_path)
        finally:
            renderer.shutdown()
    """

    def __init__(
        self,
        *,
        paths: PathsConfig,
        video_cfg: VideoConfig,
        proc_cfg: ProceduralConfig,
        branding: BrandingConfig,
        assembler: VideoAssembler,
        color_grade_filter: Optional[str] = None,  # v17
    ) -> None:
        self._paths: PathsConfig = paths
        self._video: VideoConfig = video_cfg
        self._proc: ProceduralConfig = proc_cfg
        self._branding: BrandingConfig = branding
        self._assembler: VideoAssembler = assembler
        # v17: bake color grade into per-scene encoding (no separate stage)
        self._color_grade_filter: Optional[str] = color_grade_filter

        self._pool: BrowserPool = BrowserPool(
            pool_size=proc_cfg.browser_pool_size,
            render_size=(video_cfg.width, video_cfg.height),
        )
        self._warmed_up: bool = False
        # Ensure cache + render dirs exist
        paths.scene_cache.mkdir(parents=True, exist_ok=True)
        paths.web_renders.mkdir(parents=True, exist_ok=True)
        paths.html_templates.mkdir(parents=True, exist_ok=True)

    # ───────────────────────────────────────────────────────────
    # Lifecycle
    # ───────────────────────────────────────────────────────────
    def warmup(self) -> None:
        if self._warmed_up:
            return
        self._pool.warmup()
        self._warmed_up = True

    def shutdown(self) -> None:
        self._pool.shutdown()
        self._warmed_up = False

    # ───────────────────────────────────────────────────────────
    # Public render
    # ───────────────────────────────────────────────────────────
    def render(
        self,
        request: SceneRenderRequest,
        audio_path: str,
    ) -> SceneRenderResult:
        if not Path(audio_path).exists():
            raise VisualRenderError(
                f"Audio missing for render: {audio_path}",
                context={"scene": request.scene_type},
            )

        duration = max(get_audio_duration(audio_path), 1.0)
        # v16: extract background_image from request.extra for cache key
        bg_img = (getattr(request, 'extra', None) or {}).get('background_image')
        cache_key = _scene_cache_key(
            scene_type=request.scene_type,
            palette=request.palette,
            text=request.text,
            is_ayah=request.is_ayah,
            audio_path=audio_path,
            width=self._video.width,
            height=self._video.height,
            background_image=bg_img,
            color_grade_filter=self._color_grade_filter,  # v17
        )
        cache_file = self._paths.scene_cache / f"{cache_key}.mp4"

        # ── Cache hit
        if cache_file.exists() and cache_file.stat().st_size > 1000:
            try:
                shutil.copy(cache_file, request.output_path)
                logger.info(
                    f"♻️ scene cache hit: {request.scene_type} → {cache_file.name}"
                )
                return SceneRenderResult(
                    output_path=request.output_path,
                    duration_sec=duration,
                    width=self._video.width,
                    height=self._video.height,
                )
            except OSError as e:
                logger.warning(f"⚠️ cache copy failed (will re-render): {e}")

        # ── Cache miss → render
        return self._render_fresh(request, audio_path, duration, cache_file)

    # ───────────────────────────────────────────────────────────
    # Internal render path
    # ───────────────────────────────────────────────────────────
    def _render_fresh(
        self,
        request: SceneRenderRequest,
        audio_path: str,
        duration: float,
        cache_file: Path,
    ) -> SceneRenderResult:
        # 1) Build HTML (v15: pass logo + font paths)
        extra = getattr(request, 'extra', None) or {}

        # v15: Resolve logo + font paths from PathsConfig
        logo_path = None
        font_path = None
        try:
            if self._paths.logo_primary.exists():
                logo_path = str(self._paths.logo_primary.absolute())
        except Exception:
            pass
        try:
            if self._paths.amiri_font.exists():
                font_path = str(self._paths.amiri_font.absolute())
        except Exception:
            pass

        html = build_scene_html(
            scene_type=request.scene_type,
            palette_name=request.palette,
            text=request.text,
            is_ayah=request.is_ayah,
            duration_sec=duration,
            channel_name_ar=self._branding.channel_name_ar,
            channel_name_en=self._branding.channel_name_en,
            particle_count=self._proc.particle_count,
            text_style=extra.get('text_style', 'narrator'),
            scene_emotion=extra.get('scene_emotion', 'warm'),
            logo_path=logo_path,
            font_path=font_path,
            background_image=extra.get('background_image'),  # v16
        )

        unique_id = uuid.uuid4().hex[:10]
        html_file = (
            self._paths.html_templates
            / f"scene_{request.scene_type}_{unique_id}.html"
        )
        html_file.write_text(html, encoding="utf-8")

        webm_dir = self._paths.web_renders / unique_id
        webm_dir.mkdir(parents=True, exist_ok=True)

        # 2) Acquire browser + record
        try:
            with self._pool.acquire(timeout_sec=60.0) as browser:
                webm_path = self._record_webm(
                    browser=browser,
                    html_file=html_file,
                    webm_dir=webm_dir,
                    duration_sec=duration,
                )
        except Exception as e:
            self._cleanup(html_file, webm_dir)
            raise VisualRenderError(
                f"Render failed for {request.scene_type}: {e}",
                cause=e,
            ) from e

        # 3) Encode webm + audio → mp4 (atomic rename)
        try:
            tmp_mp4 = Path(request.output_path).parent / f"{Path(request.output_path).stem}_tmp.mp4"
            self._assembler.encode_segment(
                webm_input=str(webm_path),
                audio_input=audio_path,
                output_path=str(tmp_mp4),
                max_duration=duration,
                video_filter=self._color_grade_filter,  # v17 inline color grade
            )
            tmp_mp4.replace(request.output_path)
        except Exception as e:
            self._cleanup(html_file, webm_dir)
            raise VisualRenderError(
                f"Encode failed for {request.scene_type}: {e}",
                cause=e,
            ) from e

        # 4) Cache copy (best-effort)
        try:
            shutil.copy(request.output_path, cache_file)
        except OSError as e:
            logger.warning(f"⚠️ scene cache write failed: {e}")

        # 5) Cleanup intermediates
        self._cleanup(html_file, webm_dir)

        return SceneRenderResult(
            output_path=request.output_path,
            duration_sec=duration,
            width=self._video.width,
            height=self._video.height,
        )

    def _record_webm(
        self,
        *,
        browser,
        html_file: Path,
        webm_dir: Path,
        duration_sec: float,
    ) -> Path:
        """Record one scene to webm via Playwright. Returns webm path."""
        ctx = browser.new_context(
            viewport={
                "width": self._video.width,
                "height": self._video.height,
            },
            record_video_dir=str(webm_dir),
            record_video_size={
                "width": self._video.width,
                "height": self._video.height,
            },
        )
        try:
            page = ctx.new_page()
            page.goto(f"file://{html_file.absolute()}")
            wait_ms = (
                self._proc.render_warmup_ms
                + int(duration_sec * 1000)
                + 200
            )
            page.wait_for_timeout(wait_ms)
        finally:
            # Closing the context flushes the video
            ctx.close()

        # Find the produced webm
        webms = list(webm_dir.glob("*.webm"))
        if not webms:
            raise VisualRenderError(
                f"Playwright produced no webm in {webm_dir}"
            )
        return webms[0]

    def _cleanup(self, html_file: Path, webm_dir: Path) -> None:
        try:
            html_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            shutil.rmtree(webm_dir, ignore_errors=True)
        except OSError:
            pass
