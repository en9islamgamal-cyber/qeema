"""
gamification_engine.py — VALUE / QEEMA v2
تأثيرات التلعيب البصري: نجوم، تشجيع، تقدم
"""
from __future__ import annotations
import logging, subprocess, shutil
from pathlib import Path
from config import Paths, VideoConfig, SubtitleConfig
from models import EpisodeScript

logger = logging.getLogger(__name__)

ENCOURAGEMENTS = [
    "أحسنت يا بطل! ⭐",
    "ممتاز جداً! ⭐⭐",
    "ما شاء الله! ⭐⭐⭐",
    "مبروك! حفظت آية 🌟",
    "أنت نجم القرآن 🌟🌟",
    "بارك الله فيك 💛",
    "رائع يا حبيبي 🎉",
]

def _run(cmd, timeout=300):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode == 0

def _font():
    for f in Paths.FONTS.glob("*.ttf"):
        return str(f)
    for p in ["/usr/share/fonts/truetype/arabic/Amiri-Bold.ttf"]:
        if Path(p).exists(): return p
    return ""

class GamificationEngine:
    def add_progress_bar(self, video: str, output: str, ratio: float) -> str:
        bar_w = max(1, int(1920 * min(max(ratio, 0), 1)))
        vf = (
            f"drawbox=x=0:y=H-10:w=W:h=10:color=#333333@0.75:t=fill,"
            f"drawbox=x=0:y=H-10:w={bar_w}:h=10:color=#FFD700@0.9:t=fill"
        )
        cmd = ["ffmpeg","-y","-i",video,"-vf",vf,"-c:a","copy",output]
        return output if _run(cmd) else (shutil.copy(video,output) or video)

    def add_encouragement(self, video: str, output: str, text: str, t_start: float=0.5, dur: float=2.8) -> str:
        font = _font()
        if not font:
            shutil.copy(video, output); return output
        try:
            import arabic_reshaper; from bidi.algorithm import get_display
            text = get_display(arabic_reshaper.reshape(text))
        except: pass
        safe = text.replace("'","").replace(":","").replace(",","")
        t_out = t_start + dur - 0.5
        vf = (
            f"drawtext=fontfile='{font}':text='{safe}':"
            f"fontcolor=yellow@0.95:fontsize=56:"
            f"x=(W-text_w)/2:y=H*0.12:"
            f"enable='between(t,{t_start},{t_start+dur})':"
            f"shadowcolor=black:shadowx=3:shadowy=3"
        )
        cmd = ["ffmpeg","-y","-i",video,"-vf",vf,"-c:a","copy",output]
        return output if _run(cmd) else (shutil.copy(video,output) or video)

    def add_ayah_counter(self, video: str, output: str, cur: int, total: int, surah: str) -> str:
        font = _font()
        if not font:
            shutil.copy(video, output); return output
        try:
            import arabic_reshaper; from bidi.algorithm import get_display
            label = get_display(arabic_reshaper.reshape(f"{surah} • {cur}/{total}"))
        except:
            label = f"{surah} {cur}/{total}"
        safe = label.replace("'","").replace(":","\\:")
        vf = (
            f"drawbox=x=W-230:y=8:w=222:h=48:color=black@0.65:t=fill,"
            f"drawtext=fontfile='{font}':text='{safe}':"
            f"fontcolor=#FFD700:fontsize=22:x=W-220:y=18"
        )
        cmd = ["ffmpeg","-y","-i",video,"-vf",vf,"-c:a","copy",output]
        return output if _run(cmd) else (shutil.copy(video,output) or video)

    def apply_to_episode(self, video_path: str, script: EpisodeScript, output_path: str) -> str:
        """يطبق جميع تأثيرات التلعيب على الفيديو النهائي"""
        # إضافة شريط التقدم فقط في هذه المرحلة (انتهت الحلقة = 100%)
        temp = video_path + "_gam_tmp.mp4"
        result = self.add_progress_bar(video_path, temp, 1.0)
        if Path(temp).exists():
            shutil.move(temp, output_path)
        else:
            shutil.copy(video_path, output_path)
        return output_path
