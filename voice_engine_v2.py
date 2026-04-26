"""
voice_engine_v2.py — VALUE / QEEMA v10.0 (Egyptian Voice + Multi-CDN Quran)
=============================================================================
- ElevenLabs primary: Haytham (Egyptian storyteller) UR972wNGq3zluze0LoIp
- Multi-CDN fallback للتلاوة (3 مصادر)
- Cache نشط للصوتيات (يوفّر credits)
"""

import os
import re
import logging
import hashlib
import requests
from pathlib import Path
from typing import Optional, Dict
from tenacity import retry, stop_after_attempt, wait_exponential

from config import AudioConfig, Paths

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Arabic prep — تطبيع لتقليل الـ artifacts في TTS
# ════════════════════════════════════════════════════════════════
TASHKEEL_END_RE = re.compile(r"[\u064B-\u0650]+(?=\s|$|[،.؟!:])")
NORMALIZE_SPACE = re.compile(r"\s+")


def prepare_arabic_for_tts(text: str) -> str:
    if not text: return text
    text = TASHKEEL_END_RE.sub("", text)
    text = NORMALIZE_SPACE.sub(" ", text)
    text = text.replace("،", ",").replace("؟", "?").replace("؛", ";")
    return text.strip()


def text_hash(text: str, voice: str = "") -> str:
    return hashlib.md5(f"{voice}:{text}".encode("utf-8")).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════
# ElevenLabs Adapter (Haytham — Egyptian)
# ════════════════════════════════════════════════════════════════
class ElevenLabsAdapter:
    BASE_URL = "https://api.elevenlabs.io/v1"

    def __init__(self, api_key: str, voice_id: Optional[str] = None):
        self.api_key = api_key
        self.voice_id = voice_id or AudioConfig.ELEVENLABS_VOICE_ID
        self.headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        logger.info(f"✅ ElevenLabs voice: {self.voice_id}")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def synthesize(self, text: str, output_path: str) -> bool:
        url = f"{self.BASE_URL}/text-to-speech/{self.voice_id}"
        payload = {
            "text": text,
            "model_id": AudioConfig.ELEVENLABS_MODEL,
            "voice_settings": {
                "stability": AudioConfig.ELEVENLABS_STABILITY,
                "similarity_boost": AudioConfig.ELEVENLABS_SIMILARITY,
                "style": AudioConfig.ELEVENLABS_STYLE,
                "use_speaker_boost": AudioConfig.ELEVENLABS_SPEAKER_BOOST,
            },
        }
        resp = requests.post(url, headers=self.headers, json=payload, timeout=90)
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:200]}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(resp.content)
        return True


# ════════════════════════════════════════════════════════════════
# Google TTS Fallback
# ════════════════════════════════════════════════════════════════
class GoogleTTSAdapter:
    def __init__(self):
        from google.cloud import texttospeech
        self.tts = texttospeech
        self.client = texttospeech.TextToSpeechClient()
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
        text = text.replace(",", '<break time="300ms"/>')
        text = text.replace(".", '<break time="700ms"/>')
        text = text.replace("?", '<break time="700ms"/>')
        return f"<speak>{text}</speak>"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    def synthesize(self, text: str, output_path: str) -> bool:
        ssml = self._ssml(text)
        response = self.client.synthesize_speech(
            input=self.tts.SynthesisInput(ssml=ssml),
            voice=self.voice,
            audio_config=self.audio_config,
        )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(response.audio_content)
        return True


# ════════════════════════════════════════════════════════════════
# Multi-CDN Quran Reciter (3 fallback sources)
# ════════════════════════════════════════════════════════════════
class QuranReciter:
    """تنزيل التلاوة من قارئ بشري — 3 مصادر للأمان."""

    # Sources (in priority order)
    SOURCES = [
        {
            "name": "everyayah_alafasy",
            "template": "https://everyayah.com/data/Alafasy_128kbps/{surah:03d}{ayah:03d}.mp3",
        },
        {
            "name": "everyayah_husary",
            "template": "https://everyayah.com/data/Husary_128kbps/{surah:03d}{ayah:03d}.mp3",
        },
        {
            "name": "islamic_network",
            "template": "https://cdn.islamic.network/quran/audio/128/ar.alafasy/{surah}_{ayah}.mp3",
        },
        {
            "name": "quran_com",
            "template": "https://verses.quran.com/Alafasy/mp3/{surah:03d}{ayah:03d}.mp3",
        },
    ]

    def __init__(self):
        self.cache = Paths.QURAN_AUDIO
        self.cache.mkdir(parents=True, exist_ok=True)

    def _try_url(self, url: str, output_path: str) -> bool:
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and len(resp.content) > 1000:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(resp.content)
                return True
        except Exception as e:
            logger.warning(f"  ⚠️ {url} failed: {e}")
        return False

    def fetch(self, surah: int, ayah: int, output_path: str) -> bool:
        # Cache key
        cache_file = self.cache / f"{surah:03d}_{ayah:03d}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 1000:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(cache_file.read_bytes())
            logger.info(f"♻️ Quran cache hit: {surah}:{ayah}")
            return True

        # Try sources in order
        for src in self.SOURCES:
            url = src["template"].format(surah=surah, ayah=ayah)
            logger.info(f"📿 trying {src['name']}: {url}")
            if self._try_url(url, output_path):
                # Save to cache
                try:
                    cache_file.write_bytes(Path(output_path).read_bytes())
                except Exception:
                    pass
                logger.info(f"✅ Quran {surah}:{ayah} downloaded ({src['name']})")
                return True

        logger.error(f"❌ كل مصادر التلاوة فشلت للآية {surah}:{ayah}")
        return False


