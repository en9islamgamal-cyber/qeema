"""
script_engine.py — VALUE / QEEMA v2.1
═══════════════════════════════════════════════════════
محرك السكريبت المطور - نظام السرد القصصي الشامل
• Strategy: Holistic Prompting (تفسير شامل للسورة لتقليل الكوتة)
• Persona: الجد أبو زياد (عالم أزهري، حنون، دافئ)
• Visuals: Pixar 3D Cinematic Style (English Prompts)
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

# إعدادات الموديلات
PRIMARY_MODEL = os.getenv("QEEMA_PRIMARY_MODEL", "gemini-2.5-pro")
FALLBACK_MODEL = os.getenv("QEEMA_FALLBACK_MODEL", "gemini-3.1-pro-preview")
CLAUDE_MODEL = os.getenv("QEEMA_CLAUDE_MODEL", "claude-opus-4-7")

class QuranTextFetcher:
    """يجلب النص القرآني من API موثوق"""
    API_URL = "[https://api.qurancdn.com/api/qdc/verses/by_key/](https://api.qurancdn.com/api/qdc/verses/by_key/){surah}:{ayah}?words=false&fields=text_uthmani"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def fetch(self, surah: int, ayah: int) -> str:
        try:
            resp = requests.get(self.API_URL.format(surah=surah, ayah=ayah), timeout=10)
            resp.raise_for_status()
            return resp.json()["verse"]["text_uthmani"]
        except Exception:
            return "نص قرآني موثق"

    def fetch_surah(self, surah: int, start: int, end: int) -> list[VerifiedAyah]:
        return [VerifiedAyah(surah=surah, number=n, text=self.fetch(surah, n), source="quran_api") 
                for n in range(start, end + 1)]

class ScriptEngine:
    """المحرك الذكي لإنتاج السيناريو بأسلوب الجد أبو زياد (Holistic Approach)"""
    
    SYSTEM_PROMPT = """أنت "الجد أبو زياد"، شيخ من علماء الأزهر الشريف، وقور وحنون.
مهمتك: كتابة سكريبت حلقة كرتونية للأطفال (5-8 سنوات) تفسر معاني القرآن بأسلوب "القصة المتصلة".

قواعد الإخراج:
1. السرد الشامل: لا تفسر كل آية كأنها جزر منعزلة. احكِ قصة واحدة ممتعة تبدأ من الآية الأولى حتى الأخيرة.
2. اللهجة: عامية مصرية بسيطة وراقية (يا حبايب جدو، يا أبطال، سبحان الله العظيم).
3. الصور (Visual Prompts): يجب أن تكون بالإنجليزية بأسلوب (Cute 3D Pixar style, Disney animation, vibrant colors, Islamic kid friendly).
4. المنع القرآني: ممنوع كتابة أي نص قرآني. استخدم المرجع [AYAH_X] فقط.
5. المخرجات: أجب بصيغة JSON نظيفة فقط بلا أي مقدمات."""

    def __init__(self):
        if not APIKeys.GEMINI: raise ValueError("GEMINI_API_KEY Missing")
        self.gemini_client = genai.Client(api_key=APIKeys.GEMINI)
        
        # تهيئة الكوهير والكلود
        self.cohere_client = cohere.Client(api_key=os.getenv("COHERE_API_KEY")) if os.getenv("COHERE_API_KEY") else None
        self.claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None
        
        self.text_fetcher = QuranTextFetcher()

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        logger.info(f"📖 جلب آيات سورة {info['name']} لعمل سكريبت متصل...")
        verified_ayahs = self.text_fetcher.fetch_surah(info["surah"], info["start"], info["end"])

        ayah_refs = "\n".join([f"[AYAH_{a.number}] - الآية {a.number}" for a in verified_ayahs])
        
        # 💡 إستراتيجية الطلب الواحد الشامل لتوفير الكوتة
        prompt = f"""اكتب سكريبت حلقة عن سورة {info['name']} (الآيات من {info['start']} إلى {info['end']}).
اجعل الجد أبو زياد يحكي قصة متصلة تشرح هذه المعاني بسلاسة للأطفال.
المراجع المتاحة: 
{ayah_refs}

