"""
script_engine.py — VALUE / QEEMA v5.0 (Optimized & Self-Healing)
================================================================
[CHANGELOG v5.0]
- Smart Payload Truncation: إخفاء الأمثلة الطويلة (Few-shot) تلقائياً عند استخدام Groq لتجنب حدود TPM.
- Robust JSON Parsing: دالة مدمجة لاصطياد الـ JSON وإزالة علامات الماركداون (Markdown) لتجنب انهيار Cohere.
- Model Upgrade: الانتقال إلى gemini-2.5-flash لسرعة وحصة مجانية (Quota) أعلى بكثير.
- Bug Fix: تمرير قاموس (dict) بدلاً من نص (str) إلى دالة البناء _build_script_object.
"""

from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, Dict, Any
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import APIKeys, CURRICULUM, Paths
from models import (AyahScene, AudioMood, EpisodeScript,
                    NarratorScene, SceneType, VerifiedAyah)
from core_adapters import GeminiAdapter, CohereAdapter, GroqAdapter
from quality_gate import QualityGate

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Style Bible — مرجع الاتساق البصري الكامل
# ══════════════════════════════════════════════════════════════════
STYLE_BIBLE = """
[VISUAL CONSISTENCY BIBLE — MUST FOLLOW IN EVERY visual_prompt]:

CHARACTER — ABU ZIYAD (الجد أبو زياد):
  Elderly Egyptian grandfather, 70s, warm kind face with deep smile wrinkles,
  long full white beard neatly groomed, white Al-Azhar turban,
  flowing white Egyptian thobe, warm honey-brown eyes full of wisdom.

CHILDREN (الأحفاد):
  2-3 children aged 4-7, Arabic features, cozy warm pajamas (mint green, dusty pink),
  wide curious bright eyes, sitting cross-legged around grandfather.

SETTING — THE GRANDFATHER'S STUDY:
  Warm intimate Islamic study room, late evening, wooden bookshelves,
  Arabic calligraphy on warm ochre walls, hand-woven geometric carpet,
  brass oil lanterns casting honeyed amber glow.

RENDER QUALITY:
  Pixar/DreamWorks quality 3D CGI, ultra-detailed, cinematic lighting, 4K, no text.
"""

class PromptBuilder:
    @staticmethod
    def get_system_prompt(use_few_shot: bool = True) -> str:
        base_instructions = f"""أنت "الجد أبو زياد"، حكّاء قرآني بارع وعالم أزهري حنون.
أنت جد مصري يجلس مع أحفاده الصغار (5-8 سنوات) ليحكي لهم قبل النوم.

{STYLE_BIBLE}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[قواعد السرد — يُطبَّق على كل مشهد]:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. VISUAL PROMPT:
   ✦ يبدأ بـ: "Pixar/DreamWorks quality 3D CGI render —"
   ✦ يصف أبو زياد والأطفال والبيئة بدقة من STYLE BIBLE
   ✦ لا يقل عن 80 كلمة إنجليزية.

2. NARRATOR TEXT:
   ✦ عامية مصرية راقية ودافئة جداً. بدون طلب اشتراك بالقناة.

3. INTRO/EXPLAIN TEXT:
   ✦ intro_text: ينتهي بـ "تعالوا نغمض عينينا ونسمع ربنا بيقول..."
   ✦ explain_text: يبدأ بـ "صدق الله العظيم" ثم شرح مبسط.

4. CRITICAL JSON RULES (STRICT STRICT STRICT):
   ✦ YOU MUST OUTPUT ONLY A VALID JSON OBJECT.
   ✦ DO NOT WRAP IN ```json MARKDOWN.
   ✦ NO CONVERSATIONAL TEXT BEFORE OR AFTER THE JSON.
"""
        
        few_shot_example = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[FEW-SHOT EXAMPLE]:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "title": "حكاية سورة الإخلاص",
  "youtube_title": "حواديت الجد أبو زياد | سورة الإخلاص 🌙",
  "youtube_description": "حكاية سورة الإخلاص مع الجد أبو زياد...",
  "intro_scene": {
    "narrator_text": "يا هلا بحبايبي وأبطالي الصغيرين! جه وقت الحواديت!",
    "visual_prompt": "Pixar/DreamWorks quality 3D CGI render — wide establishing shot of Abu Ziyad's warm Islamic study room..."
  },
  "ayah_scenes": [
    {
      "ayah_number": 1,
      "intro_text": "يا حبايبي، تعالوا نغمض عينينا ونسمع ربنا بيقول...",
      "explain_text": "صدق الله العظيم. الآية دي بتقولنا إن ربنا واحد بس...",
      "visual_prompt": "Pixar/DreamWorks quality 3D CGI render — medium shot, Abu Ziyad pointing up..."
    }
  ],
  "outro_scene": {
    "narrator_text": "الحمد لله يا حبايبي. تصبحوا على خير يا أبطالي.",
    "visual_prompt": "Pixar/DreamWorks quality 3D CGI render — Abu Ziyad tucking children into bed..."
  }
}
"""
        # دمج المثال إذا كان مسموحاً (لترشيد استهلاك التوكنز في Groq)
        return base_instructions + (few_shot_example if use_few_shot else "")


class ScriptEngine:
    def __init__(self):
        self.adapters = []

        # 1. Groq
        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            try:
                self.adapters.append((GroqAdapter(groq_key), "llama-3.3-70b-versatile"))
                logger.info("✅ [1] Groq → llama-3.3-70b-versatile")
            except Exception as e:
                logger.warning(f"⚠️ Groq: {e}")

        # 2. Gemini (Upgraded to Flash for better rate limits & speed)
        gemini_keys = [
            ("GEMINI_API_KEY",   "Gmail-1"),
            ("GEMINI_API_KEY_2", "Gmail-2"),
            ("GEMINI_API_KEY_3", "Gmail-3"),
        ]
        for slot, (env_var, label) in enumerate(gemini_keys, start=2):
            key = os.getenv(env_var) or (APIKeys.GEMINI if env_var == "GEMINI_API_KEY" else None)
            if key:
                try:
                    self.adapters.append((GeminiAdapter(key), "gemini-2.5-flash"))
                    logger.info(f"✅ [{slot}] Gemini [{label}] → gemini-2.5-flash")
                except Exception as e:
                    logger.warning(f"⚠️ Gemini [{label}]: {e}")

        # 3. Cohere
        cohere_key = os.getenv("COHERE_API_KEY")
        if cohere_key:
            self.adapters.append((CohereAdapter(cohere_key), "command-r-plus-08-2024"))
            self.adapters.append((CohereAdapter(cohere_key), "command-r-08-2024"))

        if not self.adapters:
            raise ValueError("❌ لم يتم توفير أي مفاتيح API.")

        logger.info(f"✅ Fallback chain جاهز — {len(self.adapters)} نموذج")
        self.quality_gate = QualityGate()
        self.prompt_builder = PromptBuilder()

    def _extract_clean_json(self, text: str) -> Dict[str, Any]:
        """دالة قوية لاستخراج JSON مهما أضاف النموذج من تعليقات أو ماركداون."""
        try:
            cleaned = re.sub(r"
http://googleusercontent.com/immersive_entry_chip/0
