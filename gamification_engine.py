"""
gamification_engine.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
═══════════════════════════════════════════════════════
محرك التلعيب البصري الذكي (Smart Gamification Engine)
• شريط تقدم ديناميكي ينمو مع وقت الفيديو (Dynamic Time-based Progress Bar)
• نصوص تشجيعية تظهر وتختفي بنعومة (Fade-in/out Animations)
• معالجة في مسار واحد (Single-Pass Rendering) للحفاظ على الجودة العالية
═══════════════════════════════════════════════════════
"""

from __future__ import annotations
import json
import logging
import random
import subprocess
import shutil
from pathlib import Path
from config import Paths, VideoConfig
from models import EpisodeScript

logger = logging.getLogger(__name__)

ENCOURAGEMENTS = [
    "أحسنت يا بطل! ⭐",
    "ما شاء الله! 🌟",
    "أنت نجم القرآن 🌟",
    "بارك الله فيك 💛",
    "رائع يا حبيبي 🎉",
]

def _run(cmd: list[str], timeout: int = 600) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error(f"❌ خطأ في محرك التلعيب:\n{r.stderr[-500:]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("⏱️ نفذ الوقت المخصص لعملية التلعيب.")
        return False

def _get_font() -> str:
    """جلب الخط الأميري أو أفضل خط متوفر للتلعيب"""
    primary = Paths.FONTS / "Amiri-Bold.ttf"
    if primary.exists():
        return str(primary)
    for p in ["/usr/share/fonts/truetype/arabic/Amiri-Bold.ttf"]:
        if Path(p).exists(): return p
    return ""

def _probe_duration(path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0

class GamificationEngine:
    def __init__(self):
        self.font = _get_font()
        if not self.font:
            logger.warning("⚠️ لم يتم العثور على خط عربي، قد تظهر نصوص التلعيب بشكل متقطع.")

    def _prepare_arabic_text(self, text: str) -> str:
        """تشكيل النص العربي ليتوافق مع FFmpeg"""
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            reshaped = arabic_reshaper.reshape(text)
            display = get_display(reshaped)
            # تنظيف الرموز التي تكسر أوامر FFmpeg
            return display.replace("'", "").replace(":", "\\:").replace(",", "")
        except ImportError:
            return text.replace("'", "").replace(":", "\\:")

    def apply_to_episode(self, video_path: str, script: EpisodeScript, output_path: str) -> str:
        """
        يطبق جميع تأثيرات التلعيب في رندرة واحدة (Single Pass)
        لضمان عدم فقدان جودة الفيديو الأساسي.
        """
        duration = _probe_duration(video_path)
        if duration <= 0:
            logger.error("❌ لم أتمكن من قراءة مدة الفيديو، سيتم تجاوز التلعيب.")
            shutil.copy(video_path, output_path)
            return output_path

        logger.info(f"🎮 بدء إضافة تأثيرات التلعيب (شريط التقدم الديناميكي)...")

        filters = []

        # 1. شريط التقدم الديناميكي (ينمو مع الزمن)
        # الخلفية الرمادية للشريط
        filters.append(f"drawbox=x=0:y=H-12:w=W:h=12:color=black@0.6:t=fill")
        # الشريط الذهبي الممتلئ بناءً على الوقت الحالي (t) مقسوماً على المدة الكلية (duration)
        filters.append(f"drawbox=x=0:y=H-12:w=W*(t/{duration}):h=12:color=#FFD700@0.9:t=fill")

        # 2. نص تشجيعي عشوائي يظهر في منتصف الفيديو (إذا كان هناك خط)
        if self.font:
            encouragement = random.choice(ENCOURAGEMENTS)
            safe_text = self._prepare_arabic_text(encouragement)
            
            # حساب وقت الظهور (في منتصف الحلقة تقريباً، لمدة 4 ثوانٍ)
            t_start = duration * 0.5
            t_end = t_start + 4.0
            
            # تأثير الظهور والاختفاء (Fade in & Fade out)
            alpha_logic = f"if(lt(t,{t_start+0.5}),(t-{t_start})/0.5,if(gt(t,{t_end-0.5}),({t_end}-t)/0.5,1))"
            
            text_filter = (
                f"drawtext=fontfile='{self.font}':text='{safe_text}':"
                f"fontcolor=yellow:fontsize=75:"
                f"x=(W-text_w)/2:y=H*0.15:" # في الثلث العلوي من الشاشة
                f"enable='between(t,{t_start},{t_end})':"
                f"alpha='{alpha_logic}':"
                f"shadowcolor=black@0.8:shadowx=4:shadowy=4"
            )
            filters.append(text_filter)

        # دمج كل الفلاتر
        vf_string = ",".join(filters)

        # أمر الرندرة مع الحفاظ على الجودة العالية جداً
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", vf_string,
            "-c:v", VideoConfig.CODEC,
            "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF), # نحافظ على الجودة السينمائية
            "-preset", "fast", # سريع لأننا نضيف فقط Overlay
            "-pix_fmt", VideoConfig.PIX_FMT,
            "-c:a", "copy", # لا نلمس الصوت أبداً للحفاظ على نقائه
            output_path
        ]

        if _run(cmd, timeout=900):
            logger.info("✅ تمت إضافة تأثيرات التلعيب بنجاح.")
            return output_path
        else:
            logger.warning("⚠️ فشل التلعيب، سيتم استخدام الفيديو الأصلي كإجراء احتياطي.")
            shutil.copy(video_path, output_path)
            return output_path
