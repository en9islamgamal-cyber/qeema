"""
script_engine.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
يحتوي على:
- Prompt Builder: هندسة أوامر معمارية (لجنة الخبراء - Persona Ensemble).
- Pacing Control: تحكم صارم في إيقاع الجمل لمنع الملل البصري.
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
    def get_system_prompt(surah_name: str) -> str:
        """
        هندسة الأوامر المتقدمة: دمج 3 شخصيات احترافية مع قوانين صارمة للإيقاع والصوت
        """
        return f"""أنت لست مجرد ذكاء اصطناعي، أنت "لجنة إنتاج محتوى إبداعي للأطفال" تتكون من 3 خبراء يعملون معاً لإنتاج حلقة عن (سورة {surah_name}):

1. [شيخ وعالم أزهري]: يضمن التفسير الصحيح، الدقيق، والموثوق للآيات، ويستخرج القيمة التربوية المستهدفة.
2. [أديب وكاتب أطفال محترف]: يحول التفسير المعقد إلى حكاية دافئة وممتعة بشخصية "الجد أبو زياد". 
   - يستخدم **عامية مصرية راقية وبسيطة** (مثل: يا حبايبي، شوفوا الجمال، يلا بينا). 
   - يُمنع منعاً باتاً استخدام اللهجات الشامية أو السورية.
3. [خبير مخرج وإنفوجرافيك]: يحول كل جملة يكتبها الأديب إلى وصف بصري (Visual Prompt) بالإنجليزية.
   - الستايل المطلوب صارم: "flat vector graphic, educational infographic style for kids, minimalist, clean solid pastel background, no text".
   - (يُمنع تماماً استخدام كلمات مثل Pixar أو 3D أو realistic).

[القواعد الذهبية الحاكمة - أطِعها بصرامة]:
- **قضاءً على الملل البصري (Micro-segmentation):** الأطفال يملون من الصور الثابتة! لذلك يجب أن تكون مقاطع (intro_text) و (explain_text) "قصيرة جداً ومكثفة" (من 10 إلى 20 كلمة كحد أقصى للمقطع). هذا سيجبر مخرج الفيديو على تغيير صورة الإنفوجرافيك بسرعة.
- **حماية النطق الآلي (TTS Safety):** ممنوع منعاً باتاً وضع علامات التشكيل (الفتحة، الضمة، الكسرة، التنوين) على أواخر الكلمات، لضمان وقوف محرك الصوت البشري على سكون بشكل طبيعي.

[Few-Shot JSON Example - التزم بهذا الهيكل والإيقاع السريع]:
{{
  "title": "قصة سورة الإخلاص",
  "youtube_title": "تفسير سورة الإخلاص | إنفوجرافيك ممتع للأطفال",
  "youtube_description": "حلقة رائعة نتعلم فيها عن سورة الإخلاص...",
  "intro_scene": {{
    "narrator_text": "يا هلا بأبطالي الحلوين! جاهزين لرحلة جديدة في كتاب ربنا؟ يلا بينا نركز",
    "visual_prompt": "flat vector graphic, a wise grandfather pointing at a glowing map, cute diverse kids listening, infographic style, warm pastel background"
  }},
  "ayah_scenes": [
    {{
      "ayah_number": 1,
      "intro_text": "ربنا سبحانه وتعالى بيعلمنا في السورة دي حقيقة مهمة جدا، اسمعوا كده",
      "explain_text": "يعني ربنا واحد بس، مفيش حد زيه أبدا، وهو اللي خلق كل حاجة حلوة بنشوفها",
      "visual_prompt": "flat vector graphic, a beautiful glowing number one surrounded by stars and galaxies, minimalist islamic concept"
    }}
  ],
  "outro_scene": {{
    "narrator_text": "دي كانت حكايتنا النهاردة. هستناكم المرة الجاية يا حبايبي، بحبكم في الله",
    "visual_prompt": "flat vector graphic, grandfather smiling and waving goodbye, modern shapes floating, cheerful colors"
  }}
}}

