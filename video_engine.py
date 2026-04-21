"""
video_engine.py — VALUE / QEEMA v2
═══════════════════════════════════════════════════════
محرك الفيديو الاحترافي
• جودة أعلى من الفيديو المرجعي (H.264 High CRF16)
• سبتايتل عربي احترافي مع ظل وخلفية
• Ken Burns effect ناعم
• انتقالات xfade سلسة
• دعم الصيغتين: Landscape 1920×1080 + Vertical 1080×1920
═══════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from config import Paths, SubtitleConfig, VideoConfig
from models import AyahScene, EpisodeScript, NarratorScene

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# FFmpeg Helper
# ══════════════════════════════════════════
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
    """يجلب أفضل خط عربي متاح"""
    font_dir = Paths.FONTS
    for ext in ["*.ttf", "*.otf"]:
        fonts = list(font_dir.glob(ext))
        if fonts:
            return str(fonts[0])
    system_fonts = [
        "/usr/share/fonts/truetype/arabic/Amiri-Bold.ttf",
        "/usr/share/fonts/truetype/arabic/Amiri-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    ]
    for f in system_fonts:
        if Path(f).exists():
            return f
    return ""


# ══════════════════════════════════════════
# Subtitle Image Generator (PIL — أفضل دعم للعربية)
# ══════════════════════════════════════════
class SubtitleOverlay:
    """
    يولد صور overlay للسبتايتل باستخدام PIL
    أفضل بكثير من FFmpeg drawtext للعربية
    """

    def __init__(self, width: int, height: int):
        self.width  = width
        self.height = height
        self._cache: dict[str, str] = {}

    def create(
        self,
        text: str,
        output_path: str,
        font_size: int = 60,
        text_color: tuple = (255, 255, 255),
        is_ayah: bool = False,
    ) -> str:
        """يولد صورة PNG شفافة تحتوي النص العربي"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            import arabic_reshaper
            from bidi.algorithm import get_display
        except ImportError:
            logger.warning("⚠️ PIL/arabic_reshaper غير مثبت — سيتم تجاوز السبتايتل")
            return ""

        # إعادة تشكيل النص العربي
        try:
            reshaped = arabic_reshaper.reshape(text)
            display  = get_display(reshaped)
        except Exception:
            display = text

        img  = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # تحميل الخط
        font_path = _get_font()
        try:
            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        # حساب حجم النص
        bbox = draw.textbbox((0, 0), display, font=font)
        tw   = bbox[2] - bbox[0]
        th   = bbox[3] - bbox[1]

        # موضع النص (أسفل الشاشة)
        margin = SubtitleConfig.MARGIN_BOTTOM_H
        x = (self.width  - tw) // 2
        y = self.height - th - margin

        # خلفية شبه شفافة
        pad = SubtitleConfig.BOX_PADDING
        bg_rect = [x - pad, y - pad, x + tw + pad, y + th + pad]
        draw.rounded_rectangle(bg_rect, radius=12, fill=(0, 0, 0, 145))

        # إطار
        draw.rounded_rectangle(bg_rect, radius=12, outline=(255, 215, 0, 80), width=2)

        # ظل
        so = SubtitleConfig.SHADOW_OFFSET
        draw.text((x + so, y + so), display, font=font, fill=(0, 0, 0, 220))

        # نص رئيسي
        color = (255, 215, 0, 255) if is_ayah else (*text_color, 255)
        draw.text((x, y), display, font=font, fill=color)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, "PNG")
        return output_path

    def batch_create(
        self,
        texts: list[tuple[str, bool]],
        output_dir: str,
    ) -> list[str]:
        """يولد مجموعة overlay في دفعة واحدة"""
        paths = []
        for i, (text, is_ayah) in enumerate(texts):
            out = str(Path(output_dir) / f"sub_{i:04d}.png")
            fs  = SubtitleConfig.FONT_SIZE_LARGE if is_ayah else SubtitleConfig.FONT_SIZE_MEDIUM
            p   = self.create(text, out, font_size=fs, is_ayah=is_ayah)
            paths.append(p)
        return paths


