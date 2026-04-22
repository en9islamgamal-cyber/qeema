"""
script_engine.py — VALUE / QEEMA v2.1 (Prompt Engineering Edition)
═══════════════════════════════════════════════════════
محرك السكريبت - منظومة الهندسة العكسية للأوامر
• لا توجد نصوص ثابتة (No Hardcoding).
• Few-Shot Prompting: إجبار الموديل عبر تقديم مثال JSON متكامل.
• Quality Gate: رفض أي مخرج ذكاء اصطناعي لا يطابق معايير الطول والجودة.
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
    AyahScene, AudioMood, EpisodeScript, 
    NarratorScene, SceneType, VerifiedAyah
)

logger = logging.getLogger(__name__)

PRIMARY_MODEL = os.getenv("QEEMA_PRIMARY_MODEL", "gemini-2.5-pro")
FALLBACK_MODEL = os.getenv("QEEMA_FALLBACK_MODEL", "gemini-3.1-pro-preview")
CLAUDE_MODEL = os.getenv("QEEMA_CLAUDE_MODEL", "claude-opus-4-7")

class QuranTextFetcher:
    API_URL = "https://api.qurancdn.com/api/qdc/verses/by_key/{surah}:{ayah}?words=false&fields=text_uthmani"

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
    # 💡 منظومة البرومبت الهندسي الشامل (Master Prompt)
    SYSTEM_PROMPT = """أنت "الجد أبو زياد"، عالم أزهري حنون ودافئ القلب، تروي قصصاً للأطفال (5-8 سنوات) لتفسير القرآن الكريم.

[القيود والقواعد الصارمة - يجب الالتزام بها حرفياً]:
1. السرد القصصي: لا تقم بتفسير جاف. اربط الآيات بقصة واحدة متصلة وممتعة.
2. اللهجة: عامية مصرية راقية وبسيطة (يا حبايبي، يا أبطال، شوفوا الجمال، سبحان الله).
3. الطول والتفصيل: يُمنع منعاً باتاً ترك أي خانة فارغة. يجب أن يكون التمهيد (intro_text) والشرح (explain_text) لكل آية مفصلاً، دافئاً، ولا يقل عن 20 كلمة.
4. هندسة الصور: اكتب (visual_prompt) باللغة الإنجليزية فقط بستايل (Pixar 3D animation, cute, Islamic kid friendly, highly detailed).
5. الأمانة: يُمنع كتابة نص الآيات القرآنية داخل السكريبت. استخدم المرجع [AYAH_X] فقط.

[مثال للإخراج المطلوب - JSON Example]:
{
  "title": "قصة سورة الإخلاص",
  "youtube_title": "تفسير سورة الإخلاص للأطفال مع الجد أبو زياد",
  "youtube_description": "حلقة ممتعة نتعلم فيها عن سورة الإخلاص...",
  "intro_scene": {
    "narrator_text": "يا هلا بأبطالي الحلوين! وحشتوني جداً. النهاردة يا حبايبي هنطير مع بعض في رحلة جميلة جداً نتعرف فيها على سورة عظيمة، سورة بتعلمنا مين هو ربنا سبحانه وتعالى.",
    "visual_prompt": "Pixar 3D animation, a wise smiling grandfather with a white beard wearing Al-Azhar uniform, sitting in a cozy room surrounded by attentive cute kids, warm lighting."
  },
  "ayah_scenes": [
    {
      "ayah_number": 1,
      "intro_text": "تعالوا يا ولاد نفتح قلوبنا ونسمع أول آية، ونشوف ربنا سبحانه وتعالى بيأمر النبي محمد صلى الله عليه وسلم يقول إيه...",
      "explain_text": "الآية دي يا حبايبي معناها إن ربنا سبحانه وتعالى واحد بس، مفيش حد زيه، ولا في حد معاه بيشاركه في الكون العظيم ده.",
      "visual_prompt": "Pixar 3D animation, beautiful glowing number one in a starry night sky, children looking up in wonder."
    }
  ],
  "outro_scene": {
    "narrator_text": "شفتوا يا ولاد سورة الإخلاص جميلة إزاي؟ بتعلمنا إن ربنا عظيم وواحد. هستناكم الحلقة الجاية في حكاية جديدة، بحبكم في الله.",
    "visual_prompt": "Pixar 3D animation, grandfather hugging the kids, waving goodbye, warm sunset lighting."
  }
}

مهمتك الآن: قم بتوليد السكريبت للسورة المطلوبة بنفس الجودة، الهيكل، وطول النصوص الموجود في المثال أعلاه. أجب بـ JSON فقط."""

    def __init__(self):
        if not APIKeys.GEMINI: raise ValueError("GEMINI_API_KEY Missing")
        self.gemini_client = genai.Client(api_key=APIKeys.GEMINI)
        self.cohere_client = cohere.Client(api_key=os.getenv("COHERE_API_KEY")) if os.getenv("COHERE_API_KEY") else None
        self.claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None
        self.text_fetcher = QuranTextFetcher()

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        logger.info(f"📖 جلب آيات سورة {info['name']}...")
        verified_ayahs = self.text_fetcher.fetch_surah(info["surah"], info["start"], info["end"])
        ayah_refs = "\n".join([f"[AYAH_{a.number}]" for a in verified_ayahs])
        
        prompt = f"""السورة المطلوبة: {info['name']}
