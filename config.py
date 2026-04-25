"""
config.py — VALUE / QEEMA v5.0 (Master Configuration)
يحتوي على جميع الثوابت والإعدادات. مع إضافات v5:
- ElevenLabs voice config (Antoni - أب حنون)
- Branding paths لـ intro/outro الموحد
- مسار الـ jingle والموسيقى الخلفية
"""

import os
from pathlib import Path
from typing import Dict, List


# ════════════════════════════════════════════════════════════════
# مفاتيح API (من متغيرات البيئة)
# ════════════════════════════════════════════════════════════════
class APIKeys:
    GEMINI = os.getenv("GEMINI_API_KEY", "")
    COHERE = os.getenv("COHERE_API_KEY", "")
    ANTHROPIC = os.getenv("ANTHROPIC_API_KEY", "")
    GROK = os.getenv("GROK_API_KEY", "")
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
    LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY", "")
    LEONARDO = LEONARDO_API_KEY
    # ✅ NEW v5
    ELEVENLABS = os.getenv("ELEVENLABS_API_KEY", "")
    YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
    YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    @classmethod
    def validate(cls) -> List[str]:
        missing = []
        if not cls.SUPABASE_URL: missing.append("SUPABASE_URL")
        if not cls.SUPABASE_KEY: missing.append("SUPABASE_KEY")
        # واحد على الأقل من الـ TTS مطلوب
        if not cls.ELEVENLABS and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            missing.append("ELEVENLABS_API_KEY أو GCP_SA_KEY")
        return missing