# ══════════════════════════════════════════
# Segment Builder
# ══════════════════════════════════════════
class SegmentBuilder:
    """يبني مقاطع الفيديو الفردية"""

    LOGO_PATH = str(Paths.OVERLAYS / "channel_logo.png")

    def __init__(self, width: int = 1920, height: int = 1080):
        self.W = width
        self.H = height
        self._sub = SubtitleOverlay(width, height)

    def _scale_image_filter(self) -> str:
        """فلتر تحجيم الصورة مع crop ذكي"""
        return (
            f"scale={self.W * 2}:{self.H * 2}:flags=lanczos,"
            f"crop={self.W}:{self.H}"
        )

    def _ken_burns(self, duration: float, zoom_speed: float = 0.0008) -> str:
        """Ken Burns effect ناعم جداً"""
        frames = int(duration * VideoConfig.FPS)
        max_z  = min(1.0 + zoom_speed * frames, 1.08)
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
        """يبني مقطع مشهد راوي"""
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"ملف صوتي مفقود: {audio_path}")

        dur = duration or _probe_duration(audio_path)
        img = image_path if Path(str(image_path)).exists() else str(Paths.OVERLAYS / "default_bg.png")

        vf = self._ken_burns(dur) if use_ken_burns else self._scale_image_filter()

        # إضافة السبتايتل كـ overlay
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
            ["ffmpeg", "-y",
             "-loop", "1", "-i", img]
            + sub_inputs
            + ["-i", audio_path,
               "-filter_complex", fc,
               "-map", final_map,
               "-map", f"{1 + len(sub_inputs)//2}:a",
               "-c:v", VideoConfig.CODEC,
               "-profile:v", VideoConfig.PROFILE,
               "-crf", str(VideoConfig.CRF),
               "-preset", VideoConfig.PRESET,
               "-pix_fmt", VideoConfig.PIX_FMT,
               "-c:a", VideoConfig.AUDIO_CODEC,
               "-b:a", VideoConfig.AUDIO_BITRATE,
               "-ar", str(VideoConfig.AUDIO_RATE),
               "-t", str(dur + 0.3),
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
        """
        يبني مقطع التلاوة القرآنية
        النص القرآني يظهر كخط ذهبي كبير
        """
        dur = duration or _probe_duration(quran_audio)
        img = image_path if Path(str(image_path)).exists() else str(Paths.OVERLAYS / "quran_bg.png")

        # overlay الآية
        sub_path = str(Paths.ASSEMBLY / f"ayah_{hash(ayah_text) % 99999:05d}.png")
        self._sub.create(
            ayah_text, sub_path,
            font_size=SubtitleConfig.FONT_SIZE_LARGE,
            is_ayah=True,
        )

        # Ken Burns أبطأ للتلاوة (تأمل)
        vf = self._ken_burns(dur, zoom_speed=0.0003)

        sub_inputs = []
        sub_filter = ""
        if Path(sub_path).exists():
            sub_inputs = ["-i", sub_path]
            # fade in/out للنص
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
            ["ffmpeg", "-y",
             "-loop", "1", "-i", img]
            + sub_inputs
            + ["-i", quran_audio,
               "-filter_complex", fc,
               "-map", final_map,
               "-map", f"{1 + len(sub_inputs)//2}:a",
               "-c:v", VideoConfig.CODEC,
               "-profile:v", VideoConfig.PROFILE,
               "-crf", str(VideoConfig.CRF),
               "-preset", VideoConfig.PRESET,
               "-pix_fmt", VideoConfig.PIX_FMT,
               "-c:a", VideoConfig.AUDIO_CODEC,
               "-b:a", VideoConfig.AUDIO_BITRATE,
               "-ar", str(VideoConfig.AUDIO_RATE),
               "-t", str(dur + 0.3),
               "-shortest",
               output_path]
        )
        _run(cmd, f"مقطع تلاوة: {Path(output_path).name}")
        return output_path

    def add_logo_overlay(self, video_path: str, output_path: str) -> str:
        """يضيف شعار القناة في الأعلى"""
        logo = self.LOGO_PATH
        if not Path(logo).exists():
            shutil.copy(video_path, output_path)
            return output_path

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", logo,
            "-filter_complex",
            f"[1:v]scale=110:110[logo];[0:v][logo]overlay=W/2-55:15[vout]",
            "-map", "[vout]",
            "-map", "0:a",
            "-c:v", VideoConfig.CODEC,
            "-crf", str(VideoConfig.CRF + 2),
            "-c:a", "copy",
            output_path,
        ]
        success = _run(cmd, "إضافة الشعار")
        return output_path if success else video_path

    def concatenate(self, segments: list[str], output_path: str) -> str:
        """يدمج المقاطع بانتقالات xfade"""
        valid = [s for s in segments if Path(s).exists()]
        if not valid:
            raise RuntimeError("لا توجد مقاطع صالحة")
        if len(valid) == 1:
            shutil.copy(valid[0], output_path)
            return output_path

        # استخدام concat filter مع fade بسيط
        concat_list = Paths.ASSEMBLY / "concat_list.txt"
        concat_list.write_text(
            "\n".join(f"file '{os.path.abspath(s)}'" for s in valid)
        )

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c:v", VideoConfig.CODEC,
            "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF),
            "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT,
            "-c:a", VideoConfig.AUDIO_CODEC,
            "-b:a", VideoConfig.AUDIO_BITRATE,
            "-ar", str(VideoConfig.AUDIO_RATE),
            "-movflags", "+faststart",   # مهم لـ YouTube streaming
            output_path,
        ]
        _run(cmd, f"دمج {len(valid)} مقطع", timeout=900)
        return output_path


