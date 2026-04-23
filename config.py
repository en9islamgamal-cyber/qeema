"""
config.py — VALUE / QEEMA v2
الإعدادات المركزية المتقدمة (Advanced Settings)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


class APIKeys:
    GEMINI           = os.environ.get("GEMINI_API_KEY", "")
    LEONARDO         = os.environ.get("LEONARDO_API_KEY", "")
    SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
    SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
    GCP_SA_KEY_PATH  = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    YT_CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID", "")
    YT_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
    YT_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
    # تم إضافتها للمستقبل في حال الترقية لمحرك صوت بشري فائق
    ELEVENLABS       = os.environ.get("ELEVENLABS_API_KEY", "")

    @classmethod
    def validate(cls) -> list[str]:
        # نكتفي بفحص الأساسيات حتى لا يتوقف البايبلاين بسبب مفاتيح اليوتيوب إذا كان في وضع DRY_RUN
        required = {
            "GEMINI_API_KEY":          cls.GEMINI,
            "LEONARDO_API_KEY":        cls.LEONARDO,
            "SUPABASE_URL":            cls.SUPABASE_URL,
            "SUPABASE_KEY":            cls.SUPABASE_KEY,
        }
        return [k for k, v in required.items() if not v]


class Paths:
    # استخدام resolve() يضمن عدم حدوث أخطاء في مسارات الملفات مهما اختلف نظام التشغيل
    ROOT       = Path(__file__).parent.resolve()
    ASSETS     = ROOT / "assets"
    FONTS      = ASSETS / "fonts"
    MUSIC      = ASSETS / "music"
    SFX        = ASSETS / "sfx"
    OVERLAYS   = ASSETS / "overlays"
    THUMBNAILS = ASSETS / "thumbnails"
    
    # 👈 حل مشكلة اللوجو: تعريف مسار ثابت له
    LOGO_PRIMARY = ASSETS / "logo.png"
    WATERMARK    = ASSETS / "watermark.png"

    OUTPUT     = ROOT / "output"
    VIDEOS     = OUTPUT / "videos"
    SHORTS     = OUTPUT / "shorts"
    TEMP       = ROOT / "temp"
    EPISODES   = TEMP / "episodes"
    ASSEMBLY   = TEMP / "assembly"
    LOGS       = ROOT / "logs"
    SCRIPT_DIR = TEMP / "scripts"

    @classmethod
    def ensure_all(cls):
        for attr in vars(cls).values():
            if isinstance(attr, Path) and not attr.suffix:
                attr.mkdir(parents=True, exist_ok=True)


class VideoConfig:
    WIDTH_H  = 1920
    HEIGHT_H = 1080
    WIDTH_V  = 1080
    HEIGHT_V = 1920
    FPS      = 30
    CODEC    = "libx264"
    PROFILE  = "high"
    LEVEL    = "4.2"
    CRF      = 18        # توازن ممتاز بين حجم الملف وجودة اليوتيوب العالية
    PRESET   = "slow"
    PIX_FMT  = "yuv420p"
    
    AUDIO_CODEC   = "aac"
    AUDIO_BITRATE = "320k" # ترقية لجودة صوت سينمائية
    AUDIO_RATE    = 48000
    
    DEFAULT_DURATION = 300
    CROSSFADE_DURATION = 0.6
    FADE_IN_DURATION   = 0.8
    FADE_OUT_DURATION  = 1.2
    
    # 👈 إعدادات دمج اللوجو الجديدة
    LOGO_MARGIN_X = 40
    LOGO_MARGIN_Y = 40
    LOGO_OPACITY = 0.85


class VoiceConfig:
    # تم تنظيف الإعدادات القديمة لتعكس المحرك الاحترافي الحالي (GCP Wavenet)
    TTS_PROVIDER = "gcp"
    GCP_VOICE_NARRATOR = "ar-XA-Wavenet-B"
    GCP_PITCH = -2.0
    GCP_SPEED = 0.90

    PCM_SAMPLE_RATE = 24000
    OUTPUT_SAMPLE_RATE = 48000
    OUTPUT_BITRATE     = "320k"

    QURAN_CDN_ALAFASY   = "https://everyayah.com/data/Alafasy_128kbps/{surah:03d}{ayah:03d}.mp3"
    QURAN_CDN_SUDAIS    = "https://everyayah.com/data/Abdurrahmaan_As-Sudais_192kbps/{surah:03d}{ayah:03d}.mp3"
    QURAN_CDN_HUSARY    = "https://everyayah.com/data/Husary_128kbps/{surah:03d}{ayah:03d}.mp3"
    QURAN_CDN_MINSHAWI  = "https://everyayah.com/data/Minshawy_Murattal_128kbps/{surah:03d}{ayah:03d}.mp3"
    DEFAULT_RECITER = "alafasy"


class SubtitleConfig:
    # 👈 إضافة خطوط مخصصة إن وجدت، مع ألوان أكثر احترافية للطفل
    PRIMARY_FONT = "Cairo, Arial"
    QURAN_FONT   = "Amiri, Traditional Arabic"

    FONT_SIZE_LARGE  = 80
    FONT_SIZE_MEDIUM = 60
    FONT_SIZE_SMALL  = 42
    
    COLOR_AYAH      = "#FFFDE7" # أبيض دافئ ومريح للعين
    COLOR_NARRATOR  = "#FFFFFF"
    COLOR_HIGHLIGHT = "#FFC107" # لون ذهبي للكلمات المهمة
    COLOR_QURAN_GOLD= "#D4AF37" 

    SHADOW_COLOR    = "black@0.6" # ظل ناعم بدلاً من الظل الحاد القديم
    SHADOW_OFFSET   = 4
    BORDER_WIDTH    = 3
    BORDER_COLOR    = "#1A1A1A@0.8"

    MARGIN_BOTTOM_H  = 120
    MARGIN_BOTTOM_V  = 200
    BOX_PADDING      = 25
    BOX_COLOR        = "#000000@0.4" # صندوق ترجمة أنيق شبه شفاف
    BOX_BORDER_RADIUS = 16


class VisualConfig:
    MODEL_ANIME    = "e71a1c2f-4f80-4800-934f-2c68979d8cc8"
    MODEL_CREATIVE = "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3"
    MODEL_VISION   = "aa77f04e-3eec-4034-9c07-d0f619684628"
    
    WIDTH  = 1920 # 👈 تعديل للأبعاد القياسية لليوتيوب
    HEIGHT = 1080
    GUIDANCE_SCALE = 8 # زيادة التزام الموديل بوصف الصورة
    STEPS = 40         # زيادة دقة الصورة وجودتها
    NUM_IMAGES = 1
    
    # 👈 التغيير السحري هنا: توجيه صارم لنمط الإنفوجرافيك النظيف 
    STYLE_SUFFIX = (
        ", premium minimalist flat vector infographic, clean modern UI style, "
        "corporate Islamic education aesthetic, solid pastel background, "
        "warm cozy palette, precise geometric vectors, NO TEXT, NO LETTERS, "
        "high-end motion graphics asset, polished, 8k resolution"
    )
    # 👈 منع الأشياء البدائية والمشوهة بشكل قاطع
    NEGATIVE_PROMPT = (
        "text, letters, words, realistic photo, 3d render, messy, chaotic, "
        "scary, violence, darkness, distorted faces, ugly, low quality, "
        "watermark, signature, western anime, inappropriate content"
    )
    
    POLL_INTERVAL = 6
    MAX_POLLS     = 20


class ChannelConfig:
    NAME             = "قيمة | VALUE"
    LANGUAGE         = "ar"
    CATEGORY_ID      = "27"
    FOR_KIDS         = True
    DEFAULT_PRIVACY  = "public"
    BASE_TAGS = [
        "قرآن للأطفال", "حفظ القرآن", "تعليم قرآن",
        "أطفال", "تربية إسلامية", "قيمة", "VALUE",
        "Quran for kids", "Islamic education"
    ]


class DBConfig:
    TABLE_EPISODES = "episodes"
    TABLE_METRICS  = "episode_metrics"


CURRICULUM: dict[int, dict] = {
    1:  {"surah": 1,   "start": 1, "end": 7,  "name": "الفاتحة",    "juz": 1},
    2:  {"surah": 112, "start": 1, "end": 4,  "name": "الإخلاص",    "juz": 30},
    3:  {"surah": 113, "start": 1, "end": 5,  "name": "الفلق",      "juz": 30},
    4:  {"surah": 114, "start": 1, "end": 6,  "name": "الناس",      "juz": 30},
    5:  {"surah": 110, "start": 1, "end": 3,  "name": "النصر",      "juz": 30},
    6:  {"surah": 108, "start": 1, "end": 3,  "name": "الكوثر",     "juz": 30},
    7:  {"surah": 103, "start": 1, "end": 3,  "name": "العصر",      "juz": 30},
    8:  {"surah": 107, "start": 1, "end": 7,  "name": "الماعون",    "juz": 30},
    9:  {"surah": 106, "start": 1, "end": 4,  "name": "قريش",       "juz": 30},
    10: {"surah": 105, "start": 1, "end": 5,  "name": "الفيل",      "juz": 30},
    11: {"surah": 104, "start": 1, "end": 9,  "name": "الهمزة",     "juz": 30},
    12: {"surah": 102, "start": 1, "end": 8,  "name": "التكاثر",    "juz": 30},
    13: {"surah": 101, "start": 1, "end": 11, "name": "القارعة",    "juz": 30},
    14: {"surah": 99,  "start": 1, "end": 8,  "name": "الزلزلة",    "juz": 30},
    15: {"surah": 98,  "start": 1, "end": 8,  "name": "البيّنة",    "juz": 30},
    16: {"surah": 97,  "start": 1, "end": 5,  "name": "القدر",      "juz": 30},
    17: {"surah": 96,  "start": 1, "end": 19, "name": "العلق",      "juz": 30},
    18: {"surah": 95,  "start": 1, "end": 8,  "name": "التين",      "juz": 30},
    19: {"surah": 94,  "start": 1, "end": 8,  "name": "الشرح",      "juz": 30},
    20: {"surah": 93,  "start": 1, "end": 11, "name": "الضحى",      "juz": 30},
}
