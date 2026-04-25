"""
intro_outro_engine.py — VALUE / QEEMA v5.0
==============================================
محرك الهوية الموحّدة:
  - يبني انترو 5 ثواني ثابت (شعار + نص + جنجل قصير)
  - يبني أوترو 5 ثواني (شعار + "اشترك في القناة")
  - يحفظهم في assets/branding/ ويستخدمهم لكل الحلقات (لا يعيد البناء)
  - يدمجهم بالحلقة الرئيسية في النهاية
"""

import logging
import shutil
import subprocess as sp
from pathlib import Path
from typing import Optional

from config import BrandingConfig, Paths, VideoConfig

logger = logging.getLogger(__name__)


def _run(cmd, timeout=120) -> bool:
    try:
        r = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error(f"intro/outro ffmpeg: {r.stderr[-400:]}")
            return False
        return True
    except Exception as e:
        logger.error(f"intro/outro exception: {e}")
        return False


def _font_path() -> str:
    candidates = [
        Paths.FONTS / "Amiri-Bold.ttf",
        Paths.FONTS / "NotoSansArabic-Bold.ttf",
        Path("/usr/share/fonts/truetype/hosny-amiri/Amiri-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for c in candidates:
        if Path(c).exists():
            return str(c)
    return ""


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


class IntroOutroEngine:
    """يبني الانترو والأوترو مرة واحدة ويحفظهم."""

    def __init__(self, force_rebuild: bool = False):
        self.font = _font_path()
        self.force_rebuild = force_rebuild
        Paths.BRANDING.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────
    # INTRO — 5 ثواني
    # ─────────────────────────────────────────────────────────────
    def build_intro(self) -> str:
        out = Paths.INTRO_VIDEO
        if out.exists() and not self.force_rebuild:
            logger.info(f"♻️ Intro موجود مسبقًا: {out.name}")
            return str(out)

        logo = Paths.LOGO_PRIMARY
        if not logo.exists():
            logger.error(f"❌ Logo missing: {logo}")
            return ""

        W, H = VideoConfig.RESOLUTION_WIDTH, VideoConfig.RESOLUTION_HEIGHT
        fps = VideoConfig.FPS
        duration = BrandingConfig.INTRO_DURATION
        bg = BrandingConfig.BG_COLOR
        primary = BrandingConfig.PRIMARY_COLOR
        accent = BrandingConfig.ACCENT_COLOR

        ar_name = BrandingConfig.CHANNEL_NAME_AR
        en_name = BrandingConfig.CHANNEL_NAME_EN
        tagline = BrandingConfig.CHANNEL_TAGLINE_AR

        # Filter graph:
        # - خلفية كريمية ثابتة
        # - الشعار: scale=480x480، fade in (0→0.7s)، يفضل ثابت، fade out (4.3→5s)
        # - النص العربي تحت الشعار: يظهر بعد 1s مع fade in
        # - النص الإنجليزي: يظهر بعد 1.3s
        # - tagline: يظهر بعد 2s

        font_arg = f"fontfile='{self.font}':" if self.font else ""

        logo_filter = (
            f"[1:v]scale=480:480:force_original_aspect_ratio=decrease,"
            f"format=rgba,"
            f"fade=t=in:st=0:d=0.6:alpha=1,"
            f"fade=t=out:st={duration-0.7}:d=0.7:alpha=1[logo]"
        )

        text_layers = ""
        if self.font:
            text_layers = (
                # اسم القناة بالعربي - يظهر بعد 1.0s
                f",drawtext={font_arg}text='{_esc(ar_name)}':"
                f"fontsize=140:fontcolor={primary}:"
                f"x=(w-text_w)/2:y=h*0.62:"
                f"alpha='if(lt(t,1),0,if(lt(t,1.5),(t-1)/0.5,if(lt(t,{duration-0.5}),1,({duration}-t)/0.5)))':"
                f"shadowcolor=black@0.3:shadowx=3:shadowy=4"
                # الاسم بالإنجليزي - يظهر بعد 1.3s
                f",drawtext={font_arg}text='{_esc(en_name)}':"
                f"fontsize=64:fontcolor={accent}:"
                f"x=(w-text_w)/2:y=h*0.74:"
                f"alpha='if(lt(t,1.3),0,if(lt(t,1.8),(t-1.3)/0.5,if(lt(t,{duration-0.5}),1,({duration}-t)/0.5)))'"
                # tagline - يظهر بعد 2s
                f",drawtext={font_arg}text='{_esc(tagline)}':"
                f"fontsize=42:fontcolor=white:"
                f"box=1:boxcolor={primary}@0.85:boxborderw=18:"
                f"x=(w-text_w)/2:y=h*0.84:"
                f"alpha='if(lt(t,2),0,if(lt(t,2.5),(t-2)/0.5,if(lt(t,{duration-0.5}),1,({duration}-t)/0.5)))'"
            )

        full_filter = (
            f"color=c={bg}:s={W}x{H}:d={duration}:r={fps}[bg];"
            f"{logo_filter};"
            f"[bg][logo]overlay=(W-w)/2:H*0.18:enable='between(t,0,{duration})'"
            f"{text_layers}[v]"
        )

        # هل يوجد jingle؟
        jingle = Paths.JINGLE
        if jingle.exists():
            cmd = [
                "ffmpeg", "-y",
                "-i", str(logo),                                           # input 0 = logo (placeholder)
                "-loop", "1", "-i", str(logo),                            # input 1 = logo loop
                "-i", str(jingle),                                         # input 2 = jingle
                "-filter_complex", full_filter,
                "-map", "[v]", "-map", "2:a",
                "-t", f"{duration}",
                "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
                "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
                "-pix_fmt", VideoConfig.PIX_FMT, "-r", str(fps),
                "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
                "-shortest",
                str(out),
            ]
        else:
            # لو مفيش jingle، نضيف صمت بالطول
            logger.info("ℹ️ No jingle.mp3 — generating silent intro")
            cmd = [
                "ffmpeg", "-y",
                "-i", str(logo),
                "-loop", "1", "-i", str(logo),
                "-f", "lavfi", "-t", f"{duration}", "-i", "anullsrc=r=44100:cl=stereo",
                "-filter_complex", full_filter,
                "-map", "[v]", "-map", "2:a",
                "-t", f"{duration}",
                "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
                "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
                "-pix_fmt", VideoConfig.PIX_FMT, "-r", str(fps),
                "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
                "-shortest",
                str(out),
            ]

        ok = _run(cmd, timeout=180)
        if ok and out.exists():
            logger.info(f"✅ Intro built: {out}")
            return str(out)
        logger.error("❌ Intro build failed")
        return ""

    # ─────────────────────────────────────────────────────────────
    # OUTRO — 5 ثواني
    # ─────────────────────────────────────────────────────────────
    def build_outro(self) -> str:
        out = Paths.OUTRO_VIDEO
        if out.exists() and not self.force_rebuild:
            logger.info(f"♻️ Outro موجود مسبقًا: {out.name}")
            return str(out)

        logo = Paths.LOGO_PRIMARY
        if not logo.exists():
            logger.error(f"❌ Logo missing for outro: {logo}")
            return ""

        W, H = VideoConfig.RESOLUTION_WIDTH, VideoConfig.RESOLUTION_HEIGHT
        fps = VideoConfig.FPS
        duration = BrandingConfig.OUTRO_DURATION
        bg = BrandingConfig.BG_COLOR
        primary = BrandingConfig.PRIMARY_COLOR
        secondary = BrandingConfig.SECONDARY_COLOR

        font_arg = f"fontfile='{self.font}':" if self.font else ""

        logo_filter = (
            f"[1:v]scale=400:400:force_original_aspect_ratio=decrease,"
            f"format=rgba,"
            f"fade=t=in:st=0:d=0.5:alpha=1,"
            f"fade=t=out:st={duration-0.5}:d=0.5:alpha=1[logo]"
        )

        text_layers = ""
        if self.font:
            text_layers = (
                # "اشترك في القناة" - النص الرئيسي
                f",drawtext={font_arg}text='{_esc(BrandingConfig.SUBSCRIBE_TEXT)}':"
                f"fontsize=110:fontcolor={primary}:"
                f"box=1:boxcolor=white@0.9:boxborderw=24:"
                f"x=(w-text_w)/2:y=h*0.68:"
                f"alpha='if(lt(t,0.6),0,if(lt(t,1.2),(t-0.6)/0.6,if(lt(t,{duration-0.5}),1,({duration}-t)/0.5)))':"
                f"shadowcolor=black@0.4:shadowx=4:shadowy=5"
                # "VALUE • قِيمَة" أسفل
                f",drawtext={font_arg}text='{_esc(BrandingConfig.CHANNEL_NAME_AR + chr(32) + chr(124) + chr(32) + BrandingConfig.CHANNEL_NAME_EN)}':"
                f"fontsize=50:fontcolor={secondary}:"
                f"x=(w-text_w)/2:y=h*0.86:"
                f"alpha='if(lt(t,1.2),0,if(lt(t,1.7),(t-1.2)/0.5,if(lt(t,{duration-0.5}),1,({duration}-t)/0.5)))'"
            )

        full_filter = (
            f"color=c={bg}:s={W}x{H}:d={duration}:r={fps}[bg];"
            f"{logo_filter};"
            f"[bg][logo]overlay=(W-w)/2:H*0.12:enable='between(t,0,{duration})'"
            f"{text_layers}[v]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(logo),
            "-loop", "1", "-i", str(logo),
            "-f", "lavfi", "-t", f"{duration}", "-i", "anullsrc=r=44100:cl=stereo",
            "-filter_complex", full_filter,
            "-map", "[v]", "-map", "2:a",
            "-t", f"{duration}",
            "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT, "-r", str(fps),
            "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
            "-shortest",
            str(out),
        ]

        ok = _run(cmd, timeout=120)
        if ok and out.exists():
            logger.info(f"✅ Outro built: {out}")
            return str(out)
        logger.error("❌ Outro build failed")
        return ""

    # ─────────────────────────────────────────────────────────────
    # ضم intro + main + outro
    # ─────────────────────────────────────────────────────────────
    def wrap_episode(self, main_video: str, output_path: str) -> str:
        """يربط: intro → main → outro."""
        intro = self.build_intro()
        outro = self.build_outro()

        if not intro or not outro:
            logger.warning("⚠️ Intro/Outro غير متاحة، استخدام الفيديو الأصلي")
            shutil.copy(main_video, output_path)
            return output_path

        # نستخدم concat filter (بدل demuxer) لضمان توحيد الخصائص
        # لو الـ resolution/codec مختلف، demuxer هيفشل
        list_file = Path(output_path).parent / "wrap_list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            f.write(f"file '{Path(intro).absolute()}'\n")
            f.write(f"file '{Path(main_video).absolute()}'\n")
            f.write(f"file '{Path(outro).absolute()}'\n")

        # نعيد الترميز عشان نضمن التوافق
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT, "-r", str(VideoConfig.FPS),
            "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
            output_path,
        ]
        ok = _run(cmd, timeout=600)

        try: list_file.unlink()
        except: pass

        if ok and Path(output_path).exists():
            logger.info(f"✅ Wrapped final episode: {output_path}")
            return output_path

        logger.error("❌ Wrap failed, copying main as fallback")
        shutil.copy(main_video, output_path)
        return output_path
