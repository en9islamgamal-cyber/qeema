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
    from utils import get_video_duration, run_ffmpeg_command
    from constants import ENCOURAGEMENTS
except ImportError:
    # قيم افتراضية للتشغيل المستقل
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
    """تطبيق شارة التقدم، اللوجو، والنصوص التشجيعية على الفيديو النهائي"""
    
    def __init__(self, font_path: str = None):
        """
        Args:
            font_path: مسار خط يدعم العربية (اختياري، إذا لم يوجد فلن تُضاف النصوص)
        """
        self.font = font_path
        if self.font and not Path(self.font).exists():
            logger.warning(f"الخط المحدد غير موجود: {self.font}. سيتم تعطيل النصوص التشجيعية.")
            self.font = None
    
    def _prepare_arabic_text(self, text: str) -> str:
        """تجهيز النص العربي لـ ffmpeg (escape الأحرف الخاصة)"""
        # استبدال الاقتباسات المنفردة والمزدوجة
        safe = text.replace("'", r"\'").replace('"', r'\"')
        # إزالة أي حروف غير قابلة للطباعة
        safe = ''.join(ch for ch in safe if ch.isprintable() or ch == ' ')
        return safe
    
    def apply_to_episode(self, video_path: str, script: "EpisodeScript", output_path: str) -> str:
        """
        يطبق جميع تأثيرات التلعيب (اللوجو + شريط التقدم + التشجيع) 
        في مسار واحد (Single-Pass Complex Filter) لضمان أعلى جودة.
        
        Returns:
            مسار الفيديو الناتج (الملف المعدل أو النسخة الأصلية في حالة الفشل).
        """
        # التحقق من وجود الفيديو الأصلي
        if not Path(video_path).exists():
            logger.error(f"❌ الفيديو الأصلي غير موجود: {video_path}")
            return video_path
        
        # الحصول على مدة الفيديو
        duration = self._get_duration(video_path)
        if duration <= 0:
            logger.error("❌ لم أتمكن من قراءة مدة الفيديو، سيتم تجاوز التلعيب.")
            shutil.copy(video_path, output_path)
            return output_path
        
        logger.info(f"🎮 بدء تطبيق التلعيب على فيديو مدته {duration:.1f} ثانية...")
        
        logo_path = Paths.LOGO_PRIMARY
        has_logo = logo_path.exists()
        
        # بناء أوامر ffmpeg
        inputs = ["-y", "-i", video_path]
        if has_logo:
            inputs.extend(["-i", str(logo_path)])
        
        filter_parts = []
        
        # 1. إضافة اللوجو (مقياس + شفافية + موضع)
        if has_logo:
            # معالجة أفضل للشفافية: دعم PNG مع قناة ألفا
            filter_parts.append(
                "[1:v]scale=160:-1,format=rgba,colorchannelmixer=aa=0.85[wm];"
                "[0:v][wm]overlay=W-w-30:30[v_base]"
            )
        else:
            filter_parts.append("[0:v]copy[v_base]")
        
        # 2. شريط التقدم (خلفية سوداء شفافة، ثم شريط ذهبي يتقدم مع الوقت)
        # نستخدم 'drawbox' مرتين: الأولى للخلفية، الثانية للشريط المتقدم
        filter_parts.append(
            "[v_base]drawbox=x=0:y=H-12:w=W:h=12:color=black@0.6:t=fill[v_box1]"
        )
        # شريط التقدم الذهبي: عرضه يتناسب مع الوقت المنقضي
        progress_width = f"W*(t/{duration})"
        filter_parts.append(
            f"[v_box1]drawbox=x=0:y=H-12:w={progress_width}:h=12:color=#FFD700@0.9:t=fill[v_box2]"
        )
        
        # 3. النص التشجيعي (يظهر في منتصف المدة لمدة 4 ثوانٍ)
        if self.font:
            encouragement = random.choice(ENCOURAGEMENTS)
            safe_text = self._prepare_arabic_text(encouragement)
            
            # زمن البدء: بعد 50% من المدة، والنهاية بعد 4 ثوانٍ أو قبل نهاية الفيديو بـ 1 ثانية
            start_time = duration * 0.5
            end_time = min(start_time + 4.0, duration - 1.0)
            if end_time > start_time:
                # تأثير تلاشي تدريجي عند الدخول والخروج
                fade_duration = 0.5
                alpha_logic = (
                    f"if(lt(t,{start_time+fade_duration}),"
                    f"(t-{start_time})/{fade_duration},"
                    f"if(gt(t,{end_time-fade_duration}),"
                    f"({end_time}-t)/{fade_duration},1))"
                )
                
                text_filter = (
                    f"[v_box2]drawtext=fontfile='{self.font}':text='{safe_text}':"
                    f"fontcolor=yellow@1.0:fontsize=75:x=(W-text_w)/2:y=H*0.15:"
                    f"enable='between(t,{start_time},{end_time})':alpha='{alpha_logic}':"
                    f"shadowcolor=black@0.8:shadowx=4:shadowy=4[vout]"
                )
                filter_parts.append(text_filter)
            else:
                logger.warning("⚠️ مدة الفيديو قصيرة جداً لعرض النص التشجيعي، سيتم تخطيه.")
                filter_parts.append("[v_box2]copy[vout]")
        else:
            filter_parts.append("[v_box2]copy[vout]")
        
        # تجميع سلسلة الفلاتر
        vf_string = ";".join(filter_parts)
        
        # أمر ffmpeg النهائي
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
        
        # تنفيذ الأمر مع مهلة 15 دقيقة
        success = self._run_ffmpeg(cmd, timeout=900)
        
        if success and Path(output_path).exists():
            logger.info("✅ تمت إضافة اللوجو وتأثيرات التلعيب بنجاح.")
            return output_path
        else:
            logger.warning("⚠️ فشل تطبيق التلعيب، سيتم استخدام الفيديو الأصلي كإجراء احتياطي.")
            shutil.copy(video_path, output_path)
            return output_path
    
    def _get_duration(self, video_path: str) -> float:
        """استخراج مدة الفيديو باستخدام ffprobe"""
        try:
            cmd = [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            result = sp.run(cmd, capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except Exception as e:
            logger.error(f"فشل قراءة المدة: {e}")
            return 0.0
    
    def _run_ffmpeg(self, cmd, timeout: int) -> bool:
        """تنفيذ أمر ffmpeg مع معالجة الأخطاء"""
        logger.debug(f"تشغيل: {' '.join(cmd)}")
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                logger.error(f"ffmpeg error (code {result.returncode}): {result.stderr[-500:]}")
                return False
            return True
        except sp.TimeoutExpired:
            logger.error(f"انتهت المهلة ({timeout} ثانية) أثناء معالجة الفيديو.")
            return False
        except Exception as e:
            logger.error(f"استثناء غير متوقع: {e}")
            return False