"""
voice_engine_v2.py — VALUE / QEEMA v2
═══════════════════════════════════════════════════════
محرك الصوت المُطوَّر
• الراوي  : Google AI Studio (Gemini 2.0 Flash TTS)
• التلاوة : صوت قرآني حقيقي من everyayah.com (لا TTS)
• الاحتياطي: Google Cloud TTS (Neural2 Arabic)
═══════════════════════════════════════════════════════
"""

from __future__ import annotations

import base64
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

import google.generativeai as genai

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
# Google AI Studio TTS (Gemini 2.0 Flash)
# ══════════════════════════════════════════
class GeminiTTS:
    """
    يولد الصوت باستخدام Google AI Studio
    النموذج: gemini-2.0-flash-exp
    """

    def __init__(self):
        if not APIKeys.GEMINI:
            raise ValueError("GEMINI_API_KEY غير موجود")
        genai.configure(api_key=APIKeys.GEMINI)
        self._cache = AudioCache(Paths.EPISODES / "tts_cache")

    def _build_model(self, voice_name: str):
        """يُعِدّ النموذج بإعدادات الصوت"""
        generation_config = {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": voice_name
                    }
                }
            },
        }
        return genai.GenerativeModel(
            model_name=VoiceConfig.MODEL,
            generation_config=generation_config,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def synthesize(self, text: str, voice_name: str, output_path: str) -> str:
        """
        يولد الصوت من النص
        يعيد المسار النهائي للملف MP3
        """
        # تحقق من الكاش
        cached_path = self._cache.get_path(text, voice_name)
        if cached_path:
            shutil.copy(cached_path, output_path)
            return output_path

        logger.info(f"🎙️ Gemini TTS [{voice_name}]: {text[:45]}…")

        model = self._build_model(voice_name)
        response = model.generate_content(text)

        if not response.candidates:
            raise RuntimeError("Gemini TTS: لا يوجد رد")

        part = response.candidates[0].content.parts[0]
        if not hasattr(part, "inline_data") or not part.inline_data.data:
            raise RuntimeError("Gemini TTS: لا توجد بيانات صوتية")

        # PCM → WAV → MP3
        pcm_bytes  = base64.b64decode(part.inline_data.data)
        wav_bytes  = _pcm_to_wav(pcm_bytes, VoiceConfig.PCM_SAMPLE_RATE)
        mp3_path   = _wav_to_mp3(wav_bytes, output_path, VoiceConfig.OUTPUT_SAMPLE_RATE)

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
        stop=stop_after_attempt(len(PREFERRED_RECITERS := ["alafasy", "husary", "sudais", "minshawi"])),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def fetch(self, surah: int, ayah: int, reciter: str = "alafasy") -> bytes:
        """يجلب صوت الآية — يجرب جميع الروابط تلقائياً"""
        cached = self._cache_path(surah, ayah, reciter)
        if cached.exists():
            logger.debug(f"📦 تلاوة محفوظة: {surah}:{ayah} [{reciter}]")
            return cached.read_bytes()

        url = self.CDN_URLS[reciter].format(surah=surah, ayah=ayah)
        logger.info(f"📖 جلب تلاوة {surah}:{ayah} [{reciter}]…")

        resp = requests.get(url, timeout=25, headers={"User-Agent": "QeemaApp/2.0"})
        if resp.status_code == 404:
            raise FileNotFoundError(f"الآية غير موجودة: {surah}:{ayah}")
        resp.raise_for_status()

        data = resp.content
        cached.write_bytes(data)
        return data

    def fetch_surah_audio(
        self,
        surah: int,
        start: int,
        end: int,
        reciter: str = "alafasy",
    ) -> list[dict]:
        """يجلب صوت كل آيات سورة ويعيد قائمة بالمسارات"""
        results = []
        for ayah_num in range(start, end + 1):
            # محاولة الرواة بالتسلسل
            audio_data = None
            used_reciter = reciter

            for r in self.PREFERRED_RECITERS:
                try:
                    audio_data = self.fetch(surah, ayah_num, r)
                    used_reciter = r
                    break
                except Exception as e:
                    logger.warning(f"⚠️ راوي {r} فشل للآية {surah}:{ayah_num}: {e}")
                    continue

            if audio_data is None:
                raise RuntimeError(f"🚨 فشل جلب تلاوة الآية {surah}:{ayah_num} من جميع الروابط")

            # حفظ في مكان مؤقت
            out_path = self._cache_path(surah, ayah_num, used_reciter)
            results.append({
                "ayah": ayah_num,
                "path": str(out_path),
                "reciter": used_reciter,
            })

        logger.info(f"✅ جُلب {len(results)} صوت قرآني لسورة {surah}")
        return results

    def create_repeated_audio(
        self,
        surah: int,
        ayah: int,
        output_path: str,
        repetitions: int = 3,
        pause_between: float = 1.0,
        reciter: str = "alafasy",
    ) -> str:
        """
        يولد ملف صوتي بالآية مكررة N مرات مع وقفات
        """
        audio_data = self.fetch(surah, ayah, reciter)
        single_path = str(self._cache_path(surah, ayah, reciter))

        if not Path(single_path).exists():
            Path(single_path).write_bytes(audio_data)

        # بناء ملف concat بالتكرار
        parts = []
        pause_file = self._create_pause(pause_between)

        for i in range(repetitions):
            parts.append(f"file '{os.path.abspath(single_path)}'")
            if i < repetitions - 1:
                parts.append(f"file '{os.path.abspath(pause_file)}'")

        concat_path = str(self._cache / f"concat_{surah:03d}_{ayah:03d}_{repetitions}x.txt")
        Path(concat_path).write_text("\n".join(parts))

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_path,
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", str(VoiceConfig.OUTPUT_SAMPLE_RATE),
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg concat فشل: {result.stderr[-200:]}")

        return output_path

    def _create_pause(self, duration: float) -> str:
        """يولد ملف صمت لاستخدامه بين التكرارات"""
        pause_path = self._cache / f"pause_{int(duration*10):03d}.mp3"
        if pause_path.exists():
            return str(pause_path)

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=r={VoiceConfig.OUTPUT_SAMPLE_RATE}:cl=mono",
            "-t", str(duration),
            "-c:a", "aac",
            str(pause_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
        return str(pause_path)


# ══════════════════════════════════════════
# MAIN VOICE ENGINE
# ══════════════════════════════════════════
class VoiceEngine:
    """
    المحرك الرئيسي للصوت — يدير Gemini TTS وتلاوة القرآن
    """

    def __init__(self):
        logger.info("🔊 تهيئة محرك الصوت…")
        self.tts        = GeminiTTS()
        self.quran      = QuranAudioFetcher()
        self._narrator_voice = VoiceConfig.NARRATOR_VOICE
        logger.info(f"✅ محرك الصوت جاهز — صوت الراوي: {self._narrator_voice}")

    # ─── Narrator ─────────────────────────────
    def generate_narrator(self, text: str, output_path: str) -> str:
        """صوت جدو أبو زياد للشرح"""
        return self.tts.synthesize(text, self._narrator_voice, output_path)

    # ─── Quran ─────────────────────────────────
    def generate_quran(
        self,
        surah: int,
        ayah: int,
        output_path: str,
        repetitions: int = 3,
        reciter: str = "alafasy",
    ) -> str:
        """تلاوة قرآنية حقيقية — لا TTS"""
        return self.quran.create_repeated_audio(
            surah=surah,
            ayah=ayah,
            output_path=output_path,
            repetitions=repetitions,
            reciter=reciter,
        )

    # ─── Full Episode ───────────────────────────
    def generate_episode_audio(self, script: EpisodeScript, ep_dir: str) -> dict[str, str]:
        """
        يولد جميع الملفات الصوتية للحلقة
        يعيد خريطة: scene_id → مسار الملف
        """
        audio_map: dict[str, str] = {}
        base = Path(ep_dir) / "audio"
        base.mkdir(parents=True, exist_ok=True)

        # ── مشهد الافتتاح ──────────────────────
        logger.info("🎙️ توليد صوت الافتتاح…")
        intro_path = str(base / "intro_narrator.mp3")
        self.generate_narrator(script.intro_scene.narrator_text, intro_path)
        script.intro_scene.audio_path = intro_path
        audio_map["intro"] = intro_path

        # ── مشاهد الآيات ───────────────────────
        for ayah_scene in script.ayah_scenes:
            sid = ayah_scene.scene_id
            logger.info(f"📖 توليد صوت الآية {ayah_scene.ayah.number}…")

            # مقدمة الآية
            intro_p = str(base / f"ayah_{sid:03d}_intro.mp3")
            self.generate_narrator(ayah_scene.intro_text, intro_p)
            ayah_scene.intro_audio = intro_p
            audio_map[f"ayah_{sid}_intro"] = intro_p

            # التلاوة (صوت حقيقي)
            quran_p = str(base / f"ayah_{sid:03d}_quran.mp3")
            self.generate_quran(
                surah=ayah_scene.ayah.surah,
                ayah=ayah_scene.ayah.number,
                output_path=quran_p,
                repetitions=ayah_scene.repetitions,
            )
            ayah_scene.quran_audio = quran_p
            audio_map[f"ayah_{sid}_quran"] = quran_p

            # شرح الآية
            explain_p = str(base / f"ayah_{sid:03d}_explain.mp3")
            self.generate_narrator(ayah_scene.explain_text, explain_p)
            ayah_scene.explain_audio = explain_p
            audio_map[f"ayah_{sid}_explain"] = explain_p

            time.sleep(1.5)   # تجنب rate limit

        # ── المشاهد الوسطى ─────────────────────
        for mid_scene in script.mid_scenes:
            sid = mid_scene.scene_id
            mid_p = str(base / f"mid_{sid:03d}_narrator.mp3")
            self.generate_narrator(mid_scene.narrator_text, mid_p)
            mid_scene.audio_path = mid_p
            audio_map[f"mid_{sid}"] = mid_p

        # ── الخاتمة ───────────────────────────
        logger.info("🎙️ توليد صوت الخاتمة…")
        outro_p = str(base / "outro_narrator.mp3")
        self.generate_narrator(script.outro_scene.narrator_text, outro_p)
        script.outro_scene.audio_path = outro_p
        audio_map["outro"] = outro_p

        logger.info(f"✅ تم توليد {len(audio_map)} ملف صوتي")
        return audio_map
