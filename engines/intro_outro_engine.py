"""
engines/intro_outro_engine.py — VALUE / QEEMA v11.0 (Production)
======================================================================
Branded intro and outro clip builder.

[Responsibilities]
  - build_intro()       : 5s opening (logo + قِيمَة/VALUE + tagline + particles)
  - build_outro()       : 5s closing (subscribe CTA + brand)
  - wrap_episode(...)   : concat intro + main_video + outro

[Caching]
Intro/outro are deterministic: built once at warmup time, reused for
every episode. Path: paths.intro_video / paths.outro_video.

[Bug fix vs v10]
The old code redefined `-i input` three times in the wrap step, leading
to ffmpeg picking only the last input. Now we use the concat demuxer
with a list file.
"""
from __future__ import annotations

import logging
import textwrap
import uuid
from pathlib import Path
from typing import Optional

from core.config import (
    BrandingConfig,
    PathsConfig,
    ProceduralConfig,
    VideoConfig,
)
from core.exceptions import VisualRenderError
from core.interfaces import IntroOutroBuilder, VideoAssembler
from infrastructure.browser_pool import BrowserPool

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Static intro HTML (no audio, fixed length)
# ════════════════════════════════════════════════════════════════
def _build_intro_html(
    *,
    duration_sec: float,
    palette: list[str],
    channel_name_ar: str,
    channel_name_en: str,
    tagline_ar: str,
) -> str:
    p0, p1, p2, p3, p4 = (palette + ["#FFD700"] * 5)[:5]
    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
        <meta charset="UTF-8">
        <link href="https://fonts.googleapis.com/css2?family=Amiri:wght@700&display=swap" rel="stylesheet">
        <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{
            width: 100%; height: 100%;
            background: radial-gradient(ellipse at center, {p0} 0%, #1a1a2e 100%);
            overflow: hidden;
            font-family: 'Amiri', serif;
            direction: rtl;
        }}
        .stage {{
            position: fixed; inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-direction: column;
        }}
        .logo-ar {{
            font-size: 220px;
            font-weight: 900;
            color: {p4};
            text-shadow:
                0 0 40px {p3},
                0 0 80px {p3};
            letter-spacing: 8px;
            opacity: 0;
            animation: zoomIn 1.0s ease-out 0.4s forwards;
        }}
        .logo-en {{
            font-size: 80px;
            font-weight: 700;
            color: #FFE5B4;
            letter-spacing: 16px;
            margin-top: 16px;
            opacity: 0;
            animation: fadeUp 0.9s ease-out 1.2s forwards;
        }}
        .tagline {{
            font-size: 38px;
            color: rgba(255,255,255,0.9);
            margin-top: 36px;
            opacity: 0;
            animation: fadeUp 0.9s ease-out 2.0s forwards;
        }}
        @keyframes zoomIn {{
            0%   {{ opacity: 0; transform: scale(0.6); }}
            100% {{ opacity: 1; transform: scale(1); }}
        }}
        @keyframes fadeUp {{
            0%   {{ opacity: 0; transform: translateY(30px); }}
            100% {{ opacity: 1; transform: translateY(0); }}
        }}
        .particles {{
            position: absolute; inset: 0;
            pointer-events: none;
            overflow: hidden;
        }}
        .particle {{
            position: absolute;
            width: 6px; height: 6px;
            background: {p3};
            border-radius: 50%;
            box-shadow: 0 0 12px {p3};
            animation: pRise linear infinite;
        }}
        @keyframes pRise {{
            0%   {{ transform: translateY(100vh); opacity: 0; }}
            10%  {{ opacity: 0.8; }}
            90%  {{ opacity: 0.8; }}
            100% {{ transform: translateY(-10vh); opacity: 0; }}
        }}
        </style>
        </head>
        <body>
        <div class="stage">
            <div class="logo-ar">{channel_name_ar}</div>
            <div class="logo-en">{channel_name_en}</div>
            <div class="tagline">{tagline_ar}</div>
        </div>
        <div class="particles" id="particles"></div>
        <script>
        // Generate 50 golden particles with random delays
        const container = document.getElementById('particles');
        for (let i = 0; i < 50; i++) {{
            const p = document.createElement('div');
            p.className = 'particle';
            p.style.left = (Math.random() * 100) + 'vw';
            p.style.animationDuration = (4 + Math.random() * 6) + 's';
            p.style.animationDelay = (Math.random() * 5) + 's';
            container.appendChild(p);
        }}
        </script>
        </body>
        </html>
    """)


def _build_outro_html(
    *,
    duration_sec: float,
    palette: list[str],
    channel_name_ar: str,
    channel_name_en: str,
    subscribe_text: str,
) -> str:
    p0, p1, p2, p3, p4 = (palette + ["#FFD700"] * 5)[:5]
    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
        <meta charset="UTF-8">
        <link href="https://fonts.googleapis.com/css2?family=Amiri:wght@700&display=swap" rel="stylesheet">
        <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{
            width: 100%; height: 100%;
            background: radial-gradient(ellipse at center, {p0} 0%, #16213e 100%);
            overflow: hidden;
            font-family: 'Amiri', serif;
            direction: rtl;
        }}
        .stage {{
            position: fixed; inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-direction: column;
        }}
        .logo-ar {{
            font-size: 160px;
            font-weight: 900;
            color: {p4};
            text-shadow: 0 0 30px {p3};
            letter-spacing: 6px;
            opacity: 0;
            animation: fadeIn 0.8s ease-out 0.3s forwards;
        }}
        .subscribe {{
            font-size: 64px;
            font-weight: 700;
            color: #FFFFFF;
            margin-top: 50px;
            padding: 28px 80px;
            background: linear-gradient(135deg, {p3} 0%, {p4} 100%);
            border-radius: 70px;
            box-shadow: 0 12px 40px rgba(0,0,0,0.6);
            opacity: 0;
            animation: fadeUp 0.9s ease-out 1.2s forwards, pulse 2s ease-in-out 2.2s infinite;
        }}
        .brand-line {{
            font-size: 32px;
            color: rgba(255,255,255,0.85);
            margin-top: 36px;
            letter-spacing: 10px;
            opacity: 0;
            animation: fadeUp 0.9s ease-out 2.0s forwards;
        }}
        @keyframes fadeIn {{
            0%   {{ opacity: 0; }}
            100% {{ opacity: 1; }}
        }}
        @keyframes fadeUp {{
            0%   {{ opacity: 0; transform: translateY(30px); }}
            100% {{ opacity: 1; transform: translateY(0); }}
        }}
        @keyframes pulse {{
            0%, 100% {{ transform: scale(1); }}
            50%      {{ transform: scale(1.05); }}
        }}
        .particles {{
            position: absolute; inset: 0;
            pointer-events: none;
            overflow: hidden;
        }}
        .particle {{
            position: absolute;
            width: 5px; height: 5px;
            background: {p3};
            border-radius: 50%;
            box-shadow: 0 0 10px {p3};
            animation: pRise linear infinite;
        }}
        @keyframes pRise {{
            0%   {{ transform: translateY(100vh); opacity: 0; }}
            10%  {{ opacity: 0.7; }}
            90%  {{ opacity: 0.7; }}
            100% {{ transform: translateY(-10vh); opacity: 0; }}
        }}
        </style>
        </head>
        <body>
        <div class="stage">
            <div class="logo-ar">{channel_name_ar}</div>
            <div class="subscribe">🔔 {subscribe_text}</div>
            <div class="brand-line">{channel_name_en}</div>
        </div>
        <div class="particles" id="particles"></div>
        <script>
        const container = document.getElementById('particles');
        for (let i = 0; i < 40; i++) {{
            const p = document.createElement('div');
            p.className = 'particle';
            p.style.left = (Math.random() * 100) + 'vw';
            p.style.animationDuration = (5 + Math.random() * 5) + 's';
            p.style.animationDelay = (Math.random() * 4) + 's';
            container.appendChild(p);
        }}
        </script>
        </body>
        </html>
    """)


