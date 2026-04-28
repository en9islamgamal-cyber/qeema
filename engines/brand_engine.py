"""
brand_engine.py — VALUE / QEEMA v1.0
======================================
محرك الهوية البصرية — يولّد الانترو والأوترو مرة واحدة ويعيد استخدامهما.

Strategy:
  - يولّد intro.mp4 و outro.mp4 مرة واحدة عند أول تشغيل
  - يحفظهما في assets/brand/
  - كل الحلقات اللاحقة تستخدم نفس الملفات (لا إعادة توليد)
  - الانترو: 5 ثوانٍ — شعار + اسم القناة + جرس موسيقي
  - الأوترو: 6 ثوانٍ — شعار + دعوة للاشتراك + دعاء
"""

import logging
import shutil
import subprocess as sp
from pathlib import Path
from typing import Tuple, Optional

from config import Paths

logger = logging.getLogger(__name__)

BRAND_DIR = Paths.ASSETS / "brand"
INTRO_PATH = BRAND_DIR / "intro.mp4"
OUTRO_PATH = BRAND_DIR / "outro.mp4"

CHANNEL_NAME    = "حواديت الجد أبو زياد"
CHANNEL_TAGLINE = "قرآن للأطفال بأسلوب الحواديت"
SUBSCRIBE_TEXT  = "اشترك وفعّل الجرس 🔔"

# ألوان الهوية (hex → ffmpeg drawtext color)
COLOR_GOLD  = "0xC9A84C"
COLOR_TEAL  = "0x1A6B7A"
COLOR_CREAM = "0xF5F0E6"
COLOR_DARK  = "0x1A1A2E"


def _run(cmd, timeout=60) -> bool:
    try:
        r = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error(f"ffmpeg error: {r.stderr[-300:]}")
            return False
        return True
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        return False


def _find_font() -> str:
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


