"""
config.py — VALUE / QEEMA v10.0 (Procedural Cinematic Edition)
================================================================
- Egyptian Voice: Haytham (UR972wNGq3zluze0LoIp) — storyteller
- Procedural Visuals: Three.js + SVG (مفيش API خارجي)
- Word-level animations sync with TTS
- Quran Multi-CDN fallback
"""

import os
from pathlib import Path

# ════════════════════════════════════════════════════════════════
# 1. مفاتيح API
# ════════════════════════════════════════════════════════════════
class APIKeys:
    GEMINI = os.getenv("GEMINI_API_KEY", "")
    GEMINI_1 = GEMINI
    GEMINI_2 = os.getenv("GEMINI_API_KEY_2", "")
    GEMINI_3 = os.getenv("GEMINI_API_KEY_3", "")
    GROQ = os.getenv("GROQ_API_KEY", "")
    COHERE = os.getenv("COHERE_API_KEY", "")
    ANTHROPIC = os.getenv("ANTHROPIC_API_KEY", "")
    ELEVENLABS = os.getenv("ELEVENLABS_API_KEY", "")
    LEONARDO = os.getenv("LEONARDO_API_KEY", "")
    LEONARDO_API_KEY = LEONARDO
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
    YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
    YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    @classmethod
    def validate(cls) -> list[str]:
        missing = []
        if not cls.SUPABASE_URL: missing.append("SUPABASE_URL")
        if not cls.SUPABASE_KEY: missing.append("SUPABASE_KEY")
        if not cls.GEMINI and not cls.GROQ: missing.append("LLM (Gemini/Groq)")
        if not cls.ELEVENLABS and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            missing.append("ELEVENLABS_API_KEY أو GCP_SA_KEY")
        return missing


# ════════════════════════════════════════════════════════════════
# 2. المسارات
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
    WEB_RENDERS = TEMP / "web_renders"
    TEMP_HTML = TEMP / "html_templates"

    VIDEOS = OUTPUT / "videos"
    THUMBNAILS = ASSETS / "thumbnails"
    FONTS = ASSETS / "fonts"
    OVERLAYS = ASSETS / "overlays"
    BRANDING = ASSETS / "branding"
    QURAN_AUDIO = ASSETS / "quran_audio"
    QURAN_CACHE = QURAN_AUDIO

    SCRIPT_DIR = TEMP_EPISODES
    LOGO_PRIMARY = ASSETS / "logo.png"
    AMIRI_FONT = FONTS / "Amiri-Bold.ttf"
    INTRO_VIDEO = BRANDING / "intro.mp4"
    OUTRO_VIDEO = BRANDING / "outro.mp4"
    JINGLE = OVERLAYS / "jingle.mp3"
    BGM = OVERLAYS / "bgm.mp3"
    BGM_FILE = BGM

    @classmethod
    def ensure_all(cls):
        for d in [cls.LOGS, cls.TEMP, cls.ASSETS, cls.OUTPUT,
                  cls.TEMP_EPISODES, cls.TTS_CACHE, cls.ASSEMBLY_DIR,
                  cls.WEB_RENDERS, cls.TEMP_HTML, cls.VIDEOS,
                  cls.THUMBNAILS, cls.FONTS, cls.OVERLAYS,
                  cls.BRANDING, cls.QURAN_AUDIO]:
            d.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════
# 3. الفيديو (Cinematic 60FPS)
# ════════════════════════════════════════════════════════════════
class VideoConfig:
    RESOLUTION_WIDTH = 1920
    RESOLUTION_HEIGHT = 1080
    FPS = 60
    CODEC = "libx264"
    PROFILE = "high"
    CRF = 17
    PRESET = "slow"
    PIX_FMT = "yuv420p"
    AUDIO_CODEC = "aac"
    AUDIO_BITRATE = "256k"


class WebRenderConfig:
    VIEWPORT_WIDTH = 1920
    VIEWPORT_HEIGHT = 1080
    RENDER_TIMEOUT_MS = 120000
    BROWSER_TYPE = "chromium"