# ════════════════════════════════════════════════════════════════
# IntroOutroEngine
# ════════════════════════════════════════════════════════════════
class IntroOutroEngine(IntroOutroBuilder):
    """Build & cache 5s intro/outro clips, then wrap final episodes."""

    def __init__(
        self,
        *,
        paths: PathsConfig,
        video_cfg: VideoConfig,
        proc_cfg: ProceduralConfig,
        branding: BrandingConfig,
        assembler: VideoAssembler,
        browser_pool: BrowserPool,
    ) -> None:
        self._paths: PathsConfig = paths
        self._video: VideoConfig = video_cfg
        self._proc: ProceduralConfig = proc_cfg
        self._branding: BrandingConfig = branding
        self._assembler: VideoAssembler = assembler
        self._pool: BrowserPool = browser_pool

        # Default golden palette (overridable later)
        self._intro_palette = ["#FFD700", "#FFCC70", "#FFB347", "#FF8C42", "#FFE5B4"]
        self._outro_palette = ["#1B2631", "#283747", "#34495E", "#FFD700", "#F5B041"]

        paths.branding.mkdir(parents=True, exist_ok=True)

    # ───────────────────────────────────────────────────────────
    # Build intro/outro (cached on disk)
    # ───────────────────────────────────────────────────────────
    def build_intro(self) -> str:
        target = self._paths.intro_video
        if target.exists() and target.stat().st_size > 1000:
            logger.info(f"♻️ intro cache hit: {target}")
            return str(target)

        html = _build_intro_html(
            duration_sec=self._branding.intro_duration_sec,
            palette=self._intro_palette,
            channel_name_ar=self._branding.channel_name_ar,
            channel_name_en=self._branding.channel_name_en,
            tagline_ar=self._branding.channel_tagline_ar,
        )
        return self._render_silent_clip(
            html=html,
            duration=self._branding.intro_duration_sec,
            output=target,
            label="intro",
        )

    def build_outro(self) -> str:
        target = self._paths.outro_video
        if target.exists() and target.stat().st_size > 1000:
            logger.info(f"♻️ outro cache hit: {target}")
            return str(target)

        html = _build_outro_html(
            duration_sec=self._branding.outro_duration_sec,
            palette=self._outro_palette,
            channel_name_ar=self._branding.channel_name_ar,
            channel_name_en=self._branding.channel_name_en,
            subscribe_text=self._branding.subscribe_text,
        )
        return self._render_silent_clip(
            html=html,
            duration=self._branding.outro_duration_sec,
            output=target,
            label="outro",
        )

    # ───────────────────────────────────────────────────────────
    # Wrap episode: intro + raw + outro
    # ───────────────────────────────────────────────────────────
    def wrap_episode(self, raw_video: str, output_path: str) -> str:
        intro = self.build_intro()
        outro = self.build_outro()

        for p in (intro, raw_video, outro):
            if not Path(p).exists():
                raise VisualRenderError(f"Missing clip for wrap: {p}")

        # [Bug fix vs v10] Use concat demuxer (not -i triple-input)
        # Re-encode to guarantee compatible streams (intro/outro and raw
        # might have different keyframe alignments).
        return self._assembler.concat(
            [intro, raw_video, outro],
            output_path,
            re_encode=True,
        )

    # ───────────────────────────────────────────────────────────
    # Internal: render silent clip + add silent audio track
    # ───────────────────────────────────────────────────────────
    def _render_silent_clip(
        self,
        *,
        html: str,
        duration: float,
        output: Path,
        label: str,
    ) -> str:
        unique = uuid.uuid4().hex[:10]
        html_file = self._paths.html_templates / f"{label}_{unique}.html"
        html_file.write_text(html, encoding="utf-8")
        webm_dir = self._paths.web_renders / f"{label}_{unique}"
        webm_dir.mkdir(parents=True, exist_ok=True)

        try:
            with self._pool.acquire(timeout_sec=60.0) as browser:
                ctx = browser.new_context(
                    viewport={"width": self._video.width, "height": self._video.height},
                    record_video_dir=str(webm_dir),
                    record_video_size={
                        "width": self._video.width,
                        "height": self._video.height,
                    },
                )
                try:
                    page = ctx.new_page()
                    page.goto(f"file://{html_file.absolute()}")
                    wait_ms = self._proc.render_warmup_ms + int(duration * 1000) + 200
                    page.wait_for_timeout(wait_ms)
                finally:
                    ctx.close()

            webms = list(webm_dir.glob("*.webm"))
            if not webms:
                raise VisualRenderError(f"No webm produced for {label}")
            webm_path = webms[0]

            # Use a synthesized silent audio track
            silent_audio = self._make_silence(duration)
            tmp_mp4 = output.with_suffix(".mp4.tmp")

            self._assembler.encode_segment(
                webm_input=str(webm_path),
                audio_input=silent_audio,
                output_path=str(tmp_mp4),
                max_duration=duration,
            )
            tmp_mp4.replace(output)

            logger.info(f"✅ {label} clip built: {output}")
            return str(output)
        finally:
            try:
                html_file.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                import shutil
                shutil.rmtree(webm_dir, ignore_errors=True)
            except OSError:
                pass

    def _make_silence(self, duration: float) -> str:
        """Create a silent MP3 of given duration (cached)."""
        cache = self._paths.branding / f"silence_{duration:.2f}s.mp3"
        if cache.exists() and cache.stat().st_size > 100:
            return str(cache)
        import subprocess as sp
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=stereo",
            "-t", f"{duration:.3f}",
            "-c:a", "libmp3lame",
            "-q:a", "2",
            str(cache),
        ]
        result = sp.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise VisualRenderError(
                f"Silence synthesis failed: {result.stderr[-200:]}"
            )
        return str(cache)