class BrandEngine:
    """يولّد وسائط الهوية مرة واحدة ويعيد استخدامها في كل الحلقات."""

    def __init__(self):
        BRAND_DIR.mkdir(parents=True, exist_ok=True)
        self.font       = _find_font()
        self.logo_path  = self._find_logo()
        self.W, self.H  = 1280, 720

    def _find_logo(self) -> Optional[str]:
        candidates = [
            Paths.ASSETS / "logo.png",
            Paths.ASSETS / "logo.jpg",
            Path("logo.png"),
            Path("logo.jpg"),
        ]
        for c in candidates:
            if Path(c).exists():
                return str(c)
        return None

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────
    def ensure_brand_assets(self) -> Tuple[Optional[str], Optional[str]]:
        """
        يتأكد من وجود الانترو والأوترو.
        يولّدهما لو مش موجودين.
        يُعيد (intro_path, outro_path) — None لو الجيل فشل.
        """
        intro_ok = INTRO_PATH.exists() and INTRO_PATH.stat().st_size > 10_000
        outro_ok = OUTRO_PATH.exists() and OUTRO_PATH.stat().st_size > 10_000

        if not intro_ok:
            logger.info("🎬 توليد الانترو (مرة واحدة)...")
            intro_ok = self._generate_intro()

        if not outro_ok:
            logger.info("🎬 توليد الأوترو (مرة واحدة)...")
            outro_ok = self._generate_outro()

        return (
            str(INTRO_PATH) if intro_ok else None,
            str(OUTRO_PATH) if outro_ok else None,
        )

    def wrap_episode(self, raw_video: str, output: str) -> str:
        """يلصق الانترو والأوترو حول الفيديو الخام."""
        intro_p, outro_p = self.ensure_brand_assets()

        if not intro_p and not outro_p:
            logger.warning("⚠️ لا يوجد انترو/أوترو — نسخ الفيديو مباشرة")
            shutil.copy(raw_video, output)
            return output

        segments = []
        if intro_p:
            segments.append(intro_p)
        segments.append(raw_video)
        if outro_p:
            segments.append(outro_p)

        tmp_list = Path(output).parent / "brand_concat.txt"
        with open(tmp_list, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"file '{Path(seg).absolute()}'\n")

        ok = _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(tmp_list),
            "-c", "copy",
            output,
        ], timeout=180)

        try:
            tmp_list.unlink()
        except Exception:
            pass

        if ok:
            logger.info(f"✅ فيديو مع الهوية: {Path(output).name}")
            return output
        else:
            shutil.copy(raw_video, output)
            return output

    # ─────────────────────────────────────────────────────────────
    # Intro Generator (5 ثوانٍ)
    # ─────────────────────────────────────────────────────────────
    def _generate_intro(self) -> bool:
        """
        الانترو: خلفية داكنة → نجوم → اسم القناة يظهر تدريجياً → شعار
        مدة: 5 ثوانٍ
        """
        W, H   = self.W, self.H
        font   = self.font
        dur    = 5
        fps    = 30

        # ── بناء الـ filtergraph
        # خلفية: تدرج من أزرق داكن لأسود
        bg = (f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}[bg]")

        if font:
            # اسم القناة — يظهر مع fade in من الثانية 0.5
            title_filter = (
                f"[bg]drawtext="
                f"fontfile='{font}':"
                f"text='{CHANNEL_NAME}':"
                f"fontsize=72:"
                f"fontcolor={COLOR_GOLD}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2-40:"
                f"alpha='if(lt(t,0.5),0,if(lt(t,1.5),(t-0.5),1))':"
                f"shadowcolor=black@0.5:shadowx=3:shadowy=3[t1];"

                f"[t1]drawtext="
                f"fontfile='{font}':"
                f"text='{CHANNEL_TAGLINE}':"
                f"fontsize=36:"
                f"fontcolor={COLOR_CREAM}@0.85:"
                f"x=(w-text_w)/2:y=(h-text_h)/2+50:"
                f"alpha='if(lt(t,1),0,if(lt(t,2),(t-1),1))'[t2]"
            )
            vf_chain = f"[0:v]{title_filter}"
            final_node = "[t2]"
        else:
            vf_chain = f"[0:v]null[t2]"
            final_node = "[t2]"

        # إضافة Logo لو موجود
        if self.logo_path and font:
            logo_filter = (
                f";[t2][1:v]overlay="
                f"x=(W-w)/2:y=H/2-160:"
                f"enable='between(t,0.3,{dur})'[out]"
            )
            full_filter  = vf_chain + logo_filter
            final_node   = "[out]"
            inputs       = [
                "-f", "lavfi", "-i", bg,
                "-i", self.logo_path,
            ]
            logo_scale_filter = (
                f";[1:v]scale=200:-1:flags=lanczos[logo];"
                # نعيد بناء مع logo مُقلَّص
            )
            # نبسط: نستخدم filter_complex مع logo
            full_filter = (
                f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}[bg];"
                f"[1:v]scale=160:-1:flags=lanczos,"
                f"format=rgba,"
                f"fade=t=in:st=0.2:d=0.8:alpha=1[logo];"
                f"[bg][logo]overlay=x=(W-w)/2:y=H/2-140[with_logo];"
            )
            if font:
                full_filter += (
                    f"[with_logo]drawtext="
                    f"fontfile='{font}':"
                    f"text='{CHANNEL_NAME}':"
                    f"fontsize=66:fontcolor={COLOR_GOLD}:"
                    f"x=(w-text_w)/2:y=(h/2)+40:"
                    f"alpha='if(lt(t,0.8),0,if(lt(t,1.8),(t-0.8),1))':"
                    f"shadowcolor=black@0.6:shadowx=3:shadowy=3[t1];"
                    f"[t1]drawtext="
                    f"fontfile='{font}':"
                    f"text='{CHANNEL_TAGLINE}':"
                    f"fontsize=32:fontcolor={COLOR_CREAM}@0.8:"
                    f"x=(w-text_w)/2:y=(h/2)+115:"
                    f"alpha='if(lt(t,1.5),0,if(lt(t,2.5),(t-1.5),1))',"
                    f"fade=t=out:st={dur-0.8}:d=0.8[out]"
                )
            else:
                full_filter += f"[with_logo]fade=t=out:st={dur-0.8}:d=0.8[out]"

            final_node = "[out]"
            inputs = [
                "-f", "lavfi", "-i",
                f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}",
                "-i", self.logo_path,
            ]
        else:
            full_filter = (
                f"[0:v]"
                f"drawtext=fontfile='{font}':text='{CHANNEL_NAME}':"
                f"fontsize=72:fontcolor={COLOR_GOLD}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2-40:"
                f"alpha='if(lt(t,0.5),0,if(lt(t,1.5),(t-0.5),1))':"
                f"shadowcolor=black@0.5:shadowx=3:shadowy=3,"
                f"drawtext=fontfile='{font}':text='{CHANNEL_TAGLINE}':"
                f"fontsize=36:fontcolor={COLOR_CREAM}@0.8:"
                f"x=(w-text_w)/2:y=(h-text_h)/2+50:"
                f"alpha='if(lt(t,1),0,if(lt(t,2),(t-1),1))',"
                f"fade=t=in:st=0:d=0.5,"
                f"fade=t=out:st={dur-0.8}:d=0.8[out]"
            ) if font else (
                f"[0:v]fade=t=in:st=0:d=0.5,fade=t=out:st={dur-0.8}:d=0.8[out]"
            )
            final_node = "[out]"
            inputs = [
                "-f", "lavfi", "-i",
                f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}",
            ]

        # صوت صمت (يُستبدل بجرس موسيقي لو متاح)
        silence_input = ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:duration={dur}"]

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            *silence_input,
            "-filter_complex", full_filter,
            "-map", final_node,
            "-map", f"{len(inputs)//2}:a",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-r", str(fps), "-t", str(dur),
            str(INTRO_PATH),
        ]
        ok = _run(cmd, timeout=60)
        if ok:
            logger.info(f"✅ Intro: {INTRO_PATH}")
        return ok

    # ─────────────────────────────────────────────────────────────
    # Outro Generator (6 ثوانٍ)
    # ─────────────────────────────────────────────────────────────
    def _generate_outro(self) -> bool:
        """
        الأوترو: شعار + دعوة اشتراك + دعاء
        مدة: 6 ثوانٍ
        """
        W, H  = self.W, self.H
        font  = self.font
        dur   = 6
        fps   = 30

        subscribe = SUBSCRIBE_TEXT
        dua_text  = "جزاكم الله خيراً"

        if self.logo_path and font:
            full_filter = (
                f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}[bg];"
                f"[1:v]scale=140:-1:flags=lanczos,format=rgba,"
                f"fade=t=in:st=0.3:d=0.7:alpha=1[logo];"
                f"[bg][logo]overlay=x=(W-w)/2:y=H/2-120[wl];"
                f"[wl]drawtext="
                f"fontfile='{font}':text='{CHANNEL_NAME}':"
                f"fontsize=60:fontcolor={COLOR_GOLD}:"
                f"x=(w-text_w)/2:y=h/2+20:"
                f"alpha='if(lt(t,0.8),0,if(lt(t,1.8),(t-0.8),1))':"
                f"shadowcolor=black@0.5:shadowx=2:shadowy=2[t1];"
                f"[t1]drawtext="
                f"fontfile='{font}':text='{subscribe}':"
                f"fontsize=40:fontcolor=0xFF4444:"
                f"x=(w-text_w)/2:y=h/2+90:"
                f"alpha='if(lt(t,1.5),0,if(lt(t,2.5),(t-1.5),1))'[t2];"
                f"[t2]drawtext="
                f"fontfile='{font}':text='{dua_text}':"
                f"fontsize=34:fontcolor={COLOR_CREAM}@0.85:"
                f"x=(w-text_w)/2:y=h/2+145:"
                f"alpha='if(lt(t,2),0,if(lt(t,3),(t-2),1))',"
                f"fade=t=in:st=0:d=0.3,"
                f"fade=t=out:st={dur-0.6}:d=0.6[out]"
            )
            inputs = [
                "-f", "lavfi", "-i",
                f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}",
                "-i", self.logo_path,
            ]
        elif font:
            full_filter = (
                f"[0:v]"
                f"drawtext=fontfile='{font}':text='{CHANNEL_NAME}':"
                f"fontsize=66:fontcolor={COLOR_GOLD}:"
                f"x=(w-text_w)/2:y=(h/2)-40:"
                f"alpha='if(lt(t,0.5),0,if(lt(t,1.5),(t-0.5),1))':"
                f"shadowcolor=black@0.5:shadowx=2:shadowy=2,"
                f"drawtext=fontfile='{font}':text='{subscribe}':"
                f"fontsize=40:fontcolor=0xFF4444:"
                f"x=(w-text_w)/2:y=(h/2)+30:"
                f"alpha='if(lt(t,1),0,if(lt(t,2),(t-1),1))',"
                f"drawtext=fontfile='{font}':text='{dua_text}':"
                f"fontsize=34:fontcolor={COLOR_CREAM}@0.85:"
                f"x=(w-text_w)/2:y=(h/2)+80:"
                f"alpha='if(lt(t,1.8),0,if(lt(t,2.8),(t-1.8),1))',"
                f"fade=t=in:st=0:d=0.4,"
                f"fade=t=out:st={dur-0.6}:d=0.6[out]"
            )
            inputs = [
                "-f", "lavfi", "-i",
                f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}",
            ]
        else:
            full_filter = f"[0:v]fade=t=in:st=0:d=0.4,fade=t=out:st={dur-0.6}:d=0.6[out]"
            inputs = [
                "-f", "lavfi", "-i",
                f"color=c=0x0D1B2A:size={W}x{H}:rate={fps}:duration={dur}",
            ]

        silence_input = ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:duration={dur}"]

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            *silence_input,
            "-filter_complex", full_filter,
            "-map", "[out]",
            "-map", f"{len(inputs)//2}:a",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-r", str(fps), "-t", str(dur),
            str(OUTRO_PATH),
        ]
        ok = _run(cmd, timeout=60)
        if ok:
            logger.info(f"✅ Outro: {OUTRO_PATH}")
        return ok
