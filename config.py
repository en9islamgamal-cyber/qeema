"""
config.py — VALUE / QEEMA v4.0
إعدادات المشروع والثوابت.
"""

import os
from pathlib import Path
from typing import Dict, List


# ============================================================================
# مفاتيح API
# ============================================================================
class APIKeys:
    GEMINI = os.getenv("GEMINI_API_KEY", "")
    COHERE = os.getenv("COHERE_API_KEY", "")
    ANTHROPIC = os.getenv("ANTHROPIC_API_KEY", "")
    GROK = os.getenv("GROK_API_KEY", "")
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
    LEONARDO = os.getenv("LEONARDO_API_KEY", "")
    
    @classmethod
    def validate(cls) -> List[str]:
        missing = []
        if not cls.SUPABASE_URL:
            missing.append("SUPABASE_URL")
        if not cls.SUPABASE_KEY:
            missing.append("SUPABASE_KEY")
        # المفاتيح الأخرى اختيارية
        return missing


# ============================================================================
# مسارات المجلدات
# ============================================================================
class Paths:
    # المجلد الجذر للمشروع
    ROOT = Path(__file__).parent
    
    # مجلدات أساسية
    LOGS = ROOT / "logs"
    TEMP = ROOT / "temp"
    ASSETS = ROOT / "assets"
    OUTPUT = ROOT / "output"
    
    # مجلدات فرعية تحت temp
    TEMP_EPISODES = TEMP / "episodes"
    TTS_CACHE = TEMP / "tts_cache"
    ASSEMBLY_DIR = TEMP / "assembly"
    
    # مجلدات المخرجات
    VIDEOS = OUTPUT / "videos"
    THUMBNAILS = ASSETS / "thumbnails"
    FONTS = ASSETS / "fonts"
    OVERLAYS = ASSETS / "overlays"
    
    # مسار حفظ السكريبتات (يمكن أن يكون تحت temp)
    SCRIPT_DIR = TEMP_EPISODES   # أو TEMP / "scripts"
    
    # مسار اللوجو الرئيسي
    LOGO_PRIMARY = ASSETS / "logo.png"
    
    @classmethod
    def ensure_all(cls):
        """إنشاء جميع المجلدات الضرورية إن لم تكن موجودة."""
        dirs = [
            cls.LOGS,
            cls.TEMP,
            cls.TEMP_EPISODES,
            cls.TTS_CACHE,
            cls.ASSEMBLY_DIR,
            cls.VIDEOS,
            cls.THUMBNAILS,
            cls.FONTS,
            cls.OVERLAYS,
            cls.SCRIPT_DIR,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# ============================================================================
# تكوين الفيديو
# ============================================================================
class VideoConfig:
    CODEC = "libx264"
    PROFILE = "high"
    CRF = 18
    PIX_FMT = "yuv420p"
    PRESET = "medium"
    AUDIO_CODEC = "aac"
    AUDIO_BITRATE = "128k"


# ============================================================================
# تكوين قاعدة البيانات
# ============================================================================
class DBConfig:
    TABLE_EPISODES = "episodes"
    TABLE_PIPELINE_STATE = "pipeline_state"
    # إضافة اسم الجدول إذا لزم الأمر


# ============================================================================
# منهج الحلقات (يمكنك توسيعه)
# ============================================================================
CURRICULUM: Dict[int, Dict[str, object]] = {
    1: {"surah": 1, "name": "الفاتحة", "start": 1, "end": 7, "title": "فاتحة الكتاب"},
    2: {"surah": 112, "name": "الإخلاص", "start": 1, "end": 4, "title": "سورة الإخلاص"},
    3: {"surah": 113, "name": "الفلق", "start": 1, "end": 5, "title": "سورة الفلق"},
    4: {"surah": 114, "name": "الناس", "start": 1, "end": 6, "title": "سورة الناس"},
    5: {"surah": 109, "name": "الكافرون", "start": 1, "end": 6, "title": "سورة الكافرون"},
    6: {"surah": 110, "name": "النصر", "start": 1, "end": 3, "title": "سورة النصر"},
    7: {"surah": 111, "name": "المسد", "start": 1, "end": 5, "title": "سورة المسد"},
    8: {"surah": 108, "name": "الكوثر", "start": 1, "end": 3, "title": "سورة الكوثر"},
    9: {"surah": 106, "name": "قريش", "start": 1, "end": 4, "title": "سورة قريش"},
    10: {"surah": 105, "name": "الفيل", "start": 1, "end": 5, "title": "سورة الفيل"},
    11: {"surah": 107, "name": "الماعون", "start": 1, "end": 7, "title": "سورة الماعون"},
    12: {"surah": 104, "name": "الهمزة", "start": 1, "end": 9, "title": "سورة الهمزة"},
    13: {"surah": 103, "name": "العصر", "start": 1, "end": 3, "title": "سورة العصر"},
    14: {"surah": 102, "name": "التكاثر", "start": 1, "end": 8, "title": "سورة التكاثر"},
    15: {"surah": 101, "name": "القارعة", "start": 1, "end": 11, "title": "سورة القارعة"},
    16: {"surah": 100, "name": "العاديات", "start": 1, "end": 11, "title": "سورة العاديات"},
}

# في حالة طلب رقم سورة (surah number) من المنهج
def get_surah_name(surah_number: int) -> str:
    for ep in CURRICULUM.values():
        if ep.get("surah") == surah_number:
            return str(ep.get("name", ""))
    return ""