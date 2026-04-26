"""
config.py — VALUE / QEEMA v6.0 (Cinematic Web-Rendering Edition)
================================================================
يحتوي على إعدادات محرك الرندرة الجديد وتقنيات الـ 60fps.
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
    ELEVENLABS = os.getenv("ELEVENLABS_API_KEY", "")
    YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
    YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    @classmethod
    def validate(cls) -> List[str]:
        missing = []
        if not cls.SUPABASE_URL: missing.append("SUPABASE_URL")
        if not cls.SUPABASE_KEY: missing.append("SUPABASE_KEY")
        if not cls.ELEVENLABS and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            missing.append("ELEVENLABS_API_KEY أو GCP_SA_KEY")
        return missing


# ════════════════════════════════════════════════════════════════
# مسارات المجلدات والملفات (تحديث v6 لدعم الرندرة)
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
    
    # ✅ NEW v6: مسارات محرك رندرة الويب (Playwright)
    WEB_RENDERS = TEMP / "web_renders"
    TEMP_HTML = TEMP / "html_templates"

    VIDEOS = OUTPUT / "videos"
    THUMBNAILS = ASSETS / "thumbnails"
    FONTS = ASSETS / "fonts"
    OVERLAYS = ASSETS / "overlays"

    SCRIPT_DIR = TEMP_EPISODES
    LOGO_PRIMARY = ASSETS / "logo.png"

    BRANDING = ASSETS / "branding"
    INTRO_VIDEO = BRANDING / "intro.mp4"
    OUTRO_VIDEO = BRANDING / "outro.mp4"
    JINGLE = OVERLAYS / "jingle.mp3"
    BGM = OVERLAYS / "bgm.mp3"
    QURAN_CACHE = ASSETS / "quran_audio"

    @classmethod
    def ensure_all(cls):
        dirs = [
            cls.LOGS, cls.TEMP, cls.TEMP_EPISODES, cls.TTS_CACHE,
            cls.ASSEMBLY_DIR, cls.VIDEOS, cls.THUMBNAILS,
            cls.FONTS, cls.OVERLAYS, cls.SCRIPT_DIR,
            cls.BRANDING, cls.QURAN_CACHE,
            cls.WEB_RENDERS, cls.TEMP_HTML, # تأمين المجلدات الجديدة
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════
# إعدادات الفيديو (World-Class Quality)
# ════════════════════════════════════════════════════════════════
class VideoConfig:
    CODEC = "libx264"
    PROFILE = "high"
    CRF = 17        # تقليل القيمة يعني جودة أعلى (المدى الاحترافي 17-18)
    PIX_FMT = "yuv420p"
    PRESET = "slow"  # بطيء لضمان أفضل ضغط ونقاء للصورة
    AUDIO_CODEC = "aac"
    AUDIO_BITRATE = "256k" # رفع جودة الصوت للوضوح الفائق
    FPS = 60               # 60 إطار لضمان نعومة حركات الـ CSS مثل Meta AI
    RESOLUTION_WIDTH = 1920
    RESOLUTION_HEIGHT = 1080


# ════════════════════════════════════════════════════════════════
# ✅ NEW v6: إعدادات رندرة الويب (Web Render Config)
# ════════════════════════════════════════════════════════════════
class WebRenderConfig:
    VIEWPORT_WIDTH = 1920
    VIEWPORT_HEIGHT = 1080
    RENDER_TIMEOUT_MS = 60000  # دقيقة واحدة كحد أقصى لكل مشهد
    BROWSER_TYPE = "chromium"  # الأفضل لدعم تأثيرات CSS المتقدمة


# ════════════════════════════════════════════════════════════════
# إعدادات الصوت (Storytelling Settings)
# ════════════════════════════════════════════════════════════════
class AudioConfig:
    DEFAULT_VOICE = "ar-XA-Wavenet-B"
    DEFAULT_SPEAKING_RATE = 0.95
    DEFAULT_PITCH = -1.0
    VOLUME_GAIN_DB = 0.0

    ELEVENLABS_MODEL = "eleven_multilingual_v2"
    ELEVENLABS_VOICE_ID_ANTONI = "ErXwobaYiN019PkySvjV"
    ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID") or ELEVENLABS_VOICE_ID_ANTONI

    ELEVENLABS_STABILITY = 0.60    # زيادة الثبات لصوت أكثر رصانة
    ELEVENLABS_SIMILARITY = 0.85
    ELEVENLABS_STYLE = 0.35
    ELEVENLABS_SPEAKER_BOOST = True


# (باقي الملف - SFXConfig, BrandingConfig, DBConfig, CURRICULUM يظل كما هو)
# ... [نفس كود النسخة السابقة] ...
