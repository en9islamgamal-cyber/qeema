"""
voice_engine.py — VALUE / QEEMA v5.0
======================================
محرك الصوت الاحترافي:
  - ElevenLabs (eleven_multilingual_v2) كصوت بشري جودة عالية للسرد
  - Google Cloud TTS كـ fallback آمن
  - تلاوة قرآنية حقيقية من قارئ معتمد (مشاري العفاسي) للآيات
  - معالجة احترافية للتشكيل (إزالة من نهايات الكلمات)
"""

import os
import re
import logging
import hashlib
import requests
from pathlib import Path
from typing import Optional, Dict, Any
from tenacity import retry, stop_after_attempt, wait_exponential

from config import AudioConfig, Paths

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# نص العربي: تطبيع ومعالجة التشكيل
# ═══════════════════════════════════════════════════════════════
# إزالة التشكيل من نهايات الكلمات فقط (سبب الصوت الروبوتي):
# الفتحة، الضمة، الكسرة، التنوين بأنواعه — قبل مسافة أو علامة ترقيم
TASHKEEL_END_RE = re.compile(r"[\u064B-\u0650]+(?=\s|$|[،.؟!:])")
# الشدة + سكون نسيبهم (مهمين لنطق صحيح في وسط الكلمة)
NORMALIZE_SPACE = re.compile(r"\s+")


def prepare_arabic_for_tts(text: str) -> str:
    """تنظيف النص العربي قبل إرساله للـ TTS — يقلل الصوت الروبوتي بشكل ملحوظ."""
    if not text:
        return text
    # 1) إزالة تشكيل نهايات الكلمات (السبب الأكبر للروبوتية)
    text = TASHKEEL_END_RE.sub("", text)
    # 2) توحيد المسافات
    text = NORMALIZE_SPACE.sub(" ", text)
    # 3) استبدال علامات الترقيم العربية بمكافئاتها (ElevenLabs أحسن مع الإنجليزية)
    text = text.replace("،", ",").replace("؟", "?").replace("؛", ";")
    return text.strip()


def text_hash(text: str, voice: str = "") -> str:
    """Hash للـ caching."""
    return hashlib.md5(f"{voice}:{text}".encode("utf-8")).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════
# ElevenLabs Adapter (الأصل: صوت بشري جودة استوديو)
# ═══════════════════════════════════════════════════════════════
class ElevenLabsAdapter:
    """ElevenLabs Multilingual v2 — أعلى جودة عربي بشري."""

    BASE_URL = "https://api.elevenlabs.io/v1"

    # أصوات جيدة للعربي في eleven_multilingual_v2:
    # "pNInz6obpgDQGcFmaJgB" Adam (رجالي عميق) - مناسب لجد القناة
    # "TxGEqnHWrfWFTfGW9XjX" Josh (شبابي دافئ)
    # "AZnzlk1XvdvUeBnXmlld" Domi (أنثى دافئة)
    DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"

    def __init__(self, api_key: str, voice_id: Optional[str] = None):
        self.api_key = api_key
        self.voice_id = voice_id or self.DEFAULT_VOICE_ID
        self.headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def synthesize(self, text: str, output_path: str) -> bool:
        url = f"{self.BASE_URL}/text-to-speech/{self.voice_id}"
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,            # توازن بين تنوع وثبات
                "similarity_boost": 0.85,    # قرب من الصوت الأصلي
                "style": 0.45,               # تعبير عاطفي معتدل (مناسب للأطفال)
                "use_speaker_boost": True,
            },
        }
        resp = requests.post(url, headers=self.headers, json=payload, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:200]}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(resp.content)
        return True


# ═══════════════════════════════════════════════════════════════
# Google Cloud TTS Adapter (Fallback)
# ═══════════════════════════════════════════════════════════════
class GoogleTTSAdapter:
    def __init__(self):
        from google.cloud import texttospeech
        self.tts = texttospeech
        self.client = texttospeech.TextToSpeechClient()
        # Chirp3-HD أحسن من Wavenet في العربي. لو مش متاح، نسقط لـ Wavenet
        self.voice = texttospeech.VoiceSelectionParams(
            language_code="ar-XA",
            name=AudioConfig.DEFAULT_VOICE,
        )
        self.audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=AudioConfig.DEFAULT_SPEAKING_RATE,
            pitch=AudioConfig.DEFAULT_PITCH,
            volume_gain_db=AudioConfig.VOLUME_GAIN_DB,
        )

    def _ssml(self, text: str) -> str:
        """SSML مع breaks طبيعية."""
        text = text.replace(",", '<break time="300ms"/>')
        text = text.replace(".", '<break time="700ms"/>')
        text = text.replace("?", '<break time="700ms"/>')
        return f"<speak>{text}</speak>"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    def synthesize(self, text: str, output_path: str) -> bool:
        ssml = self._ssml(text)
        synthesis_input = self.tts.SynthesisInput(ssml=ssml)
        response = self.client.synthesize_speech(
            input=synthesis_input,
            voice=self.voice,
            audio_config=self.audio_config,
        )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(response.audio_content)
        return True


