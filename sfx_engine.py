"""
sfx_engine.py — VALUE / QEEMA v5.0
====================================
محرك المؤثرات الصوتية الفعلي (Refactored):
  - هيكلة كائنية التوجه (OOP) بالكامل.
  - فصل منطق بناء الفلاتر (Filters) لسهولة التعديل.
  - إصلاح خطأ كتم الصوت (Fade-out bug) في التلاوات القرآنية.
  - معالجة أخطاء واستثناءات أفضل لـ FFmpeg.
"""

import logging
import subprocess as sp
from pathlib import Path
from typing import Dict, Any

from config import SFXConfig

logger = logging.getLogger(__name__)


class SFXEngine:
    """محرك معالجة الصوت — يطبق عمليات التطبيع (Normalization) والانتقالات (Fades)."""

    @staticmethod
    def _run_ffmpeg(cmd: list[str], timeout: int = 120) -> bool:
        """تنفيذ أوامر FFmpeg بأمان مع التقاط ومعالجة الأخطاء."""
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                logger.warning(f"⚠️ خطأ في FFmpeg: {result.stderr[-250:]}")
                return False
            return True
            
        except sp.TimeoutExpired:
            logger.error("❌ انتهى وقت تنفيذ FFmpeg (Timeout).")
            return False
        except Exception as e:
            logger.error(f"❌ استثناء غير متوقع أثناء تشغيل FFmpeg: {e}")
            return False

    def _get_quran_filter(self) -> str:
        """
        توليد فلاتر تلاوة القرآن.
        تم الاكتفاء بتلاشي تدريجي (Fade-in) بسيط جداً في البداية 
        للحفاظ على أداء القارئ الأصلي ومنع أي كتم أو اقتطاع.
        """
        return "afade=t=in:st=0:d=0.1"

    def _get_standard_filter(self) -> str:
        """توليد فلاتر الصوت العادي (السرد والتأثيرات)."""
        # 1. إزالة الصمت من البداية
        trim_start = "silenceremove=start_periods=1:start_silence=0.2:start_threshold=-40dB"
        
        # 2. إزالة الصمت من النهاية (باستخدام عكس الصوت ثم إزالة الصمت ثم عكسه مجدداً)
        trim_both = f"{trim_start},areverse,{trim_start},areverse"
        
        # 3. توحيد مستوى الصوت (Loudness Normalization)
        loudnorm = f"loudnorm=I={SFXConfig.NORMALIZATION_TARGET}:TP=-1.5:LRA=11"
        
        # 4. التلاشي (Fade in & Fade out)
        fade_in = f"afade=t=in:st=0:d={SFXConfig.FADE_IN_DURATION}"
        fade_out = f"afade=t=out:st=999:d={SFXConfig.FADE_OUT_DURATION}"
        
        return f"{trim_both},{loudnorm},{fade_in},{fade_out}"

    def _process_one(self, in_path: str, out_path: str, is_quran: bool = False) -> bool:
        """معالجة ملف صوتي واحد وتطبيق الفلتر المناسب بناءً على نوعه."""
        audio_filter = self._get_quran_filter() if is_quran else self._get_standard_filter()

        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-af", audio_filter,
            "-c:a", "libmp3lame", "-b:a", "192k",
            out_path,
        ]
        
        return self._run_ffmpeg(cmd, timeout=60)

    def process_all(self, audio_map: Dict[str, str], script: Any, ep_dir: str) -> Dict[str, str]:
        """معالجة دفعة كاملة من الصوتيات الخاصة بالحلقة."""
        logger.info("🎵 جاري معالجة الصوتيات (Normalize + Fade)...")
        
        processed_map: Dict[str, str] = {}
        sfx_dir = Path(ep_dir) / "sfx"
        sfx_dir.mkdir(parents=True, exist_ok=True)

        for key, src_path_str in audio_map.items():
            src_path = Path(src_path_str)
            
            # التحقق من وجود الملف الأصلي
            if not src_path.exists():
                logger.warning(f"⚠️ الملف غير موجود، سيتم تخطيه: {src_path}")
                processed_map[key] = src_path_str
                continue

            out_path = str(sfx_dir / src_path.name)
            is_quran = key.endswith("_ayah")
            
            # تنفيذ المعالجة
            success = self._process_one(src_path_str, out_path, is_quran=is_quran)

            # التحقق من نجاح المخرجات
            if success and Path(out_path).exists():
                processed_map[key] = out_path
            else:
                logger.warning(f"⚠️ فشلت معالجة [{key}]، سيتم الاحتفاظ بالملف الأصلي كبديل آمن.")
                processed_map[key] = src_path_str

        logger.info(f"✅ اكتملت معالجة {len(processed_map)} ملفات صوتية.")
        return processed_map