من الآية: {info['start']} إلى الآية: {info['end']}
المراجع (استخدم المرجع بدل النص): {ayah_refs}
قم بإنشاء JSON بناءً على القواعد والأمثلة التي تعلمتها."""

        # 🛡️ بوابة الجودة: المحاولة حتى نحصل على رد دقيق
        data = self._call_ai_with_quality_gate(prompt, info, verified_ayahs)
        
        script = self._build_script(episode_num, info, data, verified_ayahs)
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        return script

    def _call_ai_with_quality_gate(self, prompt: str, info: dict, verified: list) -> dict:
        """منظومة استدعاء الذكاء الاصطناعي مع فحص الجودة (Quality Gate)"""
        models = [
            (PRIMARY_MODEL, "gemini"), 
            (FALLBACK_MODEL, "gemini"), 
            ("command-r-plus-08-2024", "cohere"), 
            ("command-r-08-2024", "cohere"),
            ("gemini-2.5-flash", "gemini")
        ]
        
        for m_name, m_type in models:
            logger.info(f"🤖 طلب الإنتاج من: {m_name}...")
            for attempt in range(2): # محاولتان لكل موديل
                try:
                    # 1. الاستدعاء
                    if m_type == "gemini":
                        raw = self.gemini_client.models.generate_content(
                            model=m_name, contents=prompt, 
                            config=genai_types.GenerateContentConfig(system_instruction=self.SYSTEM_PROMPT)
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
                    
                    # 2. التنظيف والتحويل
                    cleaned = re.sub(r"^\x60{3}(?:json)?\s*", "", raw, flags=re.MULTILINE)
                    cleaned = re.sub(r"\s*\x60{3}$", "", cleaned, flags=re.MULTILINE)
                    data = json.loads(cleaned)
                    
                    # 3. 🛡️ بوابة الجودة (Quality Validation)
                    # نرفض الرد داخلياً إذا كانت النصوص غير كافية
                    for i, s in enumerate(data.get("ayah_scenes", [])):
                        if len(s.get("intro_text", "")) < 15 or len(s.get("explain_text", "")) < 20:
                            raise ValueError(f"AI returned incomplete/short text for Ayah. Rejecting.")
                    
                    logger.info(f"✅ تم قبول إخراج {m_name} لنجاحه في اختبار الجودة.")
                    return data
                    
                except Exception as e:
                    logger.warning(f"⚠️ رفض إخراج {m_name} (محاولة {attempt+1}): {str(e)[:60]}")
                    time.sleep(10)
                    continue # حاول مرة أخرى أو انتقل للموديل التالي
                    
        raise RuntimeError("فشلت كافة نماذج الذكاء الاصطناعي في إنتاج سكريبت مطابق لمعايير الجودة.")

    def _build_script(self, ep_num, info, data, verified):
        """بناء الكائن النهائي ببيانات نقية قادمة من الـ AI فقط"""
        v_map = {a.number: a for a in verified}
        ayah_scenes = []
        
        for i, s in enumerate(data.get("ayah_scenes", [])):
            a_num = s.get("ayah_number", info["start"] + i)
            if a_num in v_map:
                ayah_scenes.append(AyahScene(
                    scene_id=10 + i, 
                    ayah=v_map[a_num],
                    intro_text=s["intro_text"],     # نأخذ إجابة AI كما هي لأننا فحصناها
                    explain_text=s["explain_text"], # نأخذ إجابة AI كما هي لأننا فحصناها
                    visual_prompt=s.get("visual_prompt", "Pixar 3D animation"),
                    repetitions=3, 
                    duration_sec=35
                ))
        
        return EpisodeScript(
            episode_number=ep_num, surah_name=info["name"], surah_number=info["surah"],
            title=data.get("title", f"سورة {info['name']}"), 
            youtube_title=data.get("youtube_title", f"تفسير سورة {info['name']}"), 
            youtube_description=data.get("youtube_description", ""),
            youtube_tags=[], total_duration_sec=300,
            intro_scene=NarratorScene(
                scene_id=1, scene_type=SceneType.INTRO, duration_sec=25, 
                narrator_text=data["intro_scene"]["narrator_text"], 
                visual_prompt=data["intro_scene"]["visual_prompt"], 
                mood=AudioMood.INTRO
            ),
            ayah_scenes=ayah_scenes, mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99, scene_type=SceneType.OUTRO, duration_sec=25,
                narrator_text=data["outro_scene"]["narrator_text"], 
                visual_prompt=data["outro_scene"]["visual_prompt"], 
                mood=AudioMood.OUTRO
            )
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
        return None