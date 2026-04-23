"""
sfx_engine.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
═══════════════════════════════════════════════════════
محرك المؤثرات الصوتية (Cinematic Audio Engine)
• دمج لحظي وتطبيع (Loudnorm) في مسار واحد (Single-Pass)
• تلاشي ديناميكي للموسيقى (Dynamic Fade-out) بناءً على طول الكلام
• توجيه ذكي للمزاج الصوتي (Smart Mood Routing)
═══════════════════════════════════════════════════════
"""

from __future__ import annotations
import json
import logging
import subprocess
import shutil
from pathlib import Path

from config import Paths, VideoConfig

logger = logging.getLogger(__name__)

MUSIC = {
    "intro":    "intro_nasheed.mp3",
    "calm":     "calm_bg.mp3",
    "reverent": "quran_ambient.mp3",
    "happy":    "cheerful_nasheed.mp3",
    "excited":  "celebration.mp3",
    "outro":    "outro_nasheed.mp3",
}

VOLUME = {
    "under_speech": "-20dB",
    "solo":         "-6dB",
    "quran":        "-24dB", # هادئ جداً خلف القرآن احتراماً للتلاوة
}


def _run(cmd: list[str], label: str = "", timeout: int = 120) -> bool:
    """تشغيل أوامر FFmpeg بأمان مع التقاط الأخطاء كنصوص"""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        logger.warning(f"⚠️ {label} فشل:\n{r.stderr[-200:]}")
        return False
    return True


def _probe_audio_duration(path: str) -> float:
    """يقرأ طول الملف الصوتي بدقة لضبط نقطة التلاشي (Fade-out)"""
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception as e:
        logger.warning(f"⚠️ لم أتمكن من قراءة طول {Path(path).name}، سيتم افتراض 10 ثوانٍ.")
        return 10.0


class SFXEngine:
    def __init__(self):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("❌ FFmpeg غير مثبت على السيرفر الأساسي!")

    def process_all(self, audio_map: dict, script, ep_dir: str) -> dict:
        """يعالج جميع الملفات الصوتية: دمج + تلاشي ديناميكي + تطبيع (في خطوة واحدة)"""
        proc_dir = Path(ep_dir) / "processed_audio"
        proc_dir.mkdir(parents=True, exist_ok=True)
        processed = {}

        for key, path in audio_map.items():
            if not Path(path).exists():
                logger.warning(f"⚠️ الملف الصوتي غير موجود: {path}")
                processed[key] = path
                continue
            
            out_final = str(proc_dir / f"{key}.mp4") # استخدام صيغة مؤقتة أو mp3 حسب الرغبة (mp3 هو الأساس هنا)
            out_final = str(proc_dir / f"{key}.mp3")

            # 1. التحديد الذكي للمزاج الصوتي بناءً على مفتاح المشهد (Key)
            mood = "calm"
            if "quran" in key:
                mood = "reverent"
            elif "intro" in key: # يشمل intro_scene و ayah_intro
                mood = "intro" if key == "intro" else "calm"
            elif "outro" in key:
                mood = "outro"

            # 2. تحديد الموسيقى المناسبة
            music_f = Paths.MUSIC / MUSIC.get(mood, MUSIC["calm"])
            
            # إذا لم توجد موسيقى، نكتفي بعمل Normalize لملف الصوت الخام
            if not music_f.exists():
                logger.debug(f"🔇 موسيقى {mood} مفقودة، سيتم تطبيع الصوت الخام فقط لـ {key}.")
                self._normalize_only(path, out_final)
                processed[key] = out_final
                continue

            # 3. حساب نقطة التلاشي (Dynamic Fade-out)
            duration = _probe_audio_duration(path)
            fade_out_start = max(0, duration - 1.5) # التلاشي يبدأ قبل النهاية بـ 1.5 ثانية

            # 4. المعالجة الشاملة في سطر واحد (Single-Pass: Mix -> Ducking -> Fade -> Normalize)
            vol = VOLUME["quran"] if mood == "reverent" else VOLUME["under_speech"]
            
            cmd = [
                "ffmpeg", "-y",
                "-i", path,
                "-stream_loop", "-1", # تكرار الموسيقى لتغطي الكلام
                "-i", str(music_f),
                "-filter_complex",
                # خفض صوت الموسيقى، عمل Fade In، وعمل Fade Out ديناميكي بناءً على طول الكلام
                f"[1:a]volume={vol},afade=t=in:st=0:d=1.2,afade=t=out:st={fade_out_start}:d=1.5[m_ducked];"
                # دمج الكلام مع الموسيقى وإنهاء الملف بانتهاء الكلام (duration=first)
                f"[0:a][m_ducked]amix=inputs=2:duration=first:weights='1 0.25'[mixed];"
                # تطبيع الصوت النهائي (Loudnorm) ليتوافق مع معايير يوتيوب (-16 LUFS)
                f"[mixed]loudnorm=I=-16:TP=-1.5:LRA=11[out]",
                "-map", "[out]",
                "-ar", str(VideoConfig.AUDIO_RATE),
                "-ac", "2",
                "-b:a", VideoConfig.AUDIO_BITRATE, # استخدام الجودة القصوى من الإعدادات بدلاً من القيمة الثابتة
                out_final,
            ]

            if _run(cmd, f"SFX Engine (Mix+Norm): {key}"):
                processed[key] = out_final
            else:
                shutil.copy(path, out_final)
                processed[key] = out_final

        logger.info(f"🎧 اكتملت الهندسة الصوتية لـ {len(processed)} ملفات صوتية.")
        return processed

    def _normalize_only(self, input_path: str, output_path: str) -> str:
        """يطبع الصوت الخام في حال عدم توفر موسيقى خلفية"""
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", str(VideoConfig.AUDIO_RATE),
            "-b:a", VideoConfig.AUDIO_BITRATE,
            output_path,
        ]
        if not _run(cmd, "SFX Normalize Only"):
            shutil.copy(input_path, output_path)
        return output_path

    def silence(self, duration: float, output_path: str) -> str:
        """توليد صمت نقي للوقفات إن لزم الأمر"""
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=r={VideoConfig.AUDIO_RATE}:cl=stereo",
            "-t", str(duration),
            "-b:a", VideoConfig.AUDIO_BITRATE,
            output_path,
        ]
        _run(cmd, f"Silence Generator ({duration}s)")
        return output_path
