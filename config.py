"""
config.py — VALUE / QEEMA v2
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

    @classmethod
    def validate(cls) -> list[str]:
        required = {
            "GEMINI_API_KEY":          cls.GEMINI,
            "LEONARDO_API_KEY":        cls.LEONARDO,
            "SUPABASE_URL":            cls.SUPABASE_URL,
            "SUPABASE_KEY":            cls.SUPABASE_KEY,
            "YOUTUBE_CLIENT_ID":       cls.YT_CLIENT_ID,
            "YOUTUBE_CLIENT_SECRET":   cls.YT_CLIENT_SECRET,
            "YOUTUBE_REFRESH_TOKEN":   cls.YT_REFRESH_TOKEN,
        }
        return [k for k, v in required.items() if not v]


class Paths:
    ROOT       = Path(__file__).parent
    ASSETS     = ROOT / "assets"
    FONTS      = ASSETS / "fonts"
    MUSIC      = ASSETS / "music"
    SFX        = ASSETS / "sfx"
    OVERLAYS   = ASSETS / "overlays"
    THUMBNAILS = ASSETS / "thumbnails"
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
    CRF      = 16
    PRESET   = "slow"
    PIX_FMT  = "yuv420p"
    AUDIO_CODEC   = "aac"
    AUDIO_BITRATE = "256k"
    AUDIO_RATE    = 48000
    DEFAULT_DURATION = 300
    CROSSFADE_DURATION = 0.6
    FADE_IN_DURATION   = 0.8
    FADE_OUT_DURATION  = 1.2


class VoiceConfig:
    MODEL = "gemini-2.5-flash-preview-tts"
    NARRATOR_VOICE = "Charon"
    CHILD_VOICE = "Puck"
    PCM_SAMPLE_RATE = 24000
    PCM_CHANNELS    = 1
    PCM_BIT_DEPTH   = 16
    OUTPUT_SAMPLE_RATE = 48000
    OUTPUT_BITRATE     = "192k"
    QURAN_CDN_ALAFASY   = "https://everyayah.com/data/Alafasy_128kbps/{surah:03d}{ayah:03d}.mp3"
    QURAN_CDN_SUDAIS    = "https://everyayah.com/data/Abdurrahmaan_As-Sudais_192kbps/{surah:03d}{ayah:03d}.mp3"
    QURAN_CDN_HUSARY    = "https://everyayah.com/data/Husary_128kbps/{surah:03d}{ayah:03d}.mp3"
    QURAN_CDN_MINSHAWI  = "https://everyayah.com/data/Minshawy_Murattal_128kbps/{surah:03d}{ayah:03d}.mp3"
    DEFAULT_RECITER = "alafasy"


class SubtitleConfig:
    FONT_SIZE_LARGE  = 80
    FONT_SIZE_MEDIUM = 60
    FONT_SIZE_SMALL  = 42
    COLOR_AYAH      = "white"
    COLOR_NARRATOR  = "white"
    COLOR_HIGHLIGHT = "#FFD700"
    SHADOW_COLOR    = "black@0.9"
    SHADOW_OFFSET   = 3
    BORDER_WIDTH    = 4
    BORDER_COLOR    = "black@0.8"
    MARGIN_BOTTOM_H  = 120
    MARGIN_BOTTOM_V  = 200
    BOX_PADDING      = 20
    BOX_COLOR        = "black@0.55"
    BOX_BORDER_RADIUS = 12


class VisualConfig:
    MODEL_ANIME    = "e71a1c2f-4f80-4800-934f-2c68979d8cc8"
    MODEL_CREATIVE = "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3"
    MODEL_VISION   = "aa77f04e-3eec-4034-9c07-d0f619684628"
    WIDTH  = 1440
    HEIGHT = 1440
    GUIDANCE_SCALE = 7
    STEPS = 35
    NUM_IMAGES = 1
    STYLE_SUFFIX = (
        "children's illustrated Arabic Islamic style, "
        "warm soft colors, 2D flat art, cozy atmosphere, "
        "mosque geometric patterns, Arabic calligraphy accents, "
        "safe welcoming environment, child-friendly, "
        "professional high quality illustration"
    )
    NEGATIVE_PROMPT = (
        "realistic photo, violence, scary, darkness, "
        "distorted faces, ugly, low quality, watermark, "
        "western anime, inappropriate content"
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