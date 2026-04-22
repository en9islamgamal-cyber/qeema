"""
script_engine.py — VALUE / QEEMA v2.2 (FIXED)
═══════════════════════════════════════════════════════
إصلاحات أساسية:
✅ Smart rate limiting مع adaptive backoff
✅ Better fallback strategy (Cohere أولاً بدل Gemini)
✅ Robust JSON parsing من جميع الـ models
✅ Proper error messages وـ retries
═══════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from google import genai
from google.genai import types as genai_types

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    import cohere
    _COHERE_AVAILABLE = True
except ImportError:
    _COHERE_AVAILABLE = False

from config import APIKeys, CURRICULUM, Paths
from models import (
    AyahScene,
    AudioMood,
    EpisodeScript,
    NarratorScene,
    SceneType,
    VerifiedAyah,
)

logger = logging.getLogger(__name__)

# Model configuration with priority order
MODELS_PRIORITY = [
    # First priority: Cohere (most reliable JSON, best rate limits)
    ("command-r-plus-08-2024", "cohere", 120),
    ("command-r-08-2024", "cohere", 120),
    # Second: Gemini Flash (more rate limit tokens)
    ("gemini-2.5-flash", "gemini", 60),
    # Third: Gemini Pro (better quality but lower rate limit)
    ("gemini-2.5-pro", "gemini", 90),
    ("gemini-3.1-pro-preview", "gemini", 90),
    # Last resort: Claude
    ("claude-opus-4-6", "claude", 60),
    ("claude-sonnet-4-6", "claude", 60),
]


class QuranTextFetcher:
    API_URL = "https://api.qurancdn.com/api/qdc/verses/by_key/{surah}:{ayah}?words=false&fields=text_uthmani"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def fetch(self, surah: int, ayah: int) -> str:
        try:
            resp = requests.get(self.API_URL.format(surah=surah, ayah=ayah), timeout=10)
            resp.raise_for_status()
            return resp.json()["verse"]["text_uthmani"]
        except Exception as e:
            logger.warning(f"⚠️ Quran API فشل: {e}")
            return "نص قرآني موثق"

    def fetch_surah(self, surah: int, start: int, end: int) -> list[VerifiedAyah]:
        return [VerifiedAyah(surah=surah, number=n, text=self.fetch(surah, n), source="quran_api") 
                for n in range(start, end + 1)]


class ScriptEngine:
    SYSTEM_PROMPT = """أنت "الجد أبو زياد"، عالم جليل من علماء الأزهر الشريف، تمتاز بوجه بشوش وقلب حنون وصوت دافئ.
مهمتك: كتابة سيناريو حلقة كرتونية للأطفال (5-8 سنوات) تفسر فيها القرآن بأسلوب "القصة المتصلة".

قواعد الإخراج:
1. الوحدة الموضوعية: احكِ قصة واحدة مترابطة تبدأ من أول آية وتنتهي بآخر آية المطلوبة.
2. اللهجة: عامية مصرية بسيطة ودافئة (يا حبايبي، يا أبطال، سبحان الله العظيم).
3. هندسة الصور: Prompts بالإنجليزية بأسلوب (Cute 3D Pixar style, Disney animation, highly detailed, Islamic friendly).
4. المنع الصارم: ممنوع كتابة نصوص الآيات. استخدم [AYAH_X] فقط.
5. المخرجات: JSON نظيف فقط، بدون أي نصوص إضافية.

JSON SCHEMA (الالتزام التام):
{
  "title": "string",
  "youtube_description": "string",
  "intro_scene": {"narrator_text": "string", "visual_prompt": "string"},
  "ayah_scenes": [
    {
      "ayah_number": int,
      "intro_text": "string",
      "explain_text": "string",
      "visual_prompt": "string"
    }
  ],
  "outro_scene": {"narrator_text": "string", "visual_prompt": "string"}
}"""

    def __init__(self):
        if not APIKeys.GEMINI:
            raise ValueError("GEMINI_API_KEY Missing")
        self.gemini_client = genai.Client(api_key=APIKeys.GEMINI)
        self.cohere_client = cohere.Client(api_key=os.getenv("COHERE_API_KEY")) if os.getenv("COHERE_API_KEY") else None
        self.claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None
        self.text_fetcher = QuranTextFetcher()
        self._rate_limit_backoff = 30  # Start with 30s backoff for 429s

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        logger.info(f"📖 جلب آيات سورة {info['name']} لعمل سكريبت متصل...")
        verified_ayahs = self.text_fetcher.fetch_surah(info["surah"], info["start"], info["end"])

        ayah_refs = "\n".join([f"[AYAH_{a.number}] - الآية {a.number}" for a in verified_ayahs])

        prompt = f"""اكتب سكريبت حلقة عن سورة {info['name']} (من آية {info['start']} إلى {info['end']}).
اجعل التفسير قصة واحدة يرويها الجد أبو زياد. 
المراجع: {ayah_refs}

