"""
voice_engine_v2.py — VALUE / QEEMA v4.0
محرك الصوت باستخدام Google Cloud TTS.
"""

import os
import re
import logging
from pathlib import Path

from google.cloud import texttospeech
from tenacity import retry, stop_after_attempt, wait_exponential

from config import AudioConfig

logger = logging.getLogger(__name__)


class VoiceEngine:
    def __init__(self):
        self.client = texttospeech.TextToSpeechClient()
        self.voice = texttospeech.VoiceSelectionParams(
            language_code="ar-XA",
            name=AudioConfig.DEFAULT_VOICE
        )
        self.audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=AudioConfig.DEFAULT_SPEAKING_RATE,
            pitch=AudioConfig.DEFAULT_PITCH,
            volume_gain_db=AudioConfig.VOLUME_GAIN_DB
        )
        logger.info(f"✅ Google Cloud TTS initialized with voice {AudioConfig.DEFAULT_VOICE}")

    def _prepare_ssml(self, text: str) -> str:
        text = re.sub(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])', '', text)
        text = text.replace('،', '<break time="300ms"/>')
        text = text.replace('.', '<break time="700ms"/>')
        text = text.replace('؟', '<break time="700ms"/>')
        return f"<speak>{text}</speak>"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    def synthesize(self, text: str, output_path: str) -> bool:
        if not text:
            return False
        ssml = self._prepare_ssml(text)
        synthesis_input = texttospeech.SynthesisInput(ssml=ssml)
        response = self.client.synthesize_speech(
            input=synthesis_input,
            voice=self.voice,
            audio_config=self.audio_config
        )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(response.audio_content)
        return True

    def generate_episode_audio(self, script, ep_dir: str):
        audio_map = {}
        intro_path = os.path.join(ep_dir, "intro_narrator.mp3")
        self.synthesize(script.intro_scene.narrator_text, intro_path)
        audio_map["intro"] = intro_path
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            intro_audio = os.path.join(ep_dir, f"{sid}_intro.mp3")
            self.synthesize(scene.intro_text, intro_audio)
            audio_map[f"{sid}_intro"] = intro_audio
            explain_audio = os.path.join(ep_dir, f"{sid}_explain.mp3")
            self.synthesize(scene.explain_text, explain_audio)
            audio_map[f"{sid}_explain"] = explain_audio
        outro_path = os.path.join(ep_dir, "outro_narrator.mp3")
        self.synthesize(script.outro_scene.narrator_text, outro_path)
        audio_map["outro"] = outro_path
        return audio_map