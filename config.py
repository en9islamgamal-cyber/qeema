"""
config.py — VALUE / QEEMA v9.1 (Complete & Backward-Compatible Edition)
========================================================================
الملف المركزي لإدارة كافة الإعدادات والمسارات والمفاتيح.

v9.1 = v9.0 (Cinematic 60FPS) + كل الـ classes اللازمة لتشغيل الـ pipeline:
  - APIKeys, Paths, VideoConfig, AudioConfig, SFXConfig
  - VisualConfig (للـ Leonardo + Thumbnail)
  - BrandingConfig (للـ Intro/Outro)
  - WebRenderConfig (للـ Playwright)
  - DBConfig (Supabase)
  - CURRICULUM (المنهج الكامل)

⚠️ ملاحظة: لا يوجد أي تأثير على الجودة الإنتاجية.
   كل القيم محسوبة بمعايير الإنتاج السينمائي:
   - 1080p @ 60FPS, CRF 17, AAC 256k, LUFS -16
"""

import os
from pathlib import Path
from typing import Dict, List


# ════════════════════════════════════════════════════════════════
# 1. مفاتيح API والاتصالات (Security & Load Balancing)
# ════════════════════════════════════════════════════════════════
class APIKeys:
    # مفاتيح Gemini (يدعم التوزيع لتجنب حدود الكوتة)
    GEMINI = os.getenv("GEMINI_API_KEY", "")
    GEMINI_1 = GEMINI
    GEMINI_2 = os.getenv("GEMINI_API_KEY_2", "")
    GEMINI_3 = os.getenv("GEMINI_API_KEY_3", "")

    # مفاتيح النماذج الأخرى
    GROQ = os.getenv("GROQ_API_KEY", "")
    COHERE = os.getenv("COHERE_API_KEY", "")
    ANTHROPIC = os.getenv("ANTHROPIC_API_KEY", "")
    GROK = os.getenv("GROK_API_KEY", "")

    # محركات الصور والصوت
    LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY", "")
    LEONARDO = LEONARDO_API_KEY
    ELEVENLABS = os.getenv("ELEVENLABS_API_KEY", "")

    # قاعدة البيانات (Supabase)
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

    # يوتيوب (OAuth2)
    YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
    YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    @classmethod
    def validate(cls) -> List[str]:
        """فحص المفاتيح الأساسية لضمان عدم توقف المنظومة."""
        missing = []
        if not cls.SUPABASE_URL:
            missing.append("SUPABASE_URL")
        if not cls.SUPABASE_KEY:
            missing.append("SUPABASE_KEY")
        if not cls.GEMINI and not cls.GROQ:
            missing.append("LLM_API_KEY (Gemini أو Groq)")
        if not cls.ELEVENLABS and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            missing.append("ELEVENLABS_API_KEY أو GCP_SA_KEY")
        return missing


