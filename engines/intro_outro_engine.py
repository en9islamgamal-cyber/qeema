"""
engines/intro_outro_engine.py — VALUE / QEEMA v15.0
======================================================================
[Changes v15]
- Local Amiri font embedded via @font-face (was Google Fonts CDN — fragile)
- Logo PNG support: if logo.png exists, displays it above text
- Outro now accepts optional `cta_audio_path` — overlays CTA voice over outro
- Intro duration shortened to 3.5s (config-driven)
- Outro duration extended to 7.0s (more time for subscribe CTA voice)
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


def _font_face_block(font_path: Optional[str]) -> str:
    if not font_path:
        return ""
    return f"""
@font-face {{
    font-family: 'Amiri';
    src: url('file://{font_path}') format('truetype');
    font-weight: 700;
    font-display: swap;
}}
"""


def _logo_img_block(logo_path: Optional[str], top_offset: str = "80px") -> str:
    if not logo_path:
        return ""
    return f'''<img src="file://{logo_path}" class="brand-logo" alt="Logo" />'''


def _build_intro_html(
    *,
    duration_sec: float,
    palette: list,
    channel_name_ar: str,
    channel_name_en: str,
    tagline_ar: str,
    font_path: Optional[str] = None,
    logo_path: Optional[str] = None,
) -> str:
    p0, p1, p2, p3, p4 = (palette + ["#FFD700"] * 5)[:5]
    font_face = _font_face_block(font_path)
    google_fonts = "" if font_path else (
        '<link href="https://fonts.googleapis.com/css2?family=Amiri:wght@700&display=swap" rel="stylesheet">'
    )
    logo_img = _logo_img_block(logo_path)

    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
        <meta charset="UTF-8">
        {google_fonts}
        <style>
        {font_face}
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
        .brand-logo {{
            height: 180px;
            width: auto;
            opacity: 0;
            animation: zoomIn 0.9s ease-out 0.2s forwards;
            margin-bottom: 30px;
            filter: drop-shadow(0 0 30px {p3});
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
            animation: fadeUp 0.8s ease-out 1.1s forwards;
        }}
        .tagline {{
            font-size: 38px;
            color: rgba(255,255,255,0.9);
            margin-top: 36px;
            opacity: 0;
            animation: fadeUp 0.8s ease-out 1.7s forwards;
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
            {logo_img}
            <div class="logo-ar">{channel_name_ar}</div>
            <div class="logo-en">{channel_name_en}</div>
            <div class="tagline">{tagline_ar}</div>
        </div>
        <div class="particles" id="particles"></div>
        <script>
        const container = document.getElementById('particles');
        for (let i = 0; i < 40; i++) {{
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
    palette: list,
    channel_name_ar: str,
    channel_name_en: str,
    subscribe_text: str,
    font_path: Optional[str] = None,
    logo_path: Optional[str] = None,
) -> str:
    p0, p1, p2, p3, p4 = (palette + ["#FFD700"] * 5)[:5]
    font_face = _font_face_block(font_path)
    google_fonts = "" if font_path else (
        '<link href="https://fonts.googleapis.com/css2?family=Amiri:wght@700&display=swap" rel="stylesheet">'
    )
    logo_img = _logo_img_block(logo_path)

    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
        <meta charset="UTF-8">
        {google_fonts}
        <style>
        {font_face}
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
        .brand-logo {{
            height: 140px;
            width: auto;
            opacity: 0;
            animation: fadeIn 0.7s ease-out 0.2s forwards;
            margin-bottom: 24px;
            filter: drop-shadow(0 0 24px {p3});
        }}
        .logo-ar {{
            font-size: 160px;
            font-weight: 900;
            color: {p4};
            text-shadow: 0 0 30px {p3};
            letter-spacing: 6px;
            opacity: 0;
            animation: fadeIn 0.7s ease-out 0.3s forwards;
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
            animation: fadeUp 0.8s ease-out 1.0s forwards, pulse 2s ease-in-out 2.0s infinite;
        }}
        .brand-line {{
            font-size: 32px;
            color: rgba(255,255,255,0.85);
            margin-top: 36px;
            letter-spacing: 10px;
            opacity: 0;
            animation: fadeUp 0.8s ease-out 1.6s forwards;
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
            {logo_img}
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


class IntroOutroEngine(IntroOutroBuilder):
    """v15: Build & cache intro/outro clips. Outro can have CTA voice overlay."""

    def __init__(
        self,
        *,
        paths: PathsConfig,
        video_cfg: VideoConfig,
        proc_cfg: ProceduralConfig,
        branding: BrandingConfig,
        assembler: VideoAssembler,
        browser_pool: BrowserPool,
        color_grade_filter: Optional[str] = None,  # v17 — match scene encoding
    ) -> None:
        self._paths: PathsConfig = paths
        self._video: VideoConfig = video_cfg
        self._proc: ProceduralConfig = proc_cfg
        self._branding: BrandingConfig = branding
        self._assembler: VideoAssembler = assembler
        self._pool: BrowserPool = browser_pool
        self._color_grade_filter = color_grade_filter  # v17

        self._intro_palette = ["#FFD700", "#FFCC70", "#FFB347", "#FF8C42", "#FFE5B4"]
        self._outro_palette = ["#1B2631", "#283747", "#34495E", "#FFD700", "#F5B041"]

        paths.branding.mkdir(parents=True, exist_ok=True)

    def _resolve_paths(self):
        font = str(self._paths.amiri_font.absolute()) if self._paths.amiri_font.exists() else None
        logo = str(self._paths.logo_primary.absolute()) if self._paths.logo_primary.exists() else None
        return font, logo

    def build_intro(self) -> str:
        target = self._paths.intro_video
        if target.exists() and target.stat().st_size > 1000:
            logger.info(f"♻️ intro cache hit: {target}")
            return str(target)

        font, logo = self._resolve_paths()
        html = _build_intro_html(
            duration_sec=self._branding.intro_duration_sec,
            palette=self._intro_palette,
            channel_name_ar=self._branding.channel_name_ar,
            channel_name_en=self._branding.channel_name_en,
            tagline_ar=self._branding.channel_tagline_ar,
            font_path=font,
            logo_path=logo,
        )
        return self._render_silent_clip(
            html=html,
            duration=self._branding.intro_duration_sec,
            output=target,
            label="intro",
        )

    def build_outro(self, cta_audio_path: Optional[str] = None) -> str:
        """
        v15: optional cta_audio_path overlays CTA voice onto the outro video.
        Outro is rebuilt (not cached) when cta_audio is provided to embed the audio.
        """
        # If CTA audio provided, build a per-episode outro
        if cta_audio_path and Path(cta_audio_path).exists():
            return self._build_outro_with_audio(cta_audio_path)

        target = self._paths.outro_video
        if target.exists() and target.stat().st_size > 1000:
            logger.info(f"♻️ outro cache hit: {target}")
            return str(target)

        font, logo = self._resolve_paths()
        html = _build_outro_html(
            duration_sec=self._branding.outro_duration_sec,
            palette=self._outro_palette,
            channel_name_ar=self._branding.channel_name_ar,
            channel_name_en=self._branding.channel_name_en,
            subscribe_text=self._branding.subscribe_text,
            font_path=font,
            logo_path=logo,
        )
        return self._render_silent_clip(
            html=html,
            duration=self._branding.outro_duration_sec,
            output=target,
            label="outro",
        )

    def _build_outro_with_audio(self, cta_audio_path: str) -> str:
        """v15: Build outro and overlay CTA voice."""
        from infrastructure.audio_utils import get_audio_duration

        cta_duration = get_audio_duration(cta_audio_path)
        # Outro at least as long as CTA + small tail
        outro_duration = max(self._branding.outro_duration_sec, cta_duration + 1.5)

        font, logo = self._resolve_paths()
        html = _build_outro_html(
            duration_sec=outro_duration,
            palette=self._outro_palette,
            channel_name_ar=self._branding.channel_name_ar,
            channel_name_en=self._branding.channel_name_en,
            subscribe_text=self._branding.subscribe_text,
            font_path=font,
            logo_path=logo,
        )
        # Use a per-episode output path
        unique = uuid.uuid4().hex[:8]
        out = self._paths.branding / f"outro_with_cta_{unique}.mp4"
        return self._render_clip_with_audio(
            html=html,
            duration=outro_duration,
            output=out,
            audio_path=cta_audio_path,
            label="outro_cta",
        )

    def wrap_episode(self, raw_video: str, output_path: str,
                     cta_audio_path: Optional[str] = None) -> str:
        intro = self.build_intro()
        outro = self.build_outro(cta_audio_path=cta_audio_path)

        for p in (intro, raw_video, outro):
            if not Path(p).exists():
                raise VisualRenderError(f"Missing clip for wrap: {p}")

        # v17 BREAKTHROUGH: try stream-copy first.
        # Since intro/outro/body now use IDENTICAL VideoConfig + identical
        # color grade filter, their codec params match → stream-copy works
        # → wrap completes in ~5 seconds instead of 15 minutes.
        # Only falls back to re-encode if stream-copy fails.
        return self._assembler.concat(
            [intro, raw_video, outro],
            output_path,
            re_encode=False,  # was True in v15 — caused 900s timeout
        )

    def _render_silent_clip(
        self, *, html: str, duration: float, output: Path, label: str,
    ) -> str:
        silent_audio = self._make_silence(duration)
        return self._render_clip_with_audio(
            html=html, duration=duration, output=output,
            audio_path=silent_audio, label=label,
        )

    def _render_clip_with_audio(
        self, *, html: str, duration: float, output: Path,
        audio_path: str, label: str,
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

            tmp_mp4 = output.parent / f"{output.stem}_tmp.mp4"
            self._assembler.encode_segment(
                webm_input=str(webm_path),
                audio_input=audio_path,
                output_path=str(tmp_mp4),
                max_duration=duration,
                video_filter=self._color_grade_filter,  # v17 match scenes
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
        # v15.1 fix: codec MUST match container extension.
        # Was: aac codec + .mp3 extension → ffmpeg fails with "Nothing was written"
        # Now: libmp3lame codec + .mp3 extension (matches), modern lavfi syntax.
        cache = self._paths.branding / f"silence_{duration:.2f}s.mp3"
        if cache.exists() and cache.stat().st_size > 100:
            return str(cache)
        import subprocess as sp
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{duration:.3f}",
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            str(cache),
        ]
        result = sp.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise VisualRenderError(f"Silence synthesis failed: {result.stderr[-300:]}")
        return str(cache)
