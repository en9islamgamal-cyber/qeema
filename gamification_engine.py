"""
gamification_engine.py — VALUE / QEEMA v3.0
محرك إضافة تأثيرات التلعيب (Gamification): لوجو، شريط تقدم، نصوص تشجيعية.
"""

import logging
import random
import shutil
import subprocess as sp
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from script_engine import EpisodeScript

# استيرادات من المشروع (يفترض وجودها في config و utils)
try:
    from config import VideoConfig, Paths
except ImportError:
    class VideoConfig:
        CODEC = 'libx264'
        PROFILE = 'high'
        CRF = 18
        PIX_FMT = 'yuv420p'
    class Paths:
        LOGO_PRIMARY = Path("assets/logo.png")

ENCOURAGEMENTS = ["👍 ممتاز!", "💪 استمر!", "🎯 قمة التركيز", "🧠 عبقرية"]

logger = logging.getLogger(__name__)


class GamificationEngine:
    def __init__(self, font_path: str = None):
        self.font = font_path
        if self.font and not Path(self.font).exists():
            logger.warning(f"الخط غير موجود: {self.font}. سيتم تعطيل النصوص التشجيعية.")
            self.font = None

    def _prepare_arabic_text(self, text: str) -> str:
        safe = text.replace("'", r"\'").replace('"', r'\"')
        safe = ''.join(ch for ch in safe if ch.isprintable() or ch == ' ')
        return safe

    def apply_to_episode(self, video_path: str, script: "EpisodeScript", output_path: str) -> str:
        if not Path(video_path).exists():
            logger.error(f"❌ الفيديو الأصلي غير موجود: {video_path}")
            return video_path

        duration = self._get_duration(video_path)
        if duration <= 0:
            logger.error("❌ لم أتمكن من قراءة مدة الفيديو، سيتم تجاوز التلعيب.")
            shutil.copy(video_path, output_path)
            return output_path

        logger.info(f"🎮 بدء تطبيق التلعيب على فيديو مدته {duration:.1f} ثانية...")

        logo_path = Paths.LOGO_PRIMARY
        has_logo = logo_path.exists()

        inputs = ["-y", "-i", video_path]
        if has_logo:
            inputs.extend(["-i", str(logo_path)])

        filter_parts = []

        # 1. إضافة اللوجو
        if has_logo:
            filter_parts.append("[1:v]scale=160:-1,format=rgba,colorchannelmixer=aa=0.85[wm];[0:v][wm]overlay=W-w-30:30[v_base]")
        else:
            filter_parts.append("[0:v]copy[v_base]")

        # 2. شريط التقدم
        filter_parts.append("[v_base]drawbox=x=0:y=H-12:w=W:h=12:color=black@0.6:t=fill[v_box1]")
        filter_parts.append(f"[v_box1]drawbox=x=0:y=H-12:w=W*(t/{duration}):h=12:color=#FFD700@0.9:t=fill[v_box2]")

        # 3. النص التشجيعي
        if self.font:
            encouragement = random.choice(ENCOURAGEMENTS)
            safe_text = self._prepare_arabic_text(encouragement)
            start_time = duration * 0.5
            end_time = min(start_time + 4.0, duration - 1.0)
            if end_time > start_time:
                fade = 0.5
                alpha_logic = f"if(lt(t,{start_time+fade}),(t-{start_time})/{fade},if(gt(t,{end_time-fade}),({end_time}-t)/{fade},1))"
                text_filter = (
                    f"[v_box2]drawtext=fontfile='{self.font}':text='{safe_text}':"
                    f"fontcolor=yellow@1.0:fontsize=75:x=(W-text_w)/2:y=H*0.15:"
                    f"enable='between(t,{start_time},{end_time})':alpha='{alpha_logic}':"
                    f"shadowcolor=black@0.8:shadowx=4:shadowy=4[vout]"
                )
                filter_parts.append(text_filter)
            else:
                filter_parts.append("[v_box2]copy[vout]")
        else:
            filter_parts.append("[v_box2]copy[vout]")

        vf_string = ";".join(filter_parts)

        cmd = inputs + [
            "-filter_complex", vf_string,
            "-map", "[vout]",
            "-map", "0:a",
            "-c:v", VideoConfig.CODEC,
            "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF),
            "-preset", "fast",
            "-pix_fmt", VideoConfig.PIX_FMT,
            "-c:a", "copy",
            output_path
        ]

        success = self._run_ffmpeg(cmd, timeout=900)
        if success and Path(output_path).exists():
            logger.info("✅ تمت إضافة اللوجو وتأثيرات التلعيب بنجاح.")
            return output_path
        else:
            logger.warning("⚠️ فشل التلعيب، سيتم استخدام الفيديو الأصلي.")
            shutil.copy(video_path, output_path)
            return output_path

    def _get_duration(self, video_path: str) -> float:
        try:
            cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            result = sp.run(cmd, capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except Exception:
            return 0.0

    def _run_ffmpeg(self, cmd, timeout: int) -> bool:
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"ffmpeg error: {e}")
            return False