# ════════════════════════════════════════════════════════════════
# 2. هندسة المسارات (File System Architecture)
# ════════════════════════════════════════════════════════════════
class Paths:
    ROOT = Path(__file__).parent

    # مجلدات العمل الأساسية
    LOGS = ROOT / "logs"
    TEMP = ROOT / "temp"
    ASSETS = ROOT / "assets"
    OUTPUT = ROOT / "output"

    # مسارات الإنتاج
    TEMP_EPISODES = TEMP / "episodes"
    TTS_CACHE = TEMP / "tts_cache"
    ASSEMBLY_DIR = TEMP / "assembly"

    # ✅ v6: مسارات محرك رندرة الويب (Playwright)
    WEB_RENDERS = TEMP / "web_renders"
    TEMP_HTML = TEMP / "html_templates"

    # المخرجات
    VIDEOS = OUTPUT / "videos"
    THUMBNAILS = ASSETS / "thumbnails"

    # ملحقات العلامة التجارية
    FONTS = ASSETS / "fonts"
    OVERLAYS = ASSETS / "overlays"
    BRANDING = ASSETS / "branding"
    QURAN_AUDIO = ASSETS / "quran_audio"

    # Aliases للتوافق
    SCRIPT_DIR = TEMP_EPISODES
    QURAN_CACHE = QURAN_AUDIO

    # ملفات ثابتة
    LOGO_PRIMARY = ASSETS / "logo.png"
    AMIRI_FONT = FONTS / "Amiri-Bold.ttf"
    INTRO_VIDEO = BRANDING / "intro.mp4"
    OUTRO_VIDEO = BRANDING / "outro.mp4"
    JINGLE = OVERLAYS / "jingle.mp3"
    BGM = OVERLAYS / "bgm.mp3"
    BGM_FILE = BGM

    @classmethod
    def ensure_all(cls):
        """تأمين إنشاء كافة المجلدات لضمان عدم حدوث Directory Not Found."""
        dirs = [
            cls.LOGS, cls.TEMP, cls.ASSETS, cls.OUTPUT,
            cls.TEMP_EPISODES, cls.TTS_CACHE, cls.ASSEMBLY_DIR,
            cls.WEB_RENDERS, cls.TEMP_HTML,
            cls.VIDEOS, cls.THUMBNAILS,
            cls.FONTS, cls.OVERLAYS, cls.BRANDING, cls.QURAN_AUDIO,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════
# 3. إعدادات الفيديو (Cinematic Quality Settings)
# ════════════════════════════════════════════════════════════════
class VideoConfig:
    # الرندرة بجودة 1080p
    RESOLUTION_WIDTH = 1920
    RESOLUTION_HEIGHT = 1080

    # الحركية الفائقة (مهمة جداً لنعومة الـ CSS Animations والجسيمات)
    FPS = 60

    # إعدادات الترميز (Codec) — احترافي
    CODEC = "libx264"
    PROFILE = "high"
    CRF = 17        # جودة بصرية فائقة (نطاق 17-18 هو الاحترافي)
    PRESET = "slow"  # بطيء لضمان أفضل ضغط ونقاء للصورة
    PIX_FMT = "yuv420p"

    # إعدادات الصوت في الفيديو
    AUDIO_CODEC = "aac"
    AUDIO_BITRATE = "256k"


# ════════════════════════════════════════════════════════════════
# 4. إعدادات رندرة الويب (Web Render Config — Playwright)
# ════════════════════════════════════════════════════════════════
class WebRenderConfig:
    VIEWPORT_WIDTH = 1920
    VIEWPORT_HEIGHT = 1080
    RENDER_TIMEOUT_MS = 90000   # 90 ثانية كحد أقصى للمشهد الطويل
    BROWSER_TYPE = "chromium"   # الأفضل لدعم تأثيرات CSS المتقدمة


# ════════════════════════════════════════════════════════════════
# 5. إعدادات الصور التعليمية (Visual Config — Leonardo + Thumbnail)
# ════════════════════════════════════════════════════════════════
class VisualConfig:
    # أبعاد الصور
    WIDTH = 1920
    HEIGHT = 1080

    # خصائص الإخراج
    OUTPUT_QUALITY = 95   # جودة JPEG (1-100)
    BACKGROUND_COLOR = (245, 222, 179)   # wheat — هادئ للأطفال
    TEXT_COLOR = (34, 34, 34)            # رمادي داكن للقراءة المريحة

    # مسار الخط (يُستخدم في الـ thumbnail والـ fallback)
    FONT_PATH = str(Paths.AMIRI_FONT) if Paths.AMIRI_FONT.exists() else ""


# ════════════════════════════════════════════════════════════════
# 6. إعدادات هندسة الصوت (Audio Mastering & TTS)
# ════════════════════════════════════════════════════════════════
class AudioConfig:
    # Google Cloud TTS (fallback)
    DEFAULT_VOICE = "ar-XA-Wavenet-B"
    DEFAULT_SPEAKING_RATE = 0.95
    DEFAULT_PITCH = -1.0
    VOLUME_GAIN_DB = 0.0

    # ElevenLabs - Antoni (أب حنون، دافئ)
    ELEVENLABS_MODEL = "eleven_multilingual_v2"
    ELEVENLABS_VOICE_ID_ANTONI = "ErXwobaYiN019PkySvjV"
    ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or ELEVENLABS_VOICE_ID_ANTONI

    # وزن نبرة الصوت (ضبط دقيق للسرد القصصي)
    ELEVENLABS_STABILITY = 0.60
    ELEVENLABS_SIMILARITY = 0.85
    ELEVENLABS_STYLE = 0.35
    ELEVENLABS_SPEAKER_BOOST = True

    # Aliases للتوافق مع الكود الجديد
    STABILITY = ELEVENLABS_STABILITY
    SIMILARITY = ELEVENLABS_SIMILARITY
    STYLE_EXAGGERATION = ELEVENLABS_STYLE

    # معايير تطبيع الصوت (Youtube/Streaming Standard)
    TARGET_LUFS = -16.0
    TRUE_PEAK = -1.5


# ════════════════════════════════════════════════════════════════
# 7. إعدادات المؤثرات الصوتية (SFX)
# ════════════════════════════════════════════════════════════════
class SFXConfig:
    FADE_IN_DURATION = 0.5
    FADE_OUT_DURATION = 0.5
    QURAN_START_DELAY_MS = 500
    QURAN_FADE_IN_SEC = 1.0
    QURAN_END_PADDING_SEC = 1.0


# ════════════════════════════════════════════════════════════════
# 8. الهوية البصرية (Branding Config) — يستخدمها intro_outro_engine
# ════════════════════════════════════════════════════════════════
class BrandingConfig:
    # أسماء القناة
    CHANNEL_NAME_AR = "قِيمَة"
    CHANNEL_NAME_EN = "VALUE"
    CHANNEL_TAGLINE_AR = "قصص تربوية من نور القرآن"
    SUBSCRIBE_TEXT = "اشترك في القناة"

    # مدد الانترو والأوترو (بالثواني)
    INTRO_DURATION = 5.0
    OUTRO_DURATION = 5.0

    # الألوان السينمائية (تستخدم في FFmpeg drawtext + رندرة الويب)
    BG_COLOR = "0xFFFAF0"            # كريمي دافئ
    PRIMARY_COLOR = "0x0A1628"       # كحلي عميق
    SECONDARY_COLOR = "0x1E3A5F"     # كحلي متوسط
    ACCENT_COLOR = "0xD4AF37"        # ذهبي (الهوية)

    # ألوان CSS (للـ HTML render)
    COLOR_GOLD = "#FFD700"
    COLOR_AMBER = "#F5A623"
    COLOR_DARK_BG = "rgba(15, 15, 15, 0.7)"

    # إعدادات رندرة المتصفح
    RENDER_TIMEOUT_MS = 90000


# ════════════════════════════════════════════════════════════════
# 9. المنهج التعليمي (Curriculum)
# ════════════════════════════════════════════════════════════════
CURRICULUM: Dict[int, Dict[str, object]] = {
    1:  {"surah": 1,   "name": "الفاتحة",   "start": 1, "end": 7},
    2:  {"surah": 114, "name": "الناس",     "start": 1, "end": 6},
    3:  {"surah": 113, "name": "الفلق",     "start": 1, "end": 5},
    4:  {"surah": 112, "name": "الإخلاص",   "start": 1, "end": 4},
    5:  {"surah": 111, "name": "المسد",     "start": 1, "end": 5},
    6:  {"surah": 110, "name": "النصر",     "start": 1, "end": 3},
    7:  {"surah": 109, "name": "الكافرون",  "start": 1, "end": 6},
    8:  {"surah": 108, "name": "الكوثر",    "start": 1, "end": 3},
    9:  {"surah": 107, "name": "الماعون",   "start": 1, "end": 7},
    10: {"surah": 106, "name": "قريش",      "start": 1, "end": 4},
    11: {"surah": 105, "name": "الفيل",     "start": 1, "end": 5},
    12: {"surah": 104, "name": "الهمزة",    "start": 1, "end": 9},
    13: {"surah": 103, "name": "العصر",     "start": 1, "end": 3},
    14: {"surah": 102, "name": "التكاثر",   "start": 1, "end": 8},
    15: {"surah": 101, "name": "القارعة",   "start": 1, "end": 11},
    16: {"surah": 100, "name": "العاديات",  "start": 1, "end": 11},
}


# ════════════════════════════════════════════════════════════════
# 10. إعدادات قاعدة البيانات (Supabase Tables)
# ════════════════════════════════════════════════════════════════
class DBConfig:
    TABLE_EPISODES = "episodes"
    TABLE_PIPELINE_STATE = "pipeline_state"
    COLUMN_ID = "id"
    COLUMN_STATUS = "status"