# ═══════════════════════════════════════════════════════════════
# Quran Reciter — تلاوة بشرية حقيقية (لا TTS!)
# ═══════════════════════════════════════════════════════════════
class QuranReciter:
    """
    تنزيل تلاوة الآيات من قارئ بشري معتمد (مشاري العفاسي).
    يستخدم API: everyayah.com
    تنسيق الرابط: https://everyayah.com/data/{reciter}/{surah:03d}{ayah:03d}.mp3
    """
    DEFAULT_RECITER = "Alafasy_128kbps"  # مشاري العفاسي — صوت محبب للأطفال

    def __init__(self, reciter: str = None):
        self.reciter = reciter or self.DEFAULT_RECITER
        self.base = f"https://everyayah.com/data/{self.reciter}"

    def get_url(self, surah: int, ayah: int) -> str:
        return f"{self.base}/{surah:03d}{ayah:03d}.mp3"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def fetch(self, surah: int, ayah: int, output_path: str) -> bool:
        url = self.get_url(surah, ayah)
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Quran audio fetch failed: {resp.status_code} for {url}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(resp.content)
        logger.info(f"📿 تم تنزيل تلاوة سورة {surah} آية {ayah}")
        return True


# ═══════════════════════════════════════════════════════════════
# VoiceEngine — الموحّد
# ═══════════════════════════════════════════════════════════════
class VoiceEngine:
    """محرك الصوت الموحد: ElevenLabs primary + Google fallback + Quran Reciter."""

    def __init__(self):
        self.adapters = []

        # 1) ElevenLabs — الأولوية القصوى (صوت بشري)
        eleven_key = os.getenv("ELEVENLABS_API_KEY", "")
        if eleven_key:
            try:
                voice_id = os.getenv("ELEVENLABS_VOICE_ID", "")
                self.adapters.append(("elevenlabs", ElevenLabsAdapter(eleven_key, voice_id or None)))
                logger.info("✅ ElevenLabs adapter ready (primary)")
            except Exception as e:
                logger.warning(f"⚠️ ElevenLabs init failed: {e}")

        # 2) Google Cloud TTS — Fallback
        try:
            self.adapters.append(("google", GoogleTTSAdapter()))
            logger.info("✅ Google Cloud TTS adapter ready (fallback)")
        except Exception as e:
            logger.warning(f"⚠️ Google TTS init failed: {e}")

        if not self.adapters:
            raise RuntimeError("❌ لا يوجد أي محرك TTS متاح! أضف ELEVENLABS_API_KEY أو GCP_SA_KEY.")

        # 3) Quran Reciter
        self.reciter = QuranReciter()

        # Cache directory
        Paths.TTS_CACHE.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str, output_path: str, use_cache: bool = True) -> bool:
        """توليد صوت لنص — يجرب ElevenLabs أولًا ثم Google."""
        if not text or not text.strip():
            logger.warning("⚠️ نص فارغ، تخطي.")
            return False

        clean = prepare_arabic_for_tts(text)

        # Cache check
        if use_cache:
            cache_key = text_hash(clean, self.adapters[0][0])
            cache_file = Paths.TTS_CACHE / f"{cache_key}.mp3"
            if cache_file.exists():
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(cache_file.read_bytes())
                logger.info(f"♻️ Cache hit: {cache_file.name}")
                return True

        # Try adapters in order
        last_err = None
        for name, adapter in self.adapters:
            try:
                logger.info(f"🎙️ TTS via {name}: {clean[:50]}...")
                ok = adapter.synthesize(clean, output_path)
                if ok and Path(output_path).exists():
                    # Save to cache
                    if use_cache:
                        cache_key = text_hash(clean, self.adapters[0][0])
                        cache_file = Paths.TTS_CACHE / f"{cache_key}.mp3"
                        cache_file.write_bytes(Path(output_path).read_bytes())
                    return True
            except Exception as e:
                last_err = e
                logger.warning(f"⚠️ {name} failed: {e}")
                continue

        logger.error(f"❌ كل محركات TTS فشلت. آخر خطأ: {last_err}")
        return False

    def fetch_quran_audio(self, surah: int, ayah: int, output_path: str) -> bool:
        """تنزيل تلاوة بشرية للآية."""
        try:
            return self.reciter.fetch(surah, ayah, output_path)
        except Exception as e:
            logger.error(f"❌ فشل تنزيل تلاوة {surah}:{ayah}: {e}")
            return False

    def generate_episode_audio(self, script, ep_dir: str) -> Dict[str, str]:
        """إنتاج كل صوتيات الحلقة."""
        audio_map: Dict[str, str] = {}
        ep_path = Path(ep_dir)
        ep_path.mkdir(parents=True, exist_ok=True)

        # Intro
        p = str(ep_path / "intro_narrator.mp3")
        if self.synthesize(script.intro_scene.narrator_text, p):
            audio_map["intro"] = p

        # Ayah scenes (3 audio files each: intro, ayah recitation, explain)
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"

            # 1) Intro to ayah
            p_intro = str(ep_path / f"{sid}_intro.mp3")
            if self.synthesize(scene.intro_text, p_intro):
                audio_map[f"{sid}_intro"] = p_intro

            # 2) Real Quran recitation (NOT TTS!)
            p_ayah = str(ep_path / f"{sid}_recitation.mp3")
            if self.fetch_quran_audio(scene.ayah.surah, scene.ayah.number, p_ayah):
                audio_map[f"{sid}_ayah"] = p_ayah

            # 3) Explanation
            p_explain = str(ep_path / f"{sid}_explain.mp3")
            if self.synthesize(scene.explain_text, p_explain):
                audio_map[f"{sid}_explain"] = p_explain

        # Mid scenes
        for sc in script.mid_scenes:
            p = str(ep_path / f"mid_{sc.scene_id}.mp3")
            if self.synthesize(sc.narrator_text, p):
                audio_map[f"mid_{sc.scene_id}"] = p

        # Outro
        p = str(ep_path / "outro_narrator.mp3")
        if self.synthesize(script.outro_scene.narrator_text, p):
            audio_map["outro"] = p

        logger.info(f"✅ تم إنتاج {len(audio_map)} ملف صوتي")
        return audio_map