مهمتك: توليد JSON فقط للسورة المطلوبة بناءً على خبرات اللجنة والقواعد أعلاه."""

class ScriptEngine:
    def __init__(self):
        # تهيئة واجهات النماذج (Adapters)
        self.adapters = []

        if os.getenv("GROK_API_KEY"):
            try:
                self.adapters.append((GrokAdapter(os.getenv("GROK_API_KEY")), "grok-2-latest"))
            except Exception as e:
                logger.warning(f"⚠️ لم يتم تهيئة Grok: {e}")

        if APIKeys.GEMINI:
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-pro"))
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-flash"))

        if os.getenv("COHERE_API_KEY"):
            self.adapters.append((CohereAdapter(os.getenv("COHERE_API_KEY")), "command-r-plus-08-2024"))
            self.adapters.append((CohereAdapter(os.getenv("COHERE_API_KEY")), "command-r-08-2024"))

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

        # حقن الجزء المتغير (المعلومات الدقيقة)
        base_prompt = f"السورة المطلوبة: {info['name']}\nمن الآية: {info['start']} إلى الآية: {info['end']}\nالمراجع القرآنية التي سيبني عليها الشيخ تفسيره:\n{ayah_refs}"
        
        # حقن الجزء الثابت (لجنة الخبراء)
        system_prompt = self.prompt_builder.get_system_prompt(info['name'])

        raw_data = None

        for adapter, model_name in self.adapters:
            logger.info(f"🚀 استدعاء لجنة الخبراء عبر نموذج: {model_name}...")
            try:
                raw_data = adapter.generate(base_prompt, system_prompt, model_name)

                report = self.quality_gate.evaluate(raw_data)

                if not report.passed:
                    logger.warning(f"⚠️ النموذج {model_name} فشل في معايير الجودة. توجيه أمر الإصلاح الذاتي...")
                    repair_prompt = f"الـ JSON المولد يحتوي على الأخطاء التالية:\n{chr(10).join(report.critiques)}\n\nالرجاء إعادة كتابة الـ JSON كاملاً مع إصلاح الأخطاء. تأكد أن الجمل قصيرة جداً (أقل من 20 كلمة) لتسريع وتيرة الفيديو، وأن الصور بستايل إنفوجرافيك مسطح.\nالنص الأصلي المعيب:\n{raw_data}"
                    raw_data = adapter.generate(repair_prompt, system_prompt, model_name)
                    report = self.quality_gate.evaluate(raw_data)

                if report.passed:
                    logger.info(f"🏆 تم اعتماد إبداع لجنة الخبراء من {model_name} بنجاح.")
                    break
                else:
                    logger.error(f"❌ فشل الإصلاح الذاتي لنموذج {model_name}. ننتقل للنموذج التالي.")
                    raw_data = None 

            except Exception as e:
                logger.error(f"خطأ في نموذج {model_name}: {str(e)}")
                continue

        if not raw_data:
            raise RuntimeError("🚨 انهيار المنظومة: فشلت كافة النماذج في تقديم محتوى يطابق معايير اللجنة.")

        script = self._build_script_object(episode_num, info, raw_data, verified_ayahs)

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
                intro_text = str(s.get("intro_text", ""))
                explain_text = str(s.get("explain_text", ""))
                # ضمان وجود وصف إنفوجرافيك حتى لو فشل الموديل في توليده
                default_vis = "flat vector graphic, minimal educational infographic style, pastel colors"
                vis_prompt = str(s.get("visual_prompt", default_vis))

                ayah_scenes.append(AyahScene(
                    scene_id=10 + i, ayah=v_map[a_num],
                    intro_text=intro_text if len(intro_text) > 5 else "يلا بينا نركز في الآية دي", 
                    explain_text=explain_text if len(explain_text) > 10 else "الآية دي بتعلمنا حاجة عظيمة جدا",
                    visual_prompt=vis_prompt,
                    repetitions=3, duration_sec=15 # قللنا التوقع الزمني ليتناسب مع الجمل القصيرة
                ))

        return EpisodeScript(
            episode_number=ep_num, surah_name=info["name"], surah_number=info["surah"],
            title=str(data.get("title", f"سورة {info['name']}")), 
            youtube_title=str(data.get("youtube_title", f"تفسير سورة {info['name']} | إنفوجرافيك")), 
            youtube_description=str(data.get("youtube_description", "")),
            youtube_tags=[], total_duration_sec=300,
            intro_scene=NarratorScene(
                scene_id=1, scene_type=SceneType.INTRO, duration_sec=10, 
                narrator_text=str(data.get("intro_scene", {}).get("narrator_text", "أهلاً بكم يا أبطال!")), 
                visual_prompt=str(data.get("intro_scene", {}).get("visual_prompt", "flat vector graphic, grandfather with kids")), 
                mood=AudioMood.INTRO
            ),
            ayah_scenes=ayah_scenes, mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99, scene_type=SceneType.OUTRO, duration_sec=10,
                narrator_text=str(data.get("outro_scene", {}).get("narrator_text", "إلى اللقاء في حلقة جديدة.")), 
                visual_prompt=str(data.get("outro_scene", {}).get("visual_prompt", "flat vector graphic, waving goodbye")), 
                mood=AudioMood.OUTRO
            )
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
        return None