class VisualConfig:
    WIDTH = 1920
    HEIGHT = 1080
    OUTPUT_QUALITY = 95
    BACKGROUND_COLOR = (245, 222, 179)
    TEXT_COLOR = (34, 34, 34)
    FONT_PATH = str(Paths.AMIRI_FONT) if Paths.AMIRI_FONT.exists() else ""


# ════════════════════════════════════════════════════════════════
# 4. الصوت — Egyptian Storyteller
# ════════════════════════════════════════════════════════════════
class AudioConfig:
    # Google fallback
    DEFAULT_VOICE = "ar-XA-Wavenet-B"
    DEFAULT_SPEAKING_RATE = 0.95
    DEFAULT_PITCH = -1.0
    VOLUME_GAIN_DB = 0.0

    # ✅ NEW v10: Haytham - Egyptian storyteller
    ELEVENLABS_VOICE_ID_HAYTHAM = "UR972wNGq3zluze0LoIp"

    ELEVENLABS_MODEL = "eleven_multilingual_v2"
    ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or ELEVENLABS_VOICE_ID_HAYTHAM

    # ضبط للسرد القصصي للأطفال
    ELEVENLABS_STABILITY = 0.50
    ELEVENLABS_SIMILARITY = 0.85
    ELEVENLABS_STYLE = 0.50
    ELEVENLABS_SPEAKER_BOOST = True

    STABILITY = ELEVENLABS_STABILITY
    SIMILARITY = ELEVENLABS_SIMILARITY
    STYLE_EXAGGERATION = ELEVENLABS_STYLE

    TARGET_LUFS = -16.0
    TRUE_PEAK = -1.5


class SFXConfig:
    FADE_IN_DURATION = 0.4
    FADE_OUT_DURATION = 0.4
    QURAN_START_DELAY_MS = 600
    QURAN_FADE_IN_SEC = 1.0
    QURAN_END_PADDING_SEC = 1.5


# ════════════════════════════════════════════════════════════════
# 5. الهوية البصرية
# ════════════════════════════════════════════════════════════════
class BrandingConfig:
    CHANNEL_NAME_AR = "قِيمَة"
    CHANNEL_NAME_EN = "VALUE"
    CHANNEL_TAGLINE_AR = "قصص تربوية من نور القرآن"
    SUBSCRIBE_TEXT = "اشترك في القناة"

    INTRO_DURATION = 5.0
    OUTRO_DURATION = 5.0

    BG_COLOR = "0xFFFAF0"
    PRIMARY_COLOR = "0x0A1628"
    SECONDARY_COLOR = "0x1E3A5F"
    ACCENT_COLOR = "0xD4AF37"

    COLOR_GOLD = "#FFD700"
    COLOR_AMBER = "#F5A623"
    COLOR_DARK_BG = "rgba(15, 15, 15, 0.7)"

    RENDER_TIMEOUT_MS = 90000


# ════════════════════════════════════════════════════════════════
# 6. إعدادات المحرك الإجرائي (Procedural Engine)
# ════════════════════════════════════════════════════════════════
class ProceduralConfig:
    SCENE_TYPES = [
        "garden", "sky", "house", "mosque", "ocean", 
        "desert", "mountains", "child_praying", "family", "abstract_warm"
    ]

    PALETTES = {
        "warm_sunset": ["#FFB347", "#FFCC70", "#FFE5B4", "#FF6B6B", "#FFA07A"],
        "calm_blue":   ["#7FB3D5", "#A9CCE3", "#D4E6F1", "#85C1E2", "#5DADE2"],
        "lush_green":  ["#52BE80", "#82E0AA", "#ABEBC6", "#239B56", "#7DCEA0"],
        "night_stars": ["#1B2631", "#283747", "#34495E", "#FFD700", "#F5B041"],
        "golden_hour": ["#D4AF37", "#F39C12", "#F5B041", "#FAD7A0", "#F8C471"],
    }

    PARTICLE_COUNT = 80
    KEN_BURNS_SPEED = 0.05
    WORD_TRANSITION_ENABLED = True


