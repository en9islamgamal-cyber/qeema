"""
sfx_engine.py — VALUE / QEEMA v7.0 (Ultimate Cinematic Audio)
==============================================================
محرك المؤثرات الصوتية الاحترافي:
  - معالجة الفلاتر المعقدة (Complex Filtergraphs) لـ FFmpeg.
  - طبقات الهندسة الصوتية: Noise Reduction, EQ, Compression, Loudnorm.
  - معالجة مخصصة للراوي (دافئ وحيوي) وللقرآن (نقي وخاشع).
  - أتمتة كاملة واستكشاف متقدم للأخطاء.
"""

import logging
import subprocess as sp
from pathlib import Path
from typing import Dict, Any

from config import SFXConfig

logger = logging.getLogger(__name__)


class SFXEngine:
    """محرك معالجة الصوت والمكساج — يطبق خوارزميات هندسة صوتية احترافية."""

    def __init__(self):
        # معايير البث العالمية (Youtube/Podcast Standard)
        self.target_i = -16.0  # Integrated Loudness
        self.target_tp = -1.5  # True Peak
        self.lra = 11.0        # Loudness Range

    @staticmethod
    def _run_ffmpeg(cmd: list[str], timeout: int = 120) -> bool:
        """تنفيذ أوامر FFmpeg بأمان مع التقاط ومعالجة الأخطاء بدقة."""
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                logger.warning(f"⚠️ خطأ في FFmpeg: {result.stderr[-400:]}")
                return False
            return True
            
        except sp.TimeoutExpired:
            logger.error("❌ انتهى وقت تنفيذ FFmpeg (Timeout).")
            return False
        except Exception as e:
            logger.error(f"❌ استثناء غير متوقع أثناء تشغيل FFmpeg: {e}")
            return False

    def _build_narrator_chain(self) -> str:
        """
        خوارزمية الهندسة الصوتية للراوي (Narrator):
        1. afftdn: تقليل الضوضاء (Noise Reduction).
        2. highpass: قطع الترددات المنخفضة المزعجة (أقل من 80Hz).
        3. acompressor: ضغط ديناميكي لجعل الصوت دافئاً ومتماسكاً.
        4. silenceremove: إزالة الصمت الزائد من الأطراف.
        5. loudnorm: توحيد مستوى الصوت العالمي.
        6. afade: تلاشي ناعم للدخول والخروج.
        """
        noise_reduction = "afftdn=nf=-25"
        eq_highpass = "highpass=f=80"
        compressor = "acompressor=threshold=-20dB:ratio=3:attack=5:release=50"
        
        trim = "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-40dB"
        trim_both = f"{trim},areverse,{trim},areverse"
        
        loudnorm = f"loudnorm=I={self.target_i}:TP={self.target_tp}:LRA={self.lra}"
        fade = f"afade=t=in:st=0:d={SFXConfig.FADE_IN_DURATION},afade=t=out:st=999:d={SFXConfig.FADE_OUT_DURATION}"
        
        # ربط السلسلة
        return f"{noise_reduction},{eq_highpass},{compressor},{trim_both},{loudnorm},{fade}"

    def _build_quran_chain(self) -> str:
        """
        خوارزمية الهندسة الصوتية للقرآن (Quran):
        1. الحفاظ على نقاء صوت القارئ الأصلي (بدون فلاتر ضغط تغير طبيعته).
        2. loudnorm: توحيد مستوى الصوت ليتساوى مع الراوي فلا ينزعج الطفل من اختلاف الصوت.
        3. afade: دخول تدريجي بطيء (Fade-in) لحماية نفس القارئ في البداية وإعطاء طابع خشوعي.
        """
        loudnorm = f"loudnorm=I={self.target_i}:TP={self.target_tp}:LRA={self.lra}"
        
        # الدخول التدريجي للقرآن يجب أن يكون أطول قليلاً (مثلاً 0.5 ثانية) لتهيئة الأذن
        fade = "afade=t=in:st=0:d=0.5"
        
        return f"{loudnorm},{fade}"

    def _process_one(self, in_path: str, out_path: str, is_quran: bool = False) -> bool:
        """تطبيق الفلتر المناسب (راوي أو قرآن) على مسار صوتي مفرد."""
        audio_filter = self._build_quran_chain() if is_quran else self._build_narrator_chain()

        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-af", audio_filter,
            # استخدام أعلى جودة لتشفير الصوت
            "-c:a", "libmp3lame", "-q:a", "2", "-ar", "44100", 
            out_path,
        ]
        
        return self._run_ffmpeg(cmd, timeout=90)

    def process_all(self, audio_map: Dict[str, str], script: Any, ep_dir: str) -> Dict[str, str]:
        """معالجة جميع الملفات الصوتية للحلقة دفعة واحدة."""
        logger.info("🎛️ جاري تطبيق المكساج السينمائي (Noise Reduction, EQ, Compression, Loudnorm)...")
        
        processed_map: Dict[str, str] = {}
        sfx_dir = Path(ep_dir) / "sfx_mastered"
        sfx_dir.mkdir(parents=True, exist_ok=True)

        for key, src_path_str in audio_map.items():
            src_path = Path(src_path_str)
            
            if not src_path.exists():
                logger.warning(f"⚠️ الملف غير موجود، سيتم تخطيه: {src_path}")
                processed_map[key] = src_path_str
                continue

            out_path = str(sfx_dir / f"mastered_{src_path.name}")
            is_quran = key.endswith("_ayah")
            
            logger.debug(f"معالجة المسار: {key} (قرآن: {is_quran})")
            success = self._process_one(src_path_str, out_path, is_quran=is_quran)

            if success and Path(out_path).exists():
                processed_map[key] = out_path
            else:
                logger.warning(f"⚠️ فشلت المعالجة المتقدمة لـ [{key}]، سيتم استخدام الملف الخام.")
                processed_map[key] = src_path_str

        logger.info(f"✅ اكتمل المكساج لـ {len(processed_map)} ملفات بنجاح.")
        return processed_map
