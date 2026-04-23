"""
script_engine.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
يحتوي على:
- Prompt Builder: هندسة أوامر معمارية.
- Model Registry & Fallback: محاولات ذكية مدعومة بـ Grok.
- Self-Repair: نظام إصلاح ذاتي بناءً على نقد Quality Gate.
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

from config import APIKeys, CURRICULUM, Paths
from models import (
    AyahScene, AudioMood, EpisodeScript, 
    NarratorScene, SceneType, VerifiedAyah
)
# استدعاء ملفات المنظومة الجديدة
from core_adapters import GeminiAdapter, CohereAdapter, AnthropicAdapter, GrokAdapter
from quality_gate import QualityGate

logger = logging.getLogger(__name__)

class PromptBuilder:
    @staticmethod
    def get_system_prompt() -> str:
        return """أنت "الجد أبو زياد"، عالم أزهري حنون ودافئ القلب، تروي قصصاً للأطفال (5-8 سنوات) لتفسير القرآن الكريم.

[القواعد المعمارية للإخراج]:
1. السرد القصصي المترابط: لا تقم بتفسير جاف. اربط الآيات بقصة واحدة متصلة وممتعة.
2. اللهجة والأداء: عامية مصرية راقية وبسيطة (يا حبايبي، يا أبطال، شوفوا الجمال).
3. التفصيل والعمق: يُمنع منعاً باتاً الإجابات القصيرة. يجب أن يكون التمهيد (intro_text) والشرح (explain_text) لكل آية عميقاً، ولا يقل عن 30 كلمة.
4. هندسة الصور: اكتب (visual_prompt) بالإنجليزية فقط بستايل (Pixar 3D animation, cute, Islamic kid friendly, highly detailed, cinematic lighting).
5. الأمانة: يُمنع كتابة نص الآيات القرآنية. استخدم المرجع [AYAH_X] فقط.

[Few-Shot JSON Example - التزم بهذا الحجم والهيكل]:
{
  "title": "قصة سورة الإخلاص",
  "youtube_title": "تفسير سورة الإخلاص للأطفال | الجد أبو زياد",
  "youtube_description": "حلقة ممتعة نتعلم فيها عن سورة الإخلاص...",
  "intro_scene": {
    "narrator_text": "يا هلا بأبطالي الحلوين! وحشتوني جداً. النهاردة يا حبايبي هنطير مع بعض في رحلة جميلة جداً نتعرف فيها على سورة عظيمة، سورة بتعلمنا مين هو ربنا سبحانه وتعالى، جاهزين؟",
    "visual_prompt": "Pixar 3D animation, a wise smiling grandfather with a white beard wearing Al-Azhar uniform, sitting in a cozy room surrounded by attentive cute kids, warm lighting."
  },
  "ayah_scenes": [
    {
      "ayah_number": 1,
      "intro_text": "تعالوا يا ولاد نفتح قلوبنا ونسمع أول آية بتركيز، ونشوف ربنا سبحانه وتعالى بيأمر النبي محمد صلى الله عليه وسلم يقولنا إيه...",
      "explain_text": "الآية دي يا حبايبي معناها عظيم جداً، ربنا بيقولنا إنه واحد بس، مفيش حد زيه، ولا في حد معاه بيشاركه في الكون العظيم ده، هو الخالق لكل شيء بنشوفه.",
      "visual_prompt": "Pixar 3D animation, beautiful glowing number one in a starry night sky, children looking up in wonder and awe."
    }
  ],
  "outro_scene": {
    "narrator_text": "شفتوا يا ولاد السورة دي جميلة إزاي؟ بتعلمنا إن ربنا عظيم وواحد. هستناكم الحلقة الجاية في حكاية جديدة وتفسير جديد، بحبكم في الله.",
    "visual_prompt": "Pixar 3D animation, grandfather warmly hugging the kids, waving goodbye at the camera, warm sunset lighting."
  }
}

