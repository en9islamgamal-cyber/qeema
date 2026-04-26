"""
sfx_engine.py — VALUE / QEEMA v8.0 (Enterprise Audio Mastering)
===============================================================
محرك هندسة الصوت الاحترافي للمشروع.
المهام:
- تطبيق معايير البث العالمية لتطبيع الصوت (EBU R128: -16 LUFS).
- معالجة ذكية للقرآن (Crossfade Illusion) بإضافة فترات صمت (Padding) 
  في النهاية لمنع قطع التلاوة أو رنين الصوت (Reverb).
- تلاشي (Fade in/out) ناعم لسلاسة الانتقالات.
"""

import logging
import subprocess as sp
from pathlib import Path
from typing import Dict, Any

from config import SFXConfig

logger = logging.getLogger(__name__)

class SFXEngine:
    
    def _run_ffmpeg_audio(self, cmd: list[str], context: str) -> bool:
        """تشغيل FFmpeg لمعالجة الصوت مع استخراج الأخطاء بدقة."""
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error(f"❌ فشل FFmpeg [{context}]:\n{result.stderr[-400:]}")
                return False
            return True
        except Exception as e:
            logger.error(f"❌ استثناء في FFmpeg [{context}]: {e}")
            return False

    def process_all(self, audio_map: Dict[str, str], script: Any, ep_dir: str) -> Dict[str, str]:
        logger.info("🎛️ بدء مرحلة المكساج السينمائي (Audio Mastering & Ducking Preparation)...")
        processed_map = {}
        
        out_dir = Path(ep_dir) / "sfx_mastered"
        out_dir.mkdir(parents=True, exist_ok=True)

        for key, path_str in audio_map.items():
            in_path = Path(path_str)
            if not in_path.exists():
                logger.warning(f"⚠️ الملف الصوتي غير موجود، سيتم التخطي: {in_path}")
                continue
            
            out_file = str(out_dir / f"master_{in_path.name}")
            
            # ─── هندسة صوت التلاوة القرآنية ───
            # القرآن يحتاج قدسية في النقل، لا يجوز قطعه:
            # 1. adelay: تأخير 0.5 ثانية قبل بدء التلاوة.
            # 2. afade (in): دخول متدرج لحماية نَفَس القارئ.
            # 3. apad: إضافة 0.8 ثانية من الصمت في النهاية للحفاظ على تردد الصوت (Reverb Tail).
            if key.endswith("_ayah") or "recite" in key:
                filter_chain = (
                    "aresample=44100,"
                    "adelay=500|500,"
                    f"afade=t=in:st=0:d={SFXConfig.FADE_IN_DURATION + 0.2},"
                    "apad=pad_dur=0.8,"
                    f"loudnorm=I={SFXConfig.NORMALIZATION_TARGET}:TP=-1.5:LRA=11"
                )
            # ─── هندسة صوت الراوي (الجد أبو زياد) ───
            else:
                filter_chain = (
                    "aresample=44100,"
                    f"afade=t=in:st=0:d={SFXConfig.FADE_IN_DURATION},"
                    f"afade=t=out:st=999:d={SFXConfig.FADE_OUT_DURATION},"
                    f"loudnorm=I={SFXConfig.NORMALIZATION_TARGET}:TP=-1.5:LRA=11"
                )

            cmd = [
                "ffmpeg", "-y", 
                "-i", str(in_path), 
                "-af", filter_chain, 
                "-c:a", "libmp3lame", 
                "-q:a", "2", # جودة MP3 عالية جداً (VBR)
                out_file
            ]
            
            if self._run_ffmpeg_audio(cmd, context=key):
                processed_map[key] = out_file
            else:
                logger.warning(f"⚠️ استخدام الملف الخام لـ {key} كخيار طوارئ.")
                processed_map[key] = path_str
                
        logger.info(f"✅ اكتملت هندسة الصوت لـ {len(processed_map)} ملفات بنجاح.")
        return processed_map