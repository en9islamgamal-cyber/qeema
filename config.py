"""
config.py — VALUE / QEEMA v2
مركز الإعدادات الكاملة للمنظومة
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


# ═══════════════════════════════════════════════════════
# API KEYS — تُقرأ من متغيرات البيئة / GitHub Secrets
# ═══════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════
# VIDEO — إعدادات الجودة
# ═══════════════════════════════════════════════════════
class VideoConfig:
    # Full HD Horizontal (YouTube full video)
    WIDTH_H  = 1920
    HEIGHT_H = 1080

    # Full HD Vertical (YouTube Shorts + Reels)
    WIDTH_V  = 1080
    HEIGHT_V = 1920

    FPS      = 30
    CODEC    = "libx264"
    PROFILE  = "high"
    LEVEL    = "4.2"
    CRF      = 16          # جودة عالية جداً (أقل = أفضل)
    PRESET   = "slow"      # ضغط أفضل مع جودة أعلى
    PIX_FMT  = "yuv420p"

    AUDIO_CODEC   = "aac"
    AUDIO_BITRATE = "256k"
    AUDIO_RATE    = 48000

    # مدة الفيديو الافتراضية
    DEFAULT_DURATION = 300   # 5 دقائق

    # انتقالات
    CROSSFADE_DURATION = 0.6
    FADE_IN_DURATION   = 0.8
    FADE_OUT_DURATION  = 1.2


# ═══════════════════════════════════════════════════════
# VOICE — Google Gen AI (Gemini TTS)
# ═══════════════════════════════════════════════════════
class VoiceConfig:
    # ✅ تم التحديث: gemini-2.0-flash-exp → gemini-2.5-flash-preview-tts
    # بديل: "gemini-2.5-pro-preview-tts" لجودة أعلى (أغلى قليلاً)
    MODEL = "gemini-2.5-flash-preview-tts"

    # صوت جدو أبو زياد (راوي عميق دافئ)
    NARRATOR_VOICE = "Charon"     # ذكر، عميق،