# ══════════════════════════════════════════
# Main Video Engine
# ══════════════════════════════════════════
class VideoEngine:
    """المحرك الرئيسي — يجمع الحلقة كاملة"""

    def __init__(self, vertical: bool = False):
        self.vertical = vertical
        self.W = VideoConfig.WIDTH_V if vertical else VideoConfig.WIDTH_H
        self.H = VideoConfig.HEIGHT_V if vertical else VideoConfig.HEIGHT_H
        self.builder = SegmentBuilder(self.W, self.H)
        Paths.ensure_all()

    def assemble_episode(
        self,
        script: EpisodeScript,
        ep_dir: str,
    ) -> str:
        """
        يجمع الحلقة كاملة من السكريبت المُعالَج
        (بعد اكتمال الصوت والصور)
        """
        logger.info(f"🎬 تجميع الحلقة {script.episode_number}: {script.surah_name}")
        seg_dir = Path(ep_dir) / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        Paths.ASSEMBLY.mkdir(parents=True, exist_ok=True)

        segments: list[str] = []

        # ── الافتتاح ──────────────────────────
        if script.intro_scene.audio_path:
            intro_seg = str(seg_dir / "00_intro.mp4")
            self.builder.build_narrator_segment(
                image_path=script.intro_scene.image_path or "",
                audio_path=script.intro_scene.audio_path,
                subtitle_text=script.intro_scene.narrator_text,
                output_path=intro_seg,
                use_ken_burns=True,
            )
            if Path(intro_seg).exists():
                segments.append(intro_seg)

        # ── مشاهد الآيات ─────────────────────
        for i, ayah_scene in enumerate(script.ayah_scenes):
            sid = ayah_scene.scene_id

            # مقدمة الآية
            if ayah_scene.intro_audio:
                intro_p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_intro.mp4")
                self.builder.build_narrator_segment(
                    image_path=ayah_scene.image_path or "",
                    audio_path=ayah_scene.intro_audio,
                    subtitle_text=ayah_scene.intro_text,
                    output_path=intro_p,
                )
                if Path(intro_p).exists():
                    segments.append(intro_p)

            # التلاوة + النص على الشاشة
            if ayah_scene.quran_audio:
                quran_p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_quran.mp4")
                self.builder.build_ayah_segment(
                    image_path=ayah_scene.image_path or "",
                    quran_audio=ayah_scene.quran_audio,
                    ayah_text=ayah_scene.ayah.text,
                    output_path=quran_p,
                )
                if Path(quran_p).exists():
                    segments.append(quran_p)

            # الشرح بعد الآية
            if ayah_scene.explain_audio:
                exp_p = str(seg_dir / f"{i+1:02d}_ayah_{sid:03d}_explain.mp4")
                self.builder.build_narrator_segment(
                    image_path=ayah_scene.image_path or "",
                    audio_path=ayah_scene.explain_audio,
                    subtitle_text=ayah_scene.explain_text,
                    output_path=exp_p,
                )
                if Path(exp_p).exists():
                    segments.append(exp_p)

        # ── المشاهد الوسطى ─────────────────────
        for j, mid in enumerate(script.mid_scenes):
            if mid.audio_path:
                mid_p = str(seg_dir / f"mid_{j:02d}.mp4")
                self.builder.build_narrator_segment(
                    image_path=mid.image_path or "",
                    audio_path=mid.audio_path,
                    subtitle_text=mid.narrator_text,
                    output_path=mid_p,
                )
                if Path(mid_p).exists():
                    segments.append(mid_p)

        # ── الخاتمة ───────────────────────────
        if script.outro_scene.audio_path:
            outro_p = str(seg_dir / "99_outro.mp4")
            self.builder.build_narrator_segment(
                image_path=script.outro_scene.image_path or "",
                audio_path=script.outro_scene.audio_path,
                subtitle_text=script.outro_scene.narrator_text,
                output_path=outro_p,
                use_ken_burns=True,
            )
            if Path(outro_p).exists():
                segments.append(outro_p)

        if not segments:
            raise RuntimeError("❌ لا توجد مقاطع لتجميعها")

        # ── دمج كل المقاطع ─────────────────────
        raw_output = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4")
        self.builder.concatenate(segments, raw_output)

        # ── إضافة الشعار ──────────────────────
        final_output = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_final.mp4")
        self.builder.add_logo_overlay(raw_output, final_output)

        # مسح الملف المؤقت
        try:
            Path(raw_output).unlink(missing_ok=True)
        except Exception:
            pass

        size_mb = Path(final_output).stat().st_size / 1024 / 1024
        dur_min = _probe_duration(final_output) / 60
        logger.info(
            f"🎉 الفيديو النهائي: {final_output} "
            f"({size_mb:.1f} MB | {dur_min:.1f} دقيقة)"
        )
        return final_output
