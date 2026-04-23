from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

from config import Paths, SubtitleConfig, VideoConfig
from models import EpisodeScript

logger = logging.getLogger(__name__)


def _run(cmd: list[str], label: str = "", timeout: int = 900) -> bool:
    logger.info("▶ %s", label)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error("❌ %s
%s", label, r.stderr[-2000:])
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("⏱️ Timeout: %s", label)
        return False


def _probe(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}")
    return json.loads(r.stdout)


def _probe_duration(path: str) -> float:
    try:
        data = _probe(path)
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


def _sha_name(text: str, prefix: str, ext: str = ".png") -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{h}{ext}"


def _get_font() -> str:
    candidates = [
        Paths.FONTS / "Amiri-Bold.ttf",
        Paths.FONTS / "NotoNaskhArabic-Regular.ttf",
        Paths.FONTS / "Cairo-Bold.ttf",
        Paths.FONTS / "NotoSansArabic-Regular.ttf",
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    system_fonts = [
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    ]
    for f in system_fonts:
        if Path(f).exists():
            return f
    return ""


class SubtitleOverlay:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.font_path = _get_font()

    def _fit_font_size(self, draw, text: str, max_width: int, start_size: int) -> int:
        try:
            from PIL import ImageFont
        except ImportError:
            return start_size

        size = start_size
        while size > 24:
            try:
                font = ImageFont.truetype(self.font_path, size) if self.font_path else ImageFont.load_default()
                bbox = draw.multiline_textbbox((0, 0), text, font=font, align="center", spacing=12)
                if (bbox[2] - bbox[0]) <= max_width:
                    return size
            except Exception:
                pass
            size -= 2
        return 24

    def _shape_arabic(self, text: str) -> str:
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            reshaped = arabic_reshaper.reshape(text)
            return get_display(reshaped)
        except Exception:
            return text

    def _wrap_text(self, text: str, max_chars: int) -> str:
        parts = text.replace("
", " ").split()
        lines = []
        current = ""
        for w in parts:
            candidate = f"{current} {w}".strip()
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = w
        if current:
            lines.append(current)
        return "
".join(lines)

    def create(
        self,
        text: str,
        output_path: str,
        font_size: int = 60,
        text_color: tuple[int, int, int] = (255, 255, 255),
        is_ayah: bool = False,
    ) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            logger.warning("Pillow is missing")
            return ""

        if not text.strip():
            return ""

        wrap_width = 34 if is_ayah else 46
        wrapped = self._wrap_text(text, wrap_width)
        display = self._shape_arabic(wrapped)

        img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        try:
            font_size = self._fit_font_size(draw, display, int(self.width * 0.86), font_size)
            font = ImageFont.truetype(self.font_path, font_size) if self.font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.multiline_textbbox((0, 0), display, font=font, align="center", spacing=12)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

        x = (self.width - tw) // 2
        y = self.height - th - SubtitleConfig.MARGIN_BOTTOM_H

        pad_x = SubtitleConfig.BOX_PADDING
        pad_y = SubtitleConfig.BOX_PADDING
        bg_rect = [x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y]

        draw.rounded_rectangle(
            bg_rect,
            radius=SubtitleConfig.BOX_BORDER_RADIUS,
            fill=(0, 0, 0, 165),
            outline=(212, 175, 55, 180) if is_ayah else (255, 255, 255, 50),
            width=2
        )

        shadow_offset = SubtitleConfig.SHADOW_OFFSET
        draw.multiline_text(
            (x + shadow_offset, y + shadow_offset),
            display,
            font=font,
            fill=(0, 0, 0, 210),
            align="center",
            spacing=12,
        )

        color = (255, 215, 0, 255) if is_ayah else (*text_color, 255)
        draw.multiline_text(
            (x, y),
            display,
            font=font,
            fill=color,
            align="center",
            spacing=12,
        )

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, "PNG")
        return output_path


class SegmentBuilder:
    def __init__(self, width: int = 1920, height: int = 1080):
        self.W = width
        self.H = height
        self.sub = SubtitleOverlay(width, height)

    def _base_filter(self, duration: float, zoom: bool = True, zoom_speed: float = 0.00055) -> str:
        if zoom:
            frames = max(int(duration * VideoConfig.FPS), 1)
            max_z = min(1.0 + zoom_speed * frames, 1.08)
            return (
                f"scale=8000:-1:flags=lanczos,"
                f"zoompan=z='min(zoom+{zoom_speed},{max_z})':d={frames}:"
                f"s={self.W}x{self.H}:fps={VideoConfig.FPS}"
            )
        return f"scale={self.W}:{self.H}:force_original_aspect_ratio=decrease,pad={self.W}:{self.H}:(ow-iw)/2:(oh-ih)/2"

    def _audio_fade_filter(self, duration: float) -> str:
        fade_out_start = max(duration - 0.35, 0.01)
        return (
            f"afade=t=in:st=0:d=0.20,"
            f"afade=t=out:st={fade_out_start:.3f}:d=0.20,"
            f"aresample=async=1:first_pts=0"
        )

    def _subtitle_inputs(self, subtitle_text: Optional[str], is_ayah: bool, tag: str) -> Tuple[list[str], str]:
        if not subtitle_text:
            return [], ""
        safe_name = _sha_name(subtitle_text, tag)
        sub_path = str(Paths.ASSEMBLY / safe_name)
        created = self.sub.create(
            subtitle_text,
            sub_path,
            font_size=SubtitleConfig.FONT_SIZE_LARGE if is_ayah else SubtitleConfig.FONT_SIZE_MEDIUM,
            is_ayah=is_ayah,
        )
        if created and Path(created).exists():
            return ["-i", created], created
        return [], ""

    def build_segment(
        self,
        image_path: str,
        audio_path: str,
        output_path: str,
        subtitle_text: Optional[str] = None,
        duration: Optional[float] = None,
        use_ken_burns: bool = True,
        is_ayah: bool = False,
        tag: str = "sub",
    ) -> str:
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"Missing audio: {audio_path}")

        dur = duration or _probe_duration(audio_path)
        if dur <= 0:
            raise RuntimeError(f"Invalid duration for {audio_path}")

        img = image_path if Path(image_path).exists() else str(Paths.ASSETS / "default_bg.png")
        sub_inputs, sub_path = self._subtitle_inputs(subtitle_text, is_ayah, tag)

        vf = self._base_filter(dur, zoom=use_ken_burns)
        maps = ["[0:v]" + vf + "[v]"]
        if sub_path:
            maps.append(f"[1:v]format=rgba[sv];[v][sv]overlay=0:0[vout]")
            final_map = "[vout]"
        else:
            final_map = "[v]"

        fc = ";".join(maps)

        audio_input_index = 1 + len(sub_inputs)
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", img] + sub_inputs + ["-i", audio_path, "-filter_complex", fc]

        if sub_path:
            cmd += ["-map", final_map]
        else:
            cmd += ["-map", "[v]"]

        cmd += [
            "-map", f"{audio_input_index}:a:0",
            "-af", self._audio_fade_filter(dur),
            "-c:v", VideoConfig.CODEC,
            "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF),
            "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT,
            "-c:a", VideoConfig.AUDIO_CODEC,
            "-b:a", VideoConfig.AUDIO_BITRATE,
            "-ar", str(VideoConfig.AUDIO_RATE),
            "-movflags", "+faststart",
            "-shortest",
            output_path,
        ]

        _run(cmd, f"Build segment {Path(output_path).name}")
        return output_path

    def concatenate(self, segments: list[str], output_path: str) -> str:
        valid = [s for s in segments if Path(s).exists()]
        if not valid:
            raise RuntimeError("No valid segments")

        if len(valid) == 1:
            shutil.copy2(valid[0], output_path)
            return output_path

        concat_list = Paths.ASSEMBLY / "concat_list.txt"
        concat_list.write_text("
".join(f"file '{os.path.abspath(s)}'" for s in valid), encoding="utf-8")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        _run(cmd, f"Concat {len(valid)} segments")
        return output_path


class VideoEngine:
    def __init__(self, vertical: bool = False):
        self.vertical = vertical
        self.W = VideoConfig.WIDTH_V if vertical else VideoConfig.WIDTH_H
        self.H = VideoConfig.HEIGHT_V if vertical else VideoConfig.HEIGHT_H
        self.builder = SegmentBuilder(self.W, self.H)
        Paths.ensure_all()

    def _add_segment_if_exists(self, path: str, segments: list[str]) -> None:
        if Path(path).exists():
            segments.append(path)

    def assemble_episode(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info("🎬 Assembling episode %s", script.episode_number)

        seg_dir = Path(ep_dir) / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        Paths.ASSEMBLY.mkdir(parents=True, exist_ok=True)

        segments: list[str] = []

        if getattr(script.intro_scene, "audio_path", None):
            out = str(seg_dir / "00_intro.mp4")
            self.builder.build_segment(
                image_path=script.intro_scene.image_path or "",
                audio_path=script.intro_scene.audio_path,
                subtitle_text=getattr(script.intro_scene, "narrator_text", None),
                output_path=out,
                use_ken_burns=True,
                is_ayah=False,
                tag="intro",
            )
            self._add_segment_if_exists(out, segments)

        for i, ayah_scene in enumerate(script.ayah_scenes):
            sid = ayah_scene.scene_id

            if getattr(ayah_scene, "intro_audio", None):
                p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_intro.mp4")
                self.builder.build_segment(
                    ayah_scene.image_path or "",
                    ayah_scene.intro_audio,
                    p,
                    subtitle_text=getattr(ayah_scene, "intro_text", None),
                    is_ayah=False,
                    tag=f"ayah_{sid}_intro",
                )
                self._add_segment_if_exists(p, segments)

            if getattr(ayah_scene, "quran_audio", None):
                p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_quran.mp4")
                self.builder.build_segment(
                    ayah_scene.image_path or "",
                    ayah_scene.quran_audio,
                    p,
                    subtitle_text=getattr(ayah_scene.ayah, "text", None),
                    use_ken_burns=True,
                    is_ayah=True,
                    tag=f"ayah_{sid}_quran",
                )
                self._add_segment_if_exists(p, segments)

            if getattr(ayah_scene, "explain_audio", None):
                p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_explain.mp4")
                self.builder.build_segment(
                    ayah_scene.image_path or "",
                    ayah_scene.explain_audio,
                    p,
                    subtitle_text=getattr(ayah_scene, "explain_text", None),
                    use_ken_burns=True,
                    is_ayah=False,
                    tag=f"ayah_{sid}_explain",
                )
                self._add_segment_if_exists(p, segments)

        for j, mid in enumerate(script.mid_scenes):
            if getattr(mid, "audio_path", None):
                p = str(seg_dir / f"mid_{j:02d}.mp4")
                self.builder.build_segment(
                    mid.image_path or "",
                    mid.audio_path,
                    p,
                    subtitle_text=getattr(mid, "narrator_text", None),
                    use_ken_burns=True,
                    is_ayah=False,
                    tag=f"mid_{j}",
                )
                self._add_segment_if_exists(p, segments)

        if getattr(script.outro_scene, "audio_path", None):
            out = str(seg_dir / "99_outro.mp4")
            self.builder.build_segment(
                script.outro_scene.image_path or "",
                script.outro_scene.audio_path,
                out,
                subtitle_text=getattr(script.outro_scene, "narrator_text", None),
                use_ken_burns=True,
                is_ayah=False,
                tag="outro",
            )
            self._add_segment_if_exists(out, segments)

        if not segments:
            raise RuntimeError("No generated segments")

        raw_output = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4")
        self.builder.concatenate(segments, raw_output)

        logger.info("✅ Raw video ready: %s", raw_output)
        return raw_output