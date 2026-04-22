"""
voice_engine_v2.py — VALUE / QEEMA v2.1
═══════════════════════════════════════════════════════
محرك الصوت المُطوَّر
• الراوي  : Google Gen AI (Gemini 2.5 Flash TTS)
• التلاوة : صوت قرآني حقيقي من everyayah.com (لا TTS)
• الاحتياطي: Google Cloud TTS (Neural2 Arabic)
═══════════════════════════════════════════════════════
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

# ── Google Gen AI SDK الجديد (بديل google.generativeai) ──
from google import genai
from google.genai import types as genai_types

from config import APIKeys, Paths, VoiceConfig
from models import AyahScene, EpisodeScript, NarratorScene

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# PCM → WAV utility
# ══════════════════════════════════════════
def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000) -> bytes:
    """يحوّل بيانات PCM الخام إلى WAV صالح للتشغيل"""
    num_ch   = 1
    bit_d    = 16
    bps      = bit_d // 8
    byte_rt  = sample_rate * num_ch * bps
    blk_aln  = num_ch * bps
    data_sz  = len(pcm_data)

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_sz,
        b"WAVE", b"fmt ", 16,
        1, num_ch,
        sample_rate, byte_rt,
        blk_aln, bit_d,
        b"data", data_sz,
    )
    return header + pcm_data


def _wav_to_mp3(wav_bytes: bytes, output_path: str, target_sr: int = 48000) -> str:
    """يحوّل WAV إلى MP3 بجودة عالية عبر FFmpeg"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f",  "wav",
        "-i",  "pipe:0",
        "-ar", str(target_sr),
        "-ac", "1",
        "-b:a", VoiceConfig.OUTPUT_BITRATE,
        "-af",  "loudnorm=I=-16:TP=-1.5:LRA=11",
        output_path,
    ]
    result = subprocess.run(cmd, input=wav_bytes, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg WAV→MP3 فشل: {result.stderr[-200:]}")
    return output_path


# ══════════════════════════════════════════
# Audio Cache
# ══════════════════════════════════════════
class AudioCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, text: str, voice: str) -> str:
        return hashlib.sha256(f"{text}|{voice}".encode()).hexdigest()[:24]

    def get(self, text: str, voice: str) -> Optional[bytes]:
        f = self.cache_dir / f"{self._key(text, voice)}.mp3"
        if f.exists():
            logger.debug(f"📦 cache hit: {self._key(text, voice)[:8]}…")
            return f.read_bytes()
        return None

    def set(self, text: str, voice: str, data: bytes) -> None:
        f = self.cache_dir / f"{self._key(text, voice)}.mp3"
        f.write_bytes(data)

    def get_path(self, text: str, voice: str) -> Optional[str]:
        f = self.cache_dir / f"{self._key(text, voice)}.mp3"
        return str(f) if f.exists() else None


# ══════════════════════════════════════════
# Google Gen AI TTS (Gemini 2.5 Flash/Pro Preview TTS)
# ══════════════════════════════════════════
class GeminiTTS:
    """
    يولد الصوت باستخدام Google Gen AI SDK الجديد.
    الموديل المفضّل: gemini-2.5-flash-preview-tts
    (عدّل VoiceConfig.MODEL في config.py إذا احتجت)
    """

    def __init__(self):
        if not APIKeys.GEMINI:
            raise ValueError("GEMINI_API_KEY غير موجود")

        # الـ SDK الجديد: client بدل configure
        self.client = genai.Client(api_key=APIKeys.GEMINI)
        self._cache = AudioCache(Paths.EPISODES / "tts_cache")

    def _build_config(self, voice_name: str) -> genai_types.GenerateContentConfig:
        """يبني إعدادات التوليد الصوتي بالصيغة الجديدة."""
        return genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=voice_name,
                    )
                )
            ),
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def synthesize(self, text: str, voice_name: str, output_path: str) -> str:
        """
        يولد الصوت من النص ويُعيد مسار الـ MP3.
        """
        # تحقق من الكاش
        cached_path = self._cache.get_path(text, voice_name)
        if cached_path:
            shutil.copy(cached_path, output_path)
            return output_path

        logger.info(f"🎙️ Gemini TTS [{voice_name}]: {text[:45]}…")

        # استدعاء الـ API بالصيغة الجديدة
        response = self.client.models.generate_content(
            model=VoiceConfig.MODEL,
            contents=text,
            config=self._build_config(voice_name),
        )

        if not response.candidates:
            raise RuntimeError("Gemini TTS: لا يوجد رد")

        part = response.candidates[0].content.parts[0]
        if not getattr(part, "inline_data", None) or not part.inline_data.data:
            raise RuntimeError("Gemini TTS: لا توجد بيانات صوتية")

        # ⚠️ فرق جوهري عن الـ SDK القديم:
        # في الـ SDK الجديد، inline_data.data = bytes مباشرة (مش base64 string)
        pcm_bytes = part.inline_data.data
        if isinstance(pcm_bytes, str):
            # حماية دفاعية لو صدف رجع string لأي سبب
            import base64
            pcm_bytes = base64.b64decode(pcm_bytes)

        # PCM → WAV → MP3
        wav_bytes = _pcm_to_wav(pcm_bytes, VoiceConfig.PCM_SAMPLE_RATE)
        mp3_path  = _wav_to_mp3(wav_bytes, output_path, VoiceConfig.OUTPUT_SAMPLE_RATE)

        # حفظ في الكاش
        self._cache.set(text, voice_name, Path(mp3_path).read_bytes())

        logger.info(f"✅ TTS محفوظ: {Path(output_path).name}")
        return mp3_path


# ══════════════════════════════════════════
# Quran Audio Fetcher (NOT TTS — real recitation)
# ══════════════════════════════════════════
class QuranAudioFetcher:
    """
    يجلب التلاوة القرآنية الحقيقية من CDN موثوق
    لا يُستخدم أي TTS للنص القرآني — هذا خط أحمر
    """

    CDN_URLS = {
        "alafasy":  VoiceConfig.QURAN_CDN_ALAFASY,
        "sudais":   VoiceConfig.QURAN_CDN_SUDAIS,
        "husary":   VoiceConfig.QURAN_CDN_HUSARY,
        "minshawi": VoiceConfig.QURAN_CDN_MINSHAWI,
    }

    PREFERRED_RECITERS = ["alafasy", "husary", "sudais", "minshawi"]

    def __init__(self):
        self._cache = Path(Paths.EPISODES / "quran_audio_cache")
        self._cache.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, surah: int, ayah: int, reciter: str) -> Path:
        return self._cache / f"{reciter}_{surah:03d}_{ayah:03d}.mp3"

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def fetch(self, surah: int, ayah: int, reciter: str = "alafasy") -> bytes:
        """يجلب صوت الآية — retry على أخطاء الشبكة"""
        cached = self._cache_path(surah, ayah, reciter)
        if cached.exists():
            logger.debug(f"📦 تلاوة محفوظة: {surah}:{ayah} [{reciter}]")
            return cached.read_bytes()

        url = self.CDN_URLS[reciter].format(surah=surah, ayah=ayah)
        logger.info(f"📖 جلب تلاوة {surah}:{ayah} [{reciter}]…")

        resp = requests.get(url, timeout=25, headers={"User-Agent": "QeemaApp/2.0"})
        if resp.status_code == 404:
            raise FileNotFoundError(f"الآية غير موجود