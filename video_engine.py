"""
video_engine.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
═══════════════════════════════════════════════════════
محرك تجميع الفيديو (Lossless Assembly Engine)
• دمج لحظي بدون فقدان جودة (Zero-Loss Concat)
• معالجة النصوص العربية الطويلة (Word Wrapping)
• تنعيم حواف الصوت (Audio Fades) لمنع الطقطقة
• تصدير الفيديو "خاماً" ليتم تشطيبه نهائياً في محرك التلعيب
═══════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

from config import Paths, SubtitleConfig, VideoConfig
from models import AyahScene, EpisodeScript, NarratorScene

logger = logging.getLogger(__name__)


def _run(cmd: list[str], label: str = "", timeout: int = 600) -> bool:
    logger.info(f"▶ {label}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error(f"❌ {label}:\n{r.stderr[-600:]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"⏱️ Timeout: {label}")
        return False


def _probe_duration(path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 10.0


def _get_font() -> str:
    primary_font = Paths.FONTS / "Amiri-Bold.ttf"
    if primary_font.exists():
        return str(primary_font)

    font_dir = Paths.FONTS
    for ext in ["*.ttf", "*.otf"]:
        fonts = list(font_dir.glob(ext))
        if fonts:
            return str(fonts[0])

    system_fonts = [
        "/usr/share/fonts/truetype/arabic/Amiri-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    ]
    for f in system_fonts:
        if Path(f).exists():
            return f
    return ""


class SubtitleOverlay:
    """يولد صور overlay للسبتايتل مع دعم للأسطر المتعددة"""

    def __init__(self, width: int, height: int):
        self.width  = width
        self.height = height

    def create(
        self,
        text: str,
        output_path: str,
        font_size: int = 60,
        text_color: tuple = (255, 255, 255),
        is_ayah: bool = False,
    ) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFont
            import arabic_reshaper
            from bidi.algorithm import get_display
        except ImportError:
            logger.warning("⚠️ مكتبات الخطوط غير مثبتة (Pillow, arabic-reshaper, python-bidi)")
            return ""

        wrap_width = 40 if is_ayah else 55 
        wrapped_text = textwrap.fill(text, width=wrap_width)

        try:
            reshaped = arabic_reshaper.reshape(wrapped_text)
            display  = get_display(reshaped)
        except Exception:
            display = wrapped_text

        img  = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        font_path = _get_font()
        try:
            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.multiline_textbbox((0, 0), display, font=font, align="center")
        tw   = bbox[2] - bbox[0]
        th   = bbox[3] - bbox[1]

        margin = SubtitleConfig.MARGIN_BOTTOM_H
        x = (self.width  - tw) // 2
        y = self.height - th - margin

        pad = SubtitleConfig.BOX_PADDING
        bg_rect = [x - pad, y - pad, x + tw + pad, y + th + pad]
        draw.rounded_rectangle(bg_rect, radius=SubtitleConfig.BOX_BORDER_RADIUS, fill=(0, 0, 0, 160))

        outline_color = (212, 175, 55, 180) if is_ayah else (255, 255, 255, 50)
        draw.rounded_rectangle(bg_rect, radius=SubtitleConfig.BOX_BORDER_RADIUS, outline=outline_color, width=2)

        so = SubtitleConfig.SHADOW_OFFSET
        draw.multiline_text((x + so, y + so), display, font=font, fill=(0, 0, 0, 200), align="center")

        color = (255, 215, 0, 255) if is_ayah else (*text_color, 255)
        draw.multiline_text((x, y), display, font=font, fill=color, align="center")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, "PNG")
        return output_path


class SegmentBuilder:
    """يبني مقاطع الفيديو الفردية بدقة متناهية"""

    def __init__(self, width: int = 1920, height: int = 1080):
        self.W = width
        self.H = height
        self._sub = SubtitleOverlay(width, height)

    def _scale_image_filter(self) -> str:
        return f"scale={self.W * 2}:{self.H * 2}:flags=lanczos,crop={self.W}:{self.H}"

    def _ken_burns(self, duration: float, zoom_speed: float = 0.0006) -> str:
        frames = int(duration * VideoConfig.FPS)
        max_z  = min(1.0 + zoom_speed * frames, 1.10)
        return (
            f"scale=8000:-1:flags=lanczos,"
            f"zoompan=z='min(zoom+{zoom_speed},{max_z})':d={frames}:"
            f"s={self.W}x{self.H}:fps={VideoConfig.FPS}"
        )

    def build_narrator_segment(
        self,
        image_path: str,
        audio_path: str,
        output_path: str,
        subtitle_text: Optional[str] = None,
        duration: Optional[float] = None,
        use_ken_burns: bool = True,
    ) -> str:
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"ملف صوتي مفقود: {audio_path}")

        dur = duration or _probe_duration(audio_path)
        img = image_path if Path(str(image_path)).exists() else str(Paths.ASSETS / "default_bg.png")

        vf = self._ken_burns(dur) if use_ken_burns else self._scale_image_filter()

        sub_filter = ""
        sub_inputs  = []
        if subtitle_text:
            sub_path = str(Paths.ASSEMBLY / f"sub_{hash(subtitle_text) % 99999:05d}.png")
            created  = self._sub.create(subtitle_text, sub_path, font_size=SubtitleConfig.FONT_SIZE_MEDIUM)
            if created:
                sub_inputs  = ["-i", created]
                sub_filter  = f";[v][1:v]overlay=0:0[vout]"
                final_map   = "[vout]"
            else:
                final_map = "[v]"
        else:
            final_map = "[v]"

        fc = f"[0:v]{vf}[v]{sub_filter}"

        cmd = (
            ["ffmpeg", "-y", "-loop", "1", "-i", img]
            + sub_inputs
            + ["-i", audio_path,
               "-filter_complex", fc,
               "-map", final_map,
               "-map", f"{1 + len(sub_inputs)//2}:a",
               "-af", "afade=t=in:st=0:d=0.2,afade=t=out:st=999:d=0.2", 
               "-c:v", VideoConfig.CODEC,
               "-profile:v", VideoConfig.PROFILE,
               "-crf", str(VideoConfig.CRF),
               "-preset", VideoConfig.PRESET,
               "-pix_fmt", VideoConfig.PIX_FMT,
               "-c:a", VideoConfig.AUDIO_CODEC,
               "-b:a", VideoConfig.AUDIO_BITRATE,
               "-ar", str(VideoConfig.AUDIO_RATE),
               "-t", str(dur + 0.4),
               "-shortest",
               output_path]
        )
        _run(cmd, f"مقطع راوي: {Path(output_path).name}")
        return output_path

    def build_ayah_segment(
        self,
        image_path: str,
        quran_audio: str,
        ayah_text: str,
        output_path: str,
        duration: Optional[float] = None,
    ) -> str:
        dur = duration or _probe_duration(quran_audio)
        img = image_path if Path(str(image_path)).exists() else str(Paths.ASSETS / "default_bg.png")

        sub_path = str(Paths.ASSEMBLY / f"ayah_{hash(ayah_text) % 99999:05d}.png")
        self._sub.create(
            ayah_text, sub_path,
            font_size=SubtitleConfig.FONT_SIZE_LARGE,
            is_ayah=True,
        )

        vf = self._ken_burns(dur, zoom_speed=0.0004)

        sub_inputs = []
        sub_filter = ""
        if Path(sub_path).exists():
            sub_inputs = ["-i", sub_path]
            fd_in  = 0.6
            fd_out = dur - 0.8
            sub_filter = (
                f";[1:v]fade=t=in:st=0:d={fd_in}:alpha=1,"
                f"fade=t=out:st={fd_out}:d=0.6:alpha=1[sub];"
                f"[v][sub]overlay=0:0[vout]"
            )
            final_map = "[vout]"
        else:
            final_map = "[v]"

        fc = f"[0:v]{vf}[v]{sub_filter}"

        cmd = (
            ["ffmpeg", "-y", "-loop", "1", "-i", img]
            + sub_inputs
            + ["-i", quran_audio,
               "-filter_complex", fc,
               "-map", final_map,
               "-map", f"{1 + len(sub_inputs)//2}:a",
               "-af", "afade=t=in:st=0:d=0.2,afade=t=out:st=999:d=0.2",
               "-c:v", VideoConfig.CODEC,
               "-profile:v", VideoConfig.PROFILE,
               "-crf", str(VideoConfig.CRF),
               "-preset", VideoConfig.PRESET,
               "-pix_fmt", VideoConfig.PIX_FMT,
               "-c:a", VideoConfig.AUDIO_CODEC,
               "-b:a", VideoConfig.AUDIO_BITRATE,
               "-ar", str(VideoConfig.AUDIO_RATE),
               "-t", str(dur + 0.4),
               "-shortest",
               output_path]
        )
        _run(cmd, f"مقطع تلاوة: {Path(output_path).name}")
        return output_path

    def concatenate(self, segments: list[str], output_path: str) -> str:
        """
        [ترقية هندسية]: دمج لحظي (Lossless Concat) للمقاطع.
        بما أن كل المقاطع تم إنشاؤها بنفس الإعدادات، نستخدم (-c copy) لدمجها 
        في ثانية واحدة وبدون فقدان 1% من الجودة.
        """
        valid = [s for s in segments if Path(s).exists()]
        if not valid:
            raise RuntimeError("لا توجد مقاطع صالحة")
        if len(valid) == 1:
            shutil.copy(valid[0], output_path)
            return output_path

        concat_list = Paths.ASSEMBLY / "concat_list.txt"
        concat_list.write_text(
            "\n".join(f"file '{os.path.abspath(s)}'" for s in valid)
        )

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy", # 👈 السحر هنا: دمج بدون رندرة!
            "-movflags", "+faststart",
            output_path,
        ]
        _run(cmd, f"دمج {len(valid)} مقاطع (Lossless)", timeout=900)
        return output_path


class VideoEngine:
    def __init__(self, vertical: bool = False):
        self.vertical = vertical
        self.W = VideoConfig.WIDTH_V if vertical else VideoConfig.WIDTH_H
        self.H = VideoConfig.HEIGHT_V if vertical else VideoConfig.HEIGHT_H
        self.builder = SegmentBuilder(self.W, self.H)
        Paths.ensure_all()

    def assemble_episode(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info(f"🎬 تجميع الحلقة {script.episode_number}: {script.surah_name}")
        seg_dir = Path(ep_dir) / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        Paths.ASSEMBLY.mkdir(parents=True, exist_ok=True)

        segments: list[str] = []

        if script.intro_scene.audio_path:
            intro_seg = str(seg_dir / "00_intro.mp4")
            self.builder.build_narrator_segment(
                image_path=script.intro_scene.image_path or "",
                audio_path=script.intro_scene.audio_path,
                subtitle_text=script.intro_scene.narrator_text,
                output_path=intro_seg,
            )
            if Path(intro_seg).exists(): segments.append(intro_seg)

        for i, ayah_scene in enumerate(script.ayah_scenes):
            sid = ayah_scene.scene_id

            if ayah_scene.intro_audio:
                intro_p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_intro.mp4")
                self.builder.build_narrator_segment(
                    image_path=ayah_scene.image_path or "",
                    audio_path=ayah_scene.intro_audio,
                    subtitle_text=ayah_scene.intro_text,
                    output_path=intro_p,
                )
                if Path(intro_p).exists(): segments.append(intro_p)

            if ayah_scene.quran_audio:
                quran_p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_quran.mp4")
                self.builder.build_ayah_segment(
                    image_path=ayah_scene.image_path or "",
                    quran_audio=ayah_scene.quran_audio,
                    ayah_text=ayah_scene.ayah.text,
                    output_path=quran_p,
                )
                if Path(quran_p).exists(): segments.append(quran_p)

            if ayah_scene.explain_audio:
                exp_p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_explain.mp4")
                self.builder.build_narrator_segment(
                    image_path=ayah_scene.image_path or "",
                    audio_path=ayah_scene.explain_audio,
                    subtitle_text=ayah_scene.explain_text,
                    output_path=exp_p,
                )
                if Path(exp_p).exists(): segments.append(exp_p)

        for j, mid in enumerate(script.mid_scenes):
            if mid.audio_path:
                mid_p = str(seg_dir / f"mid_{j:02d}.mp4")
                self.builder.build_narrator_segment(
                    image_path=mid.image_path or "",
                    audio_path=mid.audio_path,
                    subtitle_text=mid.narrator_text,
                    output_path=mid_p,
                )
                if Path(mid_p).exists(): segments.append(mid_p)

        if script.outro_scene.audio_path:
            outro_p = str(seg_dir / "99_outro.mp4")
            self.builder.build_narrator_segment(
                image_path=script.outro_scene.image_path or "",
                audio_path=script.outro_scene.audio_path,
                subtitle_text=script.outro_scene.narrator_text,
                output_path=outro_p,
            )
            if Path(outro_p).exists(): segments.append(outro_p)

        if not segments:
            raise RuntimeError("❌ لا توجد مقاطع لتجميعها")

        raw_output = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4")
        # الدمج اللحظي بدون إضافة الشعار هنا (سيتم إضافته في Gamification)
        self.builder.concatenate(segments, raw_output)

        size_mb = Path(raw_output).stat().st_size / 1024 / 1024
        dur_min = _probe_duration(raw_output) / 60
        logger.info(f"✅ تم تجهيز الفيديو الخام: {raw_output} ({size_mb:.1f} MB | {dur_min:.1f} دقيقة)")
        
        return raw_output
