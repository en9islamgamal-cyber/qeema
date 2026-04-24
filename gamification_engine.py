"""
gamification_engine.py — VALUE / QEEMA v5.0 (AI Driven)

- Multi-event AI gamification
- Dynamic overlays
- Production-grade ffmpeg pipeline
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess as sp
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ai_director import AIDirector

if TYPE_CHECKING:
    from script_engine import EpisodeScript

logger = logging.getLogger(__name__)


# =========================
# CONFIGURATION
# =========================

@dataclass(frozen=True)
class RenderProfile:
    crf: int = 18
    preset: str = "slow"
    codec: str = "libx264"
    pix_fmt: str = "yuv420p"
    profile: str = "high"


@dataclass(frozen=True)
class GamificationSettings:
    logo_size: int = 160
    logo_opacity: float = 0.85
    margin: int = 30

    bar_height: int = 12
    bar_bg_alpha: float = 0.6
    bar_color: str = "0xFFD700"

    font_size: int = 72
    text_y_ratio: float = 0.15

    fade: float = 0.5
    text_duration: float = 4.0

    max_retries: int = 3
    timeout: int = 900


ENCOURAGEMENTS = [
    "👍 ممتاز!",
    "💪 استمر!",
    "🎯 قمة التركيز",
    "🧠 عبقرية",
    "🚀 أداء مذهل!",
    "🔥 أنت في القمة!",
]


# =========================
# ENGINE
# =========================

class GamificationEngine:

    def __init__(self, font_path: Optional[str] = None):
        self.font = Path(font_path) if font_path else None
        self.settings = GamificationSettings()
        self.profile = RenderProfile()
        self.ai_director = AIDirector()

        if self.font and not self.font.exists():
            logger.warning("Font not found → disabling text")
            self.font = None

    # =========================
    # PROBE
    # =========================

    def _probe(self, video: str):
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                video
            ]
            duration = float(sp.run(cmd, capture_output=True, text=True).stdout.strip())
            audio = self._has_audio(video)

            return {"duration": duration, "has_audio": audio}
        except:
            return {"duration": 0, "has_audio": False}

    def _has_audio(self, video):
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            video
        ]
        out = sp.run(cmd, capture_output=True, text=True).stdout.strip()
        return bool(out)

    # =========================
    # TEXT
    # =========================

    def _pick_text(self, script: Optional["EpisodeScript"]):
        if not script:
            return random.choice(ENCOURAGEMENTS)

        text_blob = str(vars(script)).lower()

        if "quiz" in text_blob:
            return "🎯 ركّز! انت قدها"
        if "learn" in text_blob:
            return "📘 فهم ممتاز!"
        if "kids" in text_blob:
            return "🌟 بطل!"

        return random.choice(ENCOURAGEMENTS)

    def _sanitize(self, text: str):
        text = unicodedata.normalize("NFC", text)
        return text.replace(":", "\\:").replace("'", "\\'")

    # =========================
    # FILTER BUILDER
    # =========================

    def _build_filters(self, duration, events, text, has_logo):

        s = self.settings
        parts = []

        parts.append(
            "[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v0]"
        )

        current = "v0"

        # Logo
        if has_logo:
            parts.append(
                f"[1:v]scale={s.logo_size}:-1,format=rgba,colorchannelmixer=aa={s.logo_opacity}[wm]"
            )
            parts.append(
                f"[{current}][wm]overlay=W-w-{s.margin}:{s.margin}[v1]"
            )
            current = "v1"

        # Progress bar
        parts.append(
            f"[{current}]drawbox=0:H-{s.bar_height}:W:{s.bar_height}:black@{s.bar_bg_alpha}:t=fill[v2]"
        )

        parts.append(
            f"[v2]drawbox=0:H-{s.bar_height}:W*(t/{duration}):{s.bar_height}:{s.bar_color}@0.9:t=fill[v3]"
        )

        current = "v3"

        # 🔥 MULTI EVENTS
        for i, event in enumerate(events):
            start = event["time"]
            end = start + s.text_duration

            alpha = (
                f"if(lt(t\\,{start+s.fade}),(t-{start})/{s.fade},"
                f"if(gt(t\\,{end-s.fade}),({end}-t)/{s.fade},1))"
            )

            event_text = self._pick_text(None)

            parts.append(
                f"[{current}]drawtext=fontfile='{self.font}':"
                f"text='{self._sanitize(event_text)}':"
                f"fontsize={s.font_size}:fontcolor=yellow:"
                f"x=(W-text_w)/2:y=H*{s.text_y_ratio}:"
                f"alpha='{alpha}':enable='between(t,{start},{end})'[v{i}]"
            )

            current = f"v{i}"

        parts.append(f"[{current}]null[vout]")

        return ";".join(parts)

    # =========================
    # EXECUTION
    # =========================

    def _run(self, cmd):
        try:
            res = sp.run(cmd, capture_output=True, text=True, timeout=self.settings.timeout)
            if res.returncode != 0:
                logger.error(res.stderr[-500:])
                return False
            return True
        except:
            return False

    def _execute_with_retry(self, cmd):
        for i in range(self.settings.max_retries):
            logger.info(f"Attempt {i+1}")
            if self._run(cmd):
                return True
            time.sleep(1.5 * (i + 1))
        return False

    # =========================
    # MAIN
    # =========================

    def apply_to_episode(self, video_path, script, output_path):

        video = Path(video_path)
        output = Path(output_path)

        if not video.exists():
            return video_path

        meta = self._probe(str(video))

        if meta["duration"] <= 0:
            shutil.copy(video, output)
            return str(output)

        # 🧠 AI EVENTS
        events = self.ai_director.decide_events(video_path, meta["duration"])

        has_logo = Path("assets/logo.png").exists()

        inputs = ["-y", "-i", str(video)]

        if has_logo:
            inputs += ["-i", "assets/logo.png"]

        filters = self._build_filters(meta["duration"], events, "", has_logo)

        cmd = inputs + [
            "-filter_complex", filters,
            "-map", "[vout]"
        ]

        if meta["has_audio"]:
            cmd += ["-map", "0:a?", "-c:a", "copy"]
        else:
            cmd += ["-an"]

        cmd += [
            "-c:v", self.profile.codec,
            "-crf", str(self.profile.crf),
            "-preset", self.profile.preset,
            "-pix_fmt", self.profile.pix_fmt,
            "-movflags", "+faststart",
            str(output)
        ]

        success = self._execute_with_retry(cmd)

        if success and output.exists():
            return str(output)

        shutil.copy(video, output)
        return str(output)