# ════════════════════════════════════════════════════════════════
# VoiceEngine — الواجهة الموحّدة
# ════════════════════════════════════════════════════════════════
class VoiceEngine:
    def __init__(self):
        self.adapters = []

        # 1) ElevenLabs primary
        eleven_key = os.getenv("ELEVENLABS_API_KEY", "")
        if eleven_key:
            try:
                voice_id = os.getenv("ELEVENLABS_VOICE_ID") or AudioConfig.ELEVENLABS_VOICE_ID
                self.adapters.append(("elevenlabs", ElevenLabsAdapter(eleven_key, voice_id)))
                logger.info(f"✅ ElevenLabs primary (voice: {voice_id})")
            except Exception as e:
                logger.warning(f"⚠️ ElevenLabs init: {e}")

        # 2) Google TTS fallback
        try:
            self.adapters.append(("google", GoogleTTSAdapter()))
            logger.info("✅ Google TTS fallback ready")
        except Exception as e:
            logger.warning(f"⚠️ Google TTS init: {e}")

        if not self.adapters:
            raise RuntimeError("❌ مفيش محرك TTS متاح!")

        self.reciter = QuranReciter()
        Paths.TTS_CACHE.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str, output_path: str, use_cache: bool = True) -> bool:
        if not text or not text.strip():
            return False

        clean = prepare_arabic_for_tts(text)

        if use_cache:
            cache_key = text_hash(clean, self.adapters[0][0])
            cache_file = Paths.TTS_CACHE / f"{cache_key}.mp3"
            if cache_file.exists() and cache_file.stat().st_size > 500:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(cache_file.read_bytes())
                logger.info(f"♻️ TTS cache hit")
                return True

        last_err = None
        for name, adapter in self.adapters:
            try:
                logger.info(f"🎙️ TTS {name}: {clean[:50]}...")
                ok = adapter.synthesize(clean, output_path)
                if ok and Path(output_path).exists():
                    if use_cache:
                        cache_key = text_hash(clean, self.adapters[0][0])
                        cache_file = Paths.TTS_CACHE / f"{cache_key}.mp3"
                        cache_file.write_bytes(Path(output_path).read_bytes())
                    return True
            except Exception as e:
                last_err = e
                logger.warning(f"⚠️ {name} failed: {e}")
                continue

        logger.error(f"❌ TTS فشل: {last_err}")
        return False

    def fetch_quran_audio(self, surah: int, ayah: int, output_path: str) -> bool:
        return self.reciter.fetch(surah, ayah, output_path)

    def generate_episode_audio(self, script, ep_dir: str) -> Dict[str, str]:
        audio_map: Dict[str, str] = {}
        ep_path = Path(ep_dir)
        ep_path.mkdir(parents=True, exist_ok=True)

        # Intro
        p = str(ep_path / "intro_narrator.mp3")
        if self.synthesize(script.intro_scene.narrator_text, p):
            audio_map["intro"] = p
            script.intro_scene.audio_path = p

        # Ayah scenes
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"

            # 1) intro
            p_intro = str(ep_path / f"{sid}_intro.mp3")
            if self.synthesize(scene.intro_text, p_intro):
                audio_map[f"{sid}_intro"] = p_intro
                scene.intro_audio = p_intro

            # 2) recitation (Quran from CDN)
            p_ayah = str(ep_path / f"{sid}_recitation.mp3")
            if self.fetch_quran_audio(scene.ayah.surah, scene.ayah.number, p_ayah):
                audio_map[f"{sid}_ayah"] = p_ayah
                scene.ayah_audio = p_ayah

            # 3) explanation
            p_explain = str(ep_path / f"{sid}_explain.mp3")
            if self.synthesize(scene.explain_text, p_explain):
                audio_map[f"{sid}_explain"] = p_explain
                scene.explain_audio = p_explain

        # Mid scenes
        for sc in script.mid_scenes:
            p = str(ep_path / f"mid_{sc.scene_id}.mp3")
            if self.synthesize(sc.narrator_text, p):
                audio_map[f"mid_{sc.scene_id}"] = p
                sc.audio_path = p

        # Outro
        p = str(ep_path / "outro_narrator.mp3")
        if self.synthesize(script.outro_scene.narrator_text, p):
            audio_map["outro"] = p
            script.outro_scene.audio_path = p

        logger.info(f"✅ {len(audio_map)} ملف صوتي")
        return audio_map
