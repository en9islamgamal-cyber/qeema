"""
script_engine.py — VALUE / QEEMA v2.1
═══════════════════════════════════════════════════════
محرك السكريبت المطور - نظام الإنتاج المنطقي
• التغيير: حذف النصوص الثابتة والاعتماد على نظام "الرفض وإعادة المحاولة".
• الإستراتيجية: فرض هيكل JSON صارم عبر الأمثلة (Few-Shot).
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

class ScriptEngine:
    """المحرك الذكي: يرفض الردود الناقصة ويحاول مع موديلات بديلة"""
    
    SYSTEM_PROMPT = """أنت "الجد أبو زياد"، عالم أزهري حنون. مهمتك كتابة سكريبت حلقة كرتونية للأطفال.
يجب أن يكون ردك عبارة عن JSON فقط، ويجب أن تلتزم تماماً بطول النصوص (التمهيد والشرح) لتكون ممتعة ومفيدة.

مثال للهيكل المطلوب (التزم بهذا الحجم من الكلام):
{
  "title": "رحلة في سورة الفاتحة",
  "youtube_description": "وصف تفصيلي للحلقة...",
  "intro_scene": {
    "narrator_text": "أهلاً يا حبايبي يا أبطال، وحشتوني جداً! النهاردة هنبدأ مع بعض رحلة ممتعة جداً في كتاب ربنا..."
  },
  "ayah_scenes": [
    {
      "ayah_number": 1,
      "intro_text": "تعالوا يا ولاد نسمع أول آية بقلوبنا، ونشوف ربنا بيبدأ كلامه بإيه..",
      "explain_text": "يعني يا حبايبي بنبدأ أي حاجة بنعملها باسم ربنا عشان يبارك لنا فيها ويحفظنا.."
    }
  ],
  "outro_scene": {
    "narrator_text": "شفتوا الجمال يا ولاد؟ القرآن كله نور.. استنوني الحلقة الجاية عشان نكمل حكاياتنا."
  }
}

قاعدة صارمة: لا تترك أي حقل فارغاً، ولا تستخدم نصوصاً قصيرة جداً."""

    def __init__(self):
        if not APIKeys.GEMINI: raise ValueError("GEMINI_API_KEY Missing")
        self.gemini_client = genai.Client(api_key=APIKeys.GEMINI)
        self.cohere_client = cohere.Client(api_key=os.getenv("COHERE_API_KEY")) if os.getenv("COHERE_API_KEY") else None
        self.claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None
        self.text_fetcher = requests # استخدام مكتبة requests مباشرة للتبسيط

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        # جلب النصوص القرآنية أولاً للتأكد من المرجعية
        verified_ayahs = self._fetch_verified_ayahs(info)
        
        ayah_refs = "\n".join([f"[AYAH_{a.number}]" for a in verified_ayahs])
        prompt = f"اكتب سكريبت حلقة سورة {info['name']} (الآيات {info['start']}-{info['end']}). المراجع: {ayah_refs}"

        # المحاولة مع الموديلات حتى نحصل على JSON "منطقي وكامل"
        data = self._call_ai_with_logic_check(prompt)
        
        return self._build_script(episode_num, info, data, verified_ayahs)

    def _call_ai_with_logic_check(self, prompt: str) -> dict:
        """يقوم هذا التابع بفحص منطقية الرد قبل قبوله"""
        models = [
            (PRIMARY_MODEL, "gemini"), (FALLBACK_MODEL, "gemini"), 
            ("gemini-2.5-flash", "gemini"), ("command-r-plus-08-2024", "cohere"),
            (CLAUDE_MODEL, "claude")
        ]
        
        for m_name, m_type in models:
            logger.info(f"🤖 محاولة مع {m_name}...")
            try:
                if m_type == "gemini":
                    raw = self.gemini_client.models.generate_content(model=m_name, contents=prompt, config=genai_types.GenerateContentConfig(system_instruction=self.SYSTEM_PROMPT)).text
                elif m_type == "cohere":
                    if not self.cohere_client: continue
                    raw = self.cohere_client.chat(message=prompt, preamble=self.SYSTEM_PROMPT, model=m_name).text
                else:
                    if not self.claude_client: continue
                    raw = self.claude_client.messages.create(model=m_name, max_tokens=4000, system=self.SYSTEM_PROMPT, messages=[{"role": "user", "content": prompt}]).content[0].text
                
                parsed = self._parse_and_validate_logic(raw)
                if parsed:
                    logger.info(f"✅ تم قبول رد {m_name} منطقياً وتقنياً.")
                    return parsed
            except Exception as e:
                logger.warning(f"⚠️ فشل {m_name} أو الرد غير منطقي: {str(e)[:50]}")
                continue
                
        raise RuntimeError("فشلت كافة المحاولات في إنتاج سكريبت منطقي كامل.")

    def _parse_and_validate_logic(self, raw: str) -> Optional[dict]:
        """فحص جودة المحتوى قبل تمريره للبايبلاين"""
        try:
            cleaned = re.sub(r"^\x60{3}(?:json)?\s*", "", raw, flags=re.MULTILINE)
            cleaned = re.sub(r"\s*\x60{3}$", "", cleaned, flags=re.MULTILINE)
            data = json.loads(cleaned)
            
            # فحص منطقي: هل النصوص قصيرة جداً؟ (أقل من 10 حروف للشرح)
            for scene in data.get("ayah_scenes", []):
                if len(scene.get("intro_text", "")) < 5 or len(scene.get("explain_text", "")) < 10:
                    logger.warning("❌ رد مرفوض: نصوص الشرح قصيرة جداً وغير منطقية.")
                    return None
            return data
        except:
            return None

    def _fetch_verified_ayahs(self, info):
        # وظيفة جلب الآيات (مبسطة)
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = f"https://api.qurancdn.com/api/qdc/verses/by_key/{info['surah']}:{n}?words=false&fields=text_uthmani"
            resp = requests.get(url).json()
            ayahs.append(VerifiedAyah(surah=info["surah"], number=n, text=resp["verse"]["text_uthmani"], source="quran_api"))
        return ayahs

    def _build_script(self, ep_num, info, data, verified):
        v_map = {a.number: a for a in verified}
        ayah_scenes = [AyahScene(
            scene_id=10 + i, ayah=v_map[s["ayah_number"]],
            intro_text=s["intro_text"], explain_text=s["explain_text"],
            visual_prompt=s.get("visual_prompt", "Pixar style"),
            repetitions=3, duration_sec=35
        ) for i, s in enumerate(data["ayah_scenes"])]
        
        return EpisodeScript(
            episode_number=ep_num, surah_name=info["name"], surah_number=info["surah"],
            title=data["title"], youtube_title=data["title"], youtube_description=data["youtube_description"],
            youtube_tags=[], total_duration_sec=300,
            intro_scene=NarratorScene(scene_id=1, scene_type=SceneType.INTRO, duration_sec=25, narrator_text=data["intro_scene"]["narrator_text"], visual_prompt=data["intro_scene"].get("visual_prompt", "Pixar"), mood=AudioMood.INTRO),
            ayah_scenes=ayah_scenes, mid_scenes=[],
            outro_scene=NarratorScene(scene_id=99, scene_type=SceneType.OUTRO, duration_sec=25, narrator_text=data["outro_scene"]["narrator_text"], visual_prompt=data["outro_scene"].get("visual_prompt", "Pixar"), mood=AudioMood.OUTRO)
        )