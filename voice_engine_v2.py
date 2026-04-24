"""
voice_engine_v2.py — VALUE / QEEMA v4.0
محرك الصوت المتقدم باستخدام Google Cloud TTS مع SSML
"""

import os
import re
import logging
from pathlib import Path
from typing import Dict

from google.cloud import texttospeech
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class VoiceEngine:
    """
    محرك صوت احترافي باستخدام Google Cloud TTS.
    يدعم SSML لإنتاج صوت بشري طبيعي.
    """

    # ✅ التصحيح: كتابة التعبير النمطي في سطر واحد (بدون فواصل أسطر داخل السلسلة)
    SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?؟])\s+')

    def __init__(self, voice_name: str = "ar-XA-Wavenet-A", speaking_rate: float = 0.95, pitch: float = -1.0):
        try:
            self.client = texttospeech.TextToSpeechClient()
            self.voice = texttospeech.VoiceSelectionParams(
                language_code="ar-XA",
                name=voice_name
            )
            self.audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=speaking_rate,
                pitch=pitch,
                volume_gain_db=0.0
            )
            logger.info(f"✅ Google Cloud TTS initialized with voice {voice_name}")
        except Exception as e:
            logger.error(f"❌ Failed to initialize TTS: {e}")
            raise

    def _prepare_ssml(self, text: str) -> str:
        """
        تحويل النص إلى SSML مع:
        - إزالة التشكيل من أواخر الكلمات (لتحسين TTS).
        - إضافة فواصل تنفس (breaks) حسب علامات الترقيم.
        """
        # إزالة حركات الإعراب من أواخر الكلمات
        text = re.sub(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])', '', text)

        # استبدال علامات الترقيم بـ <break/>
        text = text.replace('،', '<break time="300ms"/>')
        text = text.replace('؛', '<break time="400ms"/>')
        text = text.replace('.', '<break time="700ms"/>')
        text = text.replace('؟', '<break time="700ms"/>')
        text = text.replace('!', '<break time="600ms"/>')
        text = text.replace(':', '<break time="200ms"/>')

        # لف النص بـ <speak>
        return f"<speak>{text}</speak>"

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
    def synthesize(self, text: str, output_path: str) -> bool:
        """
        توليد ملف MP3 من النص.
        """
        if not text or len(text.strip()) < 2:
            logger.warning("Empty text, skipping synthesis")
            return False

        # تنظيف النص من الأحرف غير المسموحة في XML
        text = text.replace('&', 'and')

        ssml_text = self._prepare_ssml(text)
        try:
            synthesis_input = texttospeech.SynthesisInput(ssml=ssml_text)
            response = self.client.synthesize_speech(
                input=synthesis_input,
                voice=self.voice,
                audio_config=self.audio_config
            )

            # إنشاء المجلد إذا لم يكن موجوداً
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as out:
                out.write(response.audio_content)

            return True
        except Exception as e:
            logger.error(f"❌ TTS synthesis failed: {e}")
            # محاولة إرسال كنص عادي إذا فشل SSML
            try:
                logger.info("Retrying with plain text (no SSML)...")
                synthesis_input = texttospeech.SynthesisInput(text=text)
                response = self.client.synthesize_speech(
                    input=synthesis_input,
                    voice=self.voice,
                    audio_config=self.audio_config
                )
                with open(output_path, "wb") as out:
                    out.write(response.audio_content)
                return True
            except Exception as e2:
                logger.error(f"Plain text also failed: {e2}")
                raise

    def generate_episode_audio(self, script, ep_dir: str) -> Dict[str, str]:
        """
        توليد جميع المقاطع الصوتية للحلقة.
        """
        logger.info(f"🎙️ Starting audio generation for episode {script.episode_number}")
        audio_map = {}

        # intro
        intro_path = os.path.join(ep_dir, "intro_narrator.mp3")
        self.synthesize(script.intro_scene.narrator_text, intro_path)
        audio_map["intro"] = intro_path

        # ayah scenes
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            intro_audio = os.path.join(ep_dir, f"{sid}_intro.mp3")
            self.synthesize(scene.intro_text, intro_audio)
            audio_map[f"{sid}_intro"] = intro_audio

            explain_audio = os.path.join(ep_dir, f"{sid}_explain.mp3")
            self.synthesize(scene.explain_text, explain_audio)
            audio_map[f"{sid}_explain"] = explain_audio

        # outro
        outro_path = os.path.join(ep_dir, "outro_narrator.mp3")
        self.synthesize(script.outro_scene.narrator_text, outro_path)
        audio_map["outro"] = outro_path

        logger.info("✅ Audio generation completed")
        return audio_map