# ════════════════════════════════════════════════════════════════
# 7. المنهج (جزء عم كاملاً)
# ════════════════════════════════════════════════════════════════
CURRICULUM: dict[int, dict[str, object]] = {
    1:  {"surah": 1,   "name": "الفاتحة",  "start": 1, "end": 7},
    2:  {"surah": 114, "name": "الناس",    "start": 1, "end": 6},
    3:  {"surah": 113, "name": "الفلق",    "start": 1, "end": 5},
    4:  {"surah": 112, "name": "الإخلاص",  "start": 1, "end": 4},
    5:  {"surah": 111, "name": "المسد",    "start": 1, "end": 5},
    6:  {"surah": 110, "name": "النصر",    "start": 1, "end": 3},
    7:  {"surah": 109, "name": "الكافرون", "start": 1, "end": 6},
    8:  {"surah": 108, "name": "الكوثر",   "start": 1, "end": 3},
    9:  {"surah": 107, "name": "الماعون",  "start": 1, "end": 7},
    10: {"surah": 106, "name": "قريش",     "start": 1, "end": 4},
    11: {"surah": 105, "name": "الفيل",    "start": 1, "end": 5},
    12: {"surah": 104, "name": "الهمزة",   "start": 1, "end": 9},
    13: {"surah": 103, "name": "العصر",    "start": 1, "end": 3},
    14: {"surah": 102, "name": "التكاثر",  "start": 1, "end": 8},
    15: {"surah": 101, "name": "القارعة",  "start": 1, "end": 11},
    16: {"surah": 100, "name": "العاديات", "start": 1, "end": 11},
    17: {"surah": 99,  "name": "الزلزلة",  "start": 1, "end": 8},
    18: {"surah": 98,  "name": "البينة",   "start": 1, "end": 8},
    19: {"surah": 97,  "name": "القدر",    "start": 1, "end": 5},
    20: {"surah": 96,  "name": "العلق",    "start": 1, "end": 19},
    21: {"surah": 95,  "name": "التين",    "start": 1, "end": 8},
    22: {"surah": 94,  "name": "الشرح",    "start": 1, "end": 8},
    23: {"surah": 93,  "name": "الضحى",    "start": 1, "end": 11},
    24: {"surah": 92,  "name": "الليل",    "start": 1, "end": 21},
    25: {"surah": 91,  "name": "الشمس",    "start": 1, "end": 15},
    26: {"surah": 90,  "name": "البلد",    "start": 1, "end": 20},
    27: {"surah": 89,  "name": "الفجر",    "start": 1, "end": 30},
    28: {"surah": 88,  "name": "الغاشية",  "start": 1, "end": 26},
    29: {"surah": 87,  "name": "الأعلى",   "start": 1, "end": 19},
    30: {"surah": 86,  "name": "الطارق",   "start": 1, "end": 17},
    31: {"surah": 85,  "name": "البروج",   "start": 1, "end": 22},
    32: {"surah": 84,  "name": "الانشقاق", "start": 1, "end": 25},
    33: {"surah": 83,  "name": "المطففين", "start": 1, "end": 36},
    34: {"surah": 82,  "name": "الانفطار", "start": 1, "end": 19},
    35: {"surah": 81,  "name": "التكوير",  "start": 1, "end": 29},
    36: {"surah": 80,  "name": "عبس",      "start": 1, "end": 42},
    37: {"surah": 79,  "name": "النازعات", "start": 1, "end": 46},
    38: {"surah": 78,  "name": "النبأ",    "start": 1, "end": 40},
}


# ════════════════════════════════════════════════════════════════
# 8. قاعدة البيانات
# ════════════════════════════════════════════════════════════════
class DBConfig:
    TABLE_EPISODES = "episodes"
    TABLE_PIPELINE_STATE = "pipeline_state"
    COLUMN_ID = "id"
    COLUMN_STATUS = "status"