يجب أن يكون الرد JSON بالهيكل التالي:
{{
  "title": "عنوان الحلقة",
  "youtube_description": "وصف يوتيوب",
  "intro_scene": {{"narrator_text": "نص الترحيب والتمهيد للقصة", "visual_prompt": "English pixar style prompt"}},
  "ayah_scenes": [
    {{"ayah_number": {info['start']}, "intro_text": "ربط القصة بالآية", "explain_text": "شرح بسيط جداً", "visual_prompt": "English prompt"}}
  ],
  "outro_scene": {{"narrator_text": "خاتمة القصة والوداع", "visual_prompt": "English prompt"}}
}}"""

        data = self._call_ai_with_fallback(prompt)
        script = self._build_script(episode_num, info, data, verified_ayahs)
        
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        logger.info(f"✅ سكريبت احترافي محفوظ: {save_path.name}")
        return script

    def _call_ai_with_fallback(self, prompt: str) -> dict:
        # قائمة المحاولات الذكية مع إصدارات كوهير المحدثة
        models = [
            (PRIMARY_MODEL, "gemini"), (FALLBACK_MODEL, "gemini"), 
            ("gemini-2.5-flash", "gemini"), ("command-r-plus-08-2024", "cohere"),
            ("command-r-08-2024", "cohere"), (CLAUDE_MODEL, "claude")
        ]
        
        errors = []
        for m_name, m_type in models:
            logger.info(f"🤖 محاولة باستخدام: {m_name}...")
            for attempt in range(5):
                try:
                    if m_type == "gemini":
                        raw = self.gemini_client.models.generate_content(
                            model=m_name, contents=prompt, 
                            config=genai_types.GenerateContentConfig(system_instruction=self.SYSTEM_PROMPT, temperature=0.7)
                        ).text
                    elif m_type == "cohere":
                        if not self.cohere_client: break
                        raw = self.cohere_client.chat(message=prompt, preamble=self.SYSTEM_PROMPT, model=m_name).text
                    else:
                        if not self.claude_client: break
                        raw = self.claude_client.messages.create(
                            model=m_name, max_tokens=4000, system=self.SYSTEM_PROMPT,
                            messages=[{"role": "user", "content": prompt}]
                        ).content[0].text
                    
                    return self._parse_json(raw)
                except Exception as e:
                    err_msg = str(e).lower()
                    if any(x in err_msg for x in ["429", "503", "quota", "limit"]):
                        wait = 15 * (2 ** attempt)
                        logger.warning(f"⚠️ ضغط على الموديل. انتظار {wait}ث...")
                        time.sleep(wait); continue
                    errors.append(f"{m_name}: {str(e)[:60]}")
                    break
        raise RuntimeError("فشلت كل المحاولات:\n" + "\n".join(errors))

    @staticmethod
    def _parse_json(raw: str) -> dict:
        # تنظيف علامات التنسيق التي تسبب الخطأ
        cleaned = re.sub(r"^\x60{3}(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*\x60{3}$", "", cleaned, flags=re.MULTILINE)
        return json.loads(cleaned)

    def _build_script(self, ep_num, info, data, verified):
        v_map = {a.number: a for a in verified}
        ayah_scenes = []
        for i, s in enumerate(data.get("ayah_scenes", [])):
            a_num = s.get("ayah_number", info["start"] + i)
            if a_num in v_map:
                ayah_scenes.append(AyahScene(
                    scene_id=10 + i, ayah=v_map[a_num],
                    intro_text=s.get("intro_text", ""), explain_text=s.get("explain_text", ""),
                    visual_prompt=s.get("visual_prompt", "Pixar 3D animation style"),
                    repetitions=3, duration_sec=35
                ))
        
        return EpisodeScript(
            episode_number=ep_num, surah_name=info["name"], surah_number=info["surah"],
            title=data.get("title", f"سورة {info['name']}"), 
            youtube_title=data.get("title", ""), youtube_description=data.get("youtube_description", ""),
            youtube_tags=[], total_duration_sec=300,
            intro_scene=NarratorScene(
                scene_id=1, scene_type=SceneType.INTRO, duration_sec=25,
                narrator_text=data["intro_scene"]["narrator_text"],
                visual_prompt=data["intro_scene"]["visual_prompt"], mood=AudioMood.INTRO
            ),
            ayah_scenes=ayah_scenes, mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99, scene_type=SceneType.OUTRO, duration_sec=25,
                narrator_text=data["outro_scene"]["narrator_text"],
                visual_prompt=data["outro_scene"]["visual_prompt"], mood=AudioMood.OUTRO
            )
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
        return None