مهمتك: توليد JSON فقط للسورة المطلوبة بناءً على المعايير أعلاه."""

class ScriptEngine:
    def __init__(self):
        # تهيئة واجهات النماذج (Adapters)
        self.adapters = []
        
        # 1. Grok هو المهاجم الأول (سريع، دقيق، ومناسب جداً للـ JSON)
        if os.getenv("GROK_API_KEY"):
            try:
                self.adapters.append((GrokAdapter(os.getenv("GROK_API_KEY")), "grok-2-latest"))
            except Exception as e:
                logger.warning(f"⚠️ لم يتم تهيئة Grok: {e}")
                
        # 2. خط الدفاع الثاني (Gemini)
        if APIKeys.GEMINI:
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-pro"))
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-flash"))
            
        # 3. خط الدفاع الثالث (Cohere)
        if os.getenv("COHERE_API_KEY"):
            self.adapters.append((CohereAdapter(os.getenv("COHERE_API_KEY")), "command-r-plus-08-2024"))
            self.adapters.append((CohereAdapter(os.getenv("COHERE_API_KEY")), "command-r-08-2024"))
            
        # 4. خط الدفاع الأخير (Claude)
        if os.getenv("ANTHROPIC_API_KEY"):
            self.adapters.append((AnthropicAdapter(os.getenv("ANTHROPIC_API_KEY")), "claude-3-opus-20240229"))

        if not self.adapters:
            raise ValueError("لم يتم توفير أي مفاتيح API للنماذج.")

        self.quality_gate = QualityGate()
        self.prompt_builder = PromptBuilder()

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        verified_ayahs = self._fetch_ayahs(info)
        ayah_refs = "\n".join([f"[AYAH_{a.number}]" for a in verified_ayahs])

        base_prompt = f"السورة المطلوبة: {info['name']}\nمن الآية: {info['start']} إلى الآية: {info['end']}\nالمراجع: {ayah_refs}"
        system_prompt = self.prompt_builder.get_system_prompt()

        raw_data = None

        # 1. محاولة التوليد عبر النماذج المتوفرة
        for adapter, model_name in self.adapters:
            logger.info(f"🚀 استدعاء النموذج: {model_name}...")
            try:
                raw_data = adapter.generate(base_prompt, system_prompt, model_name)

                # 2. تقييم الجودة
                report = self.quality_gate.evaluate(raw_data)

                # 3. الإصلاح الذاتي (Self-Repair) إذا فشل التقييم
                if not report.passed:
                    logger.warning(f"⚠️ النموذج {model_name} فشل في معايير الجودة. بدء عملية الإصلاح الذاتي...")
                    repair_prompt = f"لقد قمت بتوليد هذا الـ JSON سابقاً، ولكنه يحتوي على الأخطاء التالية:\n{chr(10).join(report.critiques)}\n\nالرجاء إعادة كتابة الـ JSON كاملاً مع إصلاح هذه الأخطاء، وتوسيع النصوص القصيرة لتصبح قصصية ومفصلة.\nالنص الأصلي المعيب:\n{raw_data}"
                    raw_data = adapter.generate(repair_prompt, system_prompt, model_name)
                    report = self.quality_gate.evaluate(raw_data)

                # إذا نجح بعد الإصلاح (أو من أول مرة)، نعتمد النتيجة
                if report.passed:
                    logger.info(f"🏆 تم اعتماد المخرج من {model_name} بنجاح.")
                    break
                else:
                    logger.error(f"❌ فشل الإصلاح الذاتي لنموذج {model_name}. ننتقل للنموذج التالي.")
                    raw_data = None # تصفير البيانات للمحاولة التالية

            except Exception as e:
                logger.error(f"خطأ في نموذج {model_name}: {str(e)}")
                continue

        if not raw_data:
            raise RuntimeError("🚨 انهيار المنظومة: فشلت كافة النماذج في تقديم محتوى يطابق معايير الجودة.")

        # 4. بناء الكائن النهائي
        script = self._build_script_object(episode_num, info, raw_data, verified_ayahs)

        # 5. الحفظ
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        return script

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_ayahs(self, info):
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = f"https://api.qurancdn.com/api/qdc/verses/by_key/{info['surah']}:{n}?words=false&fields=text_uthmani"
            resp = requests.get(url).json()
            ayahs.append(VerifiedAyah(surah=info["surah"], number=n, text=resp["verse"]["text_uthmani"], source="quran_api"))
        return ayahs

    def _build_script_object(self, ep_num, info, data, verified):
        v_map = {a.number: a for a in verified}
        ayah_scenes = []

        for i, s in enumerate(data.get("ayah_scenes", [])):
            a_num = s.get("ayah_number", info["start"] + i)
            if a_num in v_map:
                # وضعنا حماية بسيطة هنا لمنع انهيار Pydantic حتى لو عبر من Quality Gate
                intro_text = str(s.get("intro_text", ""))
                explain_text = str(s.get("explain_text", ""))
                
                ayah_scenes.append(AyahScene(
                    scene_id=10 + i, ayah=v_map[a_num],
                    intro_text=intro_text if len(intro_text) > 5 else "يا حبايب جدو ركزوا معايا في الآية دي...", 
                    explain_text=explain_text if len(explain_text) > 10 else "الآية دي بتعلمنا حاجات جميلة جداً لازم نحفظها في قلوبنا...",
                    visual_prompt=str(s.get("visual_prompt", "Pixar 3D animation, highly detailed")),
                    repetitions=3, duration_sec=35
                ))

        return EpisodeScript(
            episode_number=ep_num, surah_name=info["name"], surah_number=info["surah"],
            title=str(data.get("title", f"سورة {info['name']}")), 
            youtube_title=str(data.get("youtube_title", f"تفسير سورة {info['name']}")), 
            youtube_description=str(data.get("youtube_description", "")),
            youtube_tags=[], total_duration_sec=300,
            intro_scene=NarratorScene(
                scene_id=1, scene_type=SceneType.INTRO, duration_sec=25, 
                narrator_text=str(data.get("intro_scene", {}).get("narrator_text", "أهلاً بأبطالي!")), 
                visual_prompt=str(data.get("intro_scene", {}).get("visual_prompt", "Pixar 3D grandfather")), 
                mood=AudioMood.INTRO
            ),
            ayah_scenes=ayah_scenes, mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99, scene_type=SceneType.OUTRO, duration_sec=25,
                narrator_text=str(data.get("outro_scene", {}).get("narrator_text", "إلى اللقاء يا أبطال.")), 
                visual_prompt=str(data.get("outro_scene", {}).get("visual_prompt", "Pixar 3D grandfather waving")), 
                mood=AudioMood.OUTRO
            )
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
        return None