# ════════════════════════════════════════════════════════════════
# مسارات المجلدات والملفات
# ════════════════════════════════════════════════════════════════
class Paths:
    ROOT = Path(__file__).parent
    LOGS = ROOT / "logs"
    TEMP = ROOT / "temp"
    ASSETS = ROOT / "assets"
    OUTPUT = ROOT / "output"

    TEMP_EPISODES = TEMP / "episodes"
    TTS_CACHE = TEMP / "tts_cache"
    ASSEMBLY_DIR = TEMP / "assembly"

    VIDEOS = OUTPUT / "videos"
    THUMBNAILS = ASSETS / "thumbnails"
    FONTS = ASSETS / "fonts"
    OVERLAYS = ASSETS / "overlays"

    SCRIPT_DIR = TEMP_EPISODES
    LOGO_PRIMARY = ASSETS / "logo.png"

    # ✅ NEW v5: Branding paths
    BRANDING = ASSETS / "branding"
    INTRO_VIDEO = BRANDING / "intro.mp4"      # محفوظ - يُولّد مرة واحدة
    OUTRO_VIDEO = BRANDING / "outro.mp4"      # محفوظ - يُولّد مرة واحدة
    JINGLE = OVERLAYS / "jingle.mp3"          # موسيقى الانترو القصيرة
    BGM = OVERLAYS / "bgm.mp3"                # موسيقى الخلفية أثناء السرد
    QURAN_CACHE = ASSETS / "quran_audio"      # cache للتلاوات

    @classmethod
    def ensure_all(cls):
        dirs = [
            cls.LOGS, cls.TEMP, cls.TEMP_EPISODES, cls.TTS_CACHE,
            cls.ASSEMBLY_DIR, cls.VIDEOS, cls.THUMBNAILS,
            cls.FONTS, cls.OVERLAYS, cls.SCRIPT_DIR,
            cls.BRANDING, cls.QURAN_CACHE,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════
# إعدادات الفيديو
# ════════════════════════════════════════════════════════════════
class VideoConfig:
    CODEC = "libx264"
    PROFILE = "high"
    CRF = 18
    PIX_FMT = "yuv420p"
    PRESET = "medium"
    AUDIO_CODEC = "aac"
    AUDIO_BITRATE = "192k"
    FPS = 30
    RESOLUTION_WIDTH = 1920
    RESOLUTION_HEIGHT = 1080


# ════════════════════════════════════════════════════════════════
# إعدادات البصريات
# ════════════════════════════════════════════════════════════════
class VisualConfig:
    WIDTH = 1920
    HEIGHT = 1080
    BACKGROUND_COLOR = (245, 222, 179)  # wheat بدل أخضر فاضي
    TEXT_COLOR = (34, 34, 34)
    STROKE_COLOR = (255, 255, 255)
    STROKE_WIDTH = 3
    FONT_SIZE_TITLE = 80
    FONT_SIZE_BODY = 50
    _amiri = Paths.FONTS / "Amiri-Bold.ttf"
    _noto = Paths.FONTS / "NotoSansArabic-Bold.ttf"
    FONT_PATH = str(_amiri) if _amiri.exists() else (str(_noto) if _noto.exists() else None)
    OUTPUT_FORMAT = "JPEG"
    OUTPUT_QUALITY = 92


# ════════════════════════════════════════════════════════════════
# إعدادات الصوت
# ════════════════════════════════════════════════════════════════
class AudioConfig:
    # Google TTS fallback
    DEFAULT_VOICE = "ar-XA-Wavenet-B"
    DEFAULT_SPEAKING_RATE = 0.95
    DEFAULT_PITCH = -1.0
    VOLUME_GAIN_DB = 0.0

    # ✅ NEW v5: ElevenLabs config
    ELEVENLABS_MODEL = "eleven_multilingual_v2"
    # Antoni — أب حنون (دافئ وواضح، ممتاز لمحتوى الأطفال)
    ELEVENLABS_VOICE_ID_ANTONI = "ErXwobaYiN019PkySvjV"
    ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or ELEVENLABS_VOICE_ID_ANTONI

    # Voice settings (tuned for warm storytelling)
    ELEVENLABS_STABILITY = 0.55
    ELEVENLABS_SIMILARITY = 0.85
    ELEVENLABS_STYLE = 0.45
    ELEVENLABS_SPEAKER_BOOST = True


# ════════════════════════════════════════════════════════════════
# إعدادات SFX
# ════════════════════════════════════════════════════════════════
class SFXConfig:
    FADE_IN_DURATION = 0.3
    FADE_OUT_DURATION = 0.3
    NORMALIZATION_TARGET = -16.0
    SAMPLE_RATE = 44100


# ════════════════════════════════════════════════════════════════
# ✅ NEW v5: إعدادات Branding (Intro/Outro)
# ════════════════════════════════════════════════════════════════
class BrandingConfig:
    INTRO_DURATION = 5.0     # ثواني
    OUTRO_DURATION = 5.0
    CHANNEL_NAME_AR = "قِيمَة"
    CHANNEL_NAME_EN = "VALUE"
    CHANNEL_TAGLINE_AR = "قناة الأطفال الدينية"
    SUBSCRIBE_TEXT = "اشترك في القناة"

    # ألوان الهوية (مستخرجة من اللوجو)
    PRIMARY_COLOR = "#FFA500"     # برتقالي دافئ
    SECONDARY_COLOR = "#FF6B35"   # برتقالي محمر
    ACCENT_COLOR = "#FFD700"      # ذهبي
    TEXT_COLOR_LIGHT = "#FFFFFF"
    TEXT_COLOR_DARK = "#2C3E50"
    BG_COLOR = "#FAF3E0"          # كريمي دافئ


# ════════════════════════════════════════════════════════════════
# إعدادات قاعدة البيانات
# ════════════════════════════════════════════════════════════════
class DBConfig:
    TABLE_EPISODES = "episodes"
    TABLE_PIPELINE_STATE = "pipeline_state"


# ════════════════════════════════════════════════════════════════
# المنهج (سور القرآن — من الفاتحة ثم جزء عم بالعكس من النهاية)
# ════════════════════════════════════════════════════════════════
CURRICULUM: Dict[int, Dict[str, object]] = {
    1: {"surah": 1, "name": "الفاتحة", "start": 1, "end": 7, "title": "فاتحة الكتاب"},
    2: {"surah": 114, "name": "الناس", "start": 1, "end": 6, "title": "سورة الناس"},
    3: {"surah": 113, "name": "الفلق", "start": 1, "end": 5, "title": "سورة الفلق"},
    4: {"surah": 112, "name": "الإخلاص", "start": 1, "end": 4, "title": "سورة الإخلاص"},
    5: {"surah": 111, "name": "المسد", "start": 1, "end": 5, "title": "سورة المسد"},
    6: {"surah": 110, "name": "النصر", "start": 1, "end": 3, "title": "سورة النصر"},
    7: {"surah": 109, "name": "الكافرون", "start": 1, "end": 6, "title": "سورة الكافرون"},
    8: {"surah": 108, "name": "الكوثر", "start": 1, "end": 3, "title": "سورة الكوثر"},
    9: {"surah": 107, "name": "الماعون", "start": 1, "end": 7, "title": "سورة الماعون"},
    10: {"surah": 106, "name": "قريش", "start": 1, "end": 4, "title": "سورة قريش"},
    11: {"surah": 105, "name": "الفيل", "start": 1, "end": 5, "title": "سورة الفيل"},
    12: {"surah": 104, "name": "الهمزة", "start": 1, "end": 9, "title": "سورة الهمزة"},
    13: {"surah": 103, "name": "العصر", "start": 1, "end": 3, "title": "سورة العصر"},
    14: {"surah": 102, "name": "التكاثر", "start": 1, "end": 8, "title": "سورة التكاثر"},
    15: {"surah": 101, "name": "القارعة", "start": 1, "end": 11, "title": "سورة القارعة"},
    16: {"surah": 100, "name": "العاديات", "start": 1, "end": 11, "title": "سورة العاديات"},
}


def get_surah_name(surah_number: int) -> str:
    for ep in CURRICULUM.values():
        if ep.get("surah") == surah_number:
            return str(ep.get("name", ""))
    return ""