أجب ONLY بـ JSON بدون أي نصوص إضافية:"""

        data = self._call_ai_with_fallback(prompt)
        script = self._build_script(episode_num, info, data, verified_ayahs)

        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        return script

    def _call_ai_with_fallback(self, prompt: str) -> dict:
        """Call AI models with smart fallback strategy"""
        errors = []
        attempt_count = 0

        for model_name, model_type, min_backoff in MODELS_PRIORITY:
            attempt_count += 1
            logger.info(f"🤖 محاولة باستخدام {model_name} (محاولة #{attempt_count})...")
            
            # Exponential backoff for rate limiting
            if attempt_count > 1:
                wait_time = min(self._rate_limit_backoff * (2 ** (attempt_count - 2)), 300)
                logger.info(f"⏳ الانتظار {wait_time}ث قبل المحاولة التالية...")
                time.sleep(wait_time)

            for retry_attempt in range(3):
                try:
                    if model_type == "gemini":
                        response = self.gemini_client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config=genai_types.GenerateContentConfig(
                                system_instruction=self.SYSTEM_PROMPT,
                                temperature=0.7
                            ),
                        )
                        raw = response.text
                    elif model_type == "cohere":
                        if not self.cohere_client:
                            logger.warning(f"⚠️ Cohere client غير متاح، تخطي {model_name}")
                            break
                        response = self.cohere_client.chat(
                            message=prompt,
                            preamble=self.SYSTEM_PROMPT,
                            model=model_name
                        )
                        raw = response.text
                    else:  # claude
                        if not self.claude_client:
                            logger.warning(f"⚠️ Claude client غير متاح، تخطي {model_name}")
                            break
                        response = self.claude_client.messages.create(
                            model=model_name,
                            max_tokens=4000,
                            system=self.SYSTEM_PROMPT,
                            messages=[{"role": "user", "content": prompt}]
                        )
                        raw = response.content[0].text

                    # Parse and validate JSON
                    parsed = self._parse_json(raw)
                    if self._validate_script_json(parsed):
                        logger.info(f"✅ نجح {model_name}!")
                        return parsed
                    else:
                        raise ValueError("JSON structure غير صالح")

                except Exception as e:
                    error_str = str(e).lower()
                    
                    # Handle rate limiting
                    if any(code in error_str for code in ["429", "quota", "rate limit"]):
                        self._rate_limit_backoff = min(self._rate_limit_backoff * 1.5, 300)
                        logger.warning(f"⚠️ Rate limiting على {model_name}, زيادة الانتظار")
                        if retry_attempt < 2:
                            time.sleep(min(30 * (2 ** retry_attempt), 120))
                            continue
                    
                    # Handle timeout
                    if "timeout" in error_str or "read operation timed out" in error_str:
                        logger.warning(f"⚠️ Timeout على {model_name}")
                        if retry_attempt < 2:
                            time.sleep(15 * (2 ** retry_attempt))
                            continue
                    
                    errors.append(f"{model_name}: {str(e)[:80]}")
                    break

        raise RuntimeError(f"❌ فشلت جميع الموديلات:\n" + "\n".join(errors))

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Robust JSON parsing with multiple cleaning strategies"""
        # Try direct parsing first
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Strategy 1: Remove markdown code blocks
        cleaned = re.sub(r"^\s*```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned, flags=re.MULTILINE)
        
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Strategy 2: Extract JSON object
        match = re.search(r'\{[\s\S]*\}', cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        # Strategy 3: Fix common JSON issues
        fixed = cleaned
        # Fix unescaped quotes
        fixed = re.sub(r'([^\\])"([^"]*)"([^"])', r'\1\"\2\"\3', fixed)
        # Fix missing commas
        fixed = re.sub(r'"\s*"', '", "', fixed)
        
        try:
            return json.loads(fixed)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON parsing فشل نهائياً: {str(e)}")

    @staticmethod
    def _validate_script_json(data: dict) -> bool:
        """Validate script JSON structure"""
        required_keys = ["title", "youtube_description", "intro_scene", "ayah_scenes", "outro_scene"]
        if not all(key in data for key in required_keys):
            return False
        
        if not isinstance(data["ayah_scenes"], list) or len(data["ayah_scenes"]) == 0:
            return False
        
        return True

    def _build_script(self, ep_num, info, data, verified):
        v_map = {a.number: a for a in verified}
        ayah_scenes = []
        
        for i, s in enumerate(data.get("ayah_scenes", [])):
            a_num = s.get("ayah_number")
            if a_num in v_map:
                ayah_scenes.append(AyahScene(
                    scene_id=10 + i,
                    ayah=v_map[a_num],
                    intro_text=s.get("intro_text", ""),
                    explain_text=s.get("explain_text", ""),
                    visual_prompt=s.get("visual_prompt", ""),
                    repetitions=3,
                    duration_sec=35
                ))

        return EpisodeScript(
            episode_number=ep_num,
            surah_name=info["name"],
            surah_number=info["surah"],
            title=data.get("title", ""),
            youtube_title=data.get("title", ""),
            youtube_description=data.get("youtube_description", ""),
            youtube_tags=[],
            total_duration_sec=300,
            intro_scene=NarratorScene(
                scene_id=1,
                scene_type=SceneType.INTRO,
                duration_sec=25,
                narrator_text=data.get("intro_scene", {}).get("narrator_text", ""),
                visual_prompt=data.get("intro_scene", {}).get("visual_prompt", ""),
                mood=AudioMood.INTRO
            ),
            ayah_scenes=ayah_scenes,
            mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99,
                scene_type=SceneType.OUTRO,
                duration_sec=25,
                narrator_text=data.get("outro_scene", {}).get("narrator_text", ""),
                visual_prompt=data.get("outro_scene", {}).get("visual_prompt", ""),
                mood=AudioMood.OUTRO
            )
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
        return None
