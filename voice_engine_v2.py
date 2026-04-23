import os
import re
import html
import math
import logging
from dataclasses import dataclass
from typing import List, Dict, Iterable, Optional

from google.cloud import texttospeech
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

ARABIC_DIACRITICS_RE = re.compile(r'[ً-ْ]+')
MULTI_SPACE_RE = re.compile(r's+')
SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?؟])s+|
+')
CLAUSE_SPLIT_RE = re.compile(r'([،؛:])')

class VoiceEngine:
    """
    محرك TTS عربي متقدم مع SSML ذكي وتقسيم نصوص
    """
    def __init__(
        self,
        voice_name: str = "ar-XA-Wavenet-B",
        language_code: str = "ar-XA",
        speaking_rate: float = 0.92,
        pitch: float = -1.5,
        volume_gain_db: float = 0.0
    ):
        try:
            self.client = texttospeech.TextToSpeechClient()
            self._voice = texttospeech.VoiceSelectionParams(
                language_code=language_code,
                name=voice_name
            )
            self._audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=speaking_rate,
                pitch=pitch,
                volume_gain_db=volume_gain_db
            )
            logger.info("VoiceEngine initialized")
        except Exception as e:
            logger.exception("TTS init failed")
            raise

    def _normalize_text(self, text: str) -> str:
        text = text.replace("ـ", "")
        text = ARABIC_DIACRITICS_RE.sub("", text)
        text = MULTI_SPACE_RE.sub(" ", text)
        return text.strip()

    def _escape_ssml(self, text: str) -> str:
        return html.escape(text, quote=True)

    def _split_text(self, text: str, max_chars: int = 220) -> List[str]:
        text = self._normalize_text(text)
        if len(text) <= max_chars:
            return [text]

        chunks = []
        paragraphs = [p.strip() for p in text.split("
") if p.strip()]

        for p in paragraphs:
            if len(p) <= max_chars:
                chunks.append(p)
                continue

            parts = [s.strip() for s in SENTENCE_SPLIT_RE.split(p) if s.strip()]
            current = ""

            for part in parts:
                if len(current) + len(part) + 1 <= max_chars:
                    current = f"{current} {part}".strip()
                else:
                    if current:
                        chunks.append(current)
                    if len(part) <= max_chars:
                        current = part
                    else:
                        subparts = CLAUSE_SPLIT_RE.split(part)
                        buffer = ""
                        for sp in subparts:
                            if not sp.strip():
                                continue
                            if len(buffer) + len(sp) + 1 <= max_chars:
                                buffer = f"{buffer}{sp}"
                            else:
                                if buffer:
                                    chunks.append(buffer.strip())
                                buffer = sp
                        if buffer:
                            current = buffer.strip()
                        else:
                            current = ""
            if current:
                chunks.append(current)

        return [c.strip() for c in chunks if c.strip()]

    def _prosody_for_chunk(self, chunk: str) -> Dict[str, str]:
        if len(chunk) < 35:
            return {"rate": "88%", "pitch": "-1st"}
        if "!" in chunk or "؟" in chunk:
            return {"rate": "92%", "pitch": "+0st"}
        if "،" in chunk or "؛" in chunk:
            return {"rate": "90%", "pitch": "-1st"}
        return {"rate": "92%", "pitch": "-1st"}

    def _build_ssml_chunk(self, chunk: str, is_first: bool = False, is_last: bool = False) -> str:
        escaped = self._escape_ssml(chunk)

        escaped = escaped.replace("؟", '<break time="650ms"/>')
        escaped = escaped.replace("!", '<break time="500ms"/>')
        escaped = escaped.replace("،", '<break time="280ms"/>')
        escaped = escaped.replace("؛", '<break time="350ms"/>')
        escaped = escaped.replace(":", '<break time="220ms"/>')
        escaped = escaped.replace(".", '<break time="420ms"/>')

        prosody = self._prosody_for_chunk(chunk)
        body = f'<prosody rate="{prosody["rate"]}" pitch="{prosody["pitch"]}">{escaped}</prosody>'

        if is_first:
            body = f'<p>{body}</p>'
        if is_last:
            body = f'{body}'
        return body

    def _prepare_ssml(self, text: str) -> str:
        chunks = self._split_text(text, max_chars=220)
        if not chunks:
            return "<speak></speak>"

        parts = []
        for i, chunk in enumerate(chunks):
            if i > 0:
                parts.append('<break time="180ms"/>')
            parts.append(self._build_ssml_chunk(chunk, is_first=(i == 0), is_last=(i == len(chunks)-1)))

        return "<speak>" + "".join(parts) + "</speak>"

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        retry=retry_if_exception_type(Exception)
    )
    def synthesize(self, text: str, output_path: str) -> bool:
        if not text or len(text.strip()) < 2:
            return False

        ssml_text = self._prepare_ssml(text)
        input_text = texttospeech.SynthesisInput(ssml=ssml_text)

        response = self.client.synthesize_speech(
            input=input_text,
            voice=self._voice,
            audio_config=self._audio_config
        )

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as out:
            out.write(response.audio_content)

        return True

    def generate_episode_audio(self, script, ep_dir) -> dict:
        logger.info(f"بدء إنتاج الحلقة {script.episode_number}")
        os.makedirs(ep_dir, exist_ok=True)

        audio_map = {}

        sections = [
            ("intro", script.intro_scene.narrator_text, "intro_narrator.mp3"),
            ("outro", script.outro_scene.narrator_text, "outro_narrator.mp3"),
        ]

        self.synthesize(script.intro_scene.narrator_text, os.path.join(ep_dir, "intro_narrator.mp3"))
        audio_map["intro"] = os.path.join(ep_dir, "intro_narrator.mp3")

        for scene in script.ayah_scenes:
            intro_key = f"ayah_{scene.scene_id}_intro"
            explain_key = f"ayah_{scene.scene_id}_explain"

            intro_path = os.path.join(ep_dir, f"{intro_key}.mp3")
            explain_path = os.path.join(ep_dir, f"{explain_key}.mp3")

            self.synthesize(scene.intro_text, intro_path)
            self.synthesize(scene.explain_text, explain_path)

            audio_map[intro_key] = intro_path
            audio_map[explain_key] = explain_path

            logger.info(f"تم تجهيز الآية {scene.ayah.number}")

        self.synthesize(script.outro_scene.narrator_text, os.path.join(ep_dir, "outro_narrator.mp3"))
        audio_map["outro"] = os.path.join(ep_dir, "outro_narrator.mp3")

        logger.info("اكتمل إنتاج الحلقة")
        return audio_map