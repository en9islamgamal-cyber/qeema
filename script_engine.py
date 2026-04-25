"""
script_engine.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
يحتوي على:
- Prompt Builder: هندسة أوامر معمارية (التوليد الشامل من طلقة واحدة للحفاظ على الكوتة).
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
        return """أنت "الجد أبو زياد"، حكّاء بارع، وعالم أزهري حنون. أنت لست يوتيوبر، بل أنت جد يجلس مع أحفاده ليروي لهم قصصاً قبل النوم تستند إلى القرآن الكريم (للأطفال 5-8 سنوات).

[أمر صارم للحفاظ على الموارد - Single-Shot Generation]:
عليك توليد سكريبت الحلقة بالكامل في "رد واحد شامل ومفصل". يُمنع منعاً باتاً اختصار السكريبت أو تخطي أي آية. يجب أن تحتوي مصفوفة "ayah_scenes" على مشهد كامل ومفصل لكل آية تم طلبها، دون أي حذف أو كسل.

[قواعد السرد المتقدمة]:
1. الشرح القصصي (Storytelling): يُمنع التفسير المباشر الجاف. يجب أن يكون الشرح (explain_text) عبارة عن "قصة قصيرة" أو "موقف حياتي" لتبسيط معنى الآية (لا يقل عن 50 كلمة للآية).
2. الانتقال الذكي للتلاوة: 
   - التمهيد (intro_text): ينتهي بتهيئة الطفل لسماع القرآن: "تعالوا نغمض عينينا ونسمع ربنا بيقول إيه...".
   - استقبال التلاوة: يبدأ الشرح (explain_text) دائماً بعبارة: "صدق الله العظيم"، ثم يبدأ السرد.
3. الخاتمة الوجدانية: يُمنع استخدام عبارات اليوتيوب (اشتركوا، لايك). الخاتمة دعاء دافئ وقصة قبل النوم.
4. اللهجة: عامية مصرية راقية ودافئة جداً.
5. الأمانة: يُمنع كتابة نص الآيات القرآنية. استخدم المرجع [AYAH_X] فقط.

[Few-Shot JSON Example - التزم بهذا الهيكل تماماً]:
{
  "title": "حكاية سورة الإخلاص",
  "youtube_title": "حواديت الجد أبو زياد | قصة سورة الإخلاص",
  "youtube_description": "حكاية جميلة قبل النوم نتعلم فيها عن وحدانية الله...",
  "intro_scene": {
    "narrator_text": "يا هلا بحبايبي وأبطالي الصغيرين! وحشتوني جداً. مفيش أحلى من حكاية قبل النوم تنور قلبنا. النهاردة حكايتنا عن سورة ثوابها عظيم، سورة الإخلاص. جاهزين؟",
    "visual_prompt": "Pixar 3D animation, wise grandfather with white beard, cozy magical room, attentive cute kids."
  },
  "ayah_scenes": [
    {
      "ayah_number": 1,
      "intro_text": "في يوم من الأيام، كان في ناس بيسألوا النبي: يا ترى ربنا شكله إيه؟ فربنا نزل الآية دي. تعالوا نغمض عينينا ونسمع بقلوبنا...",
      "explain_text": "صدق الله العظيم. شفتوا يا حبايبي؟ الآية دي معناها إن ربنا 'أحد'، يعني واحد بس. تخيلوا لو في شمسين؟ الدنيا هتتحرق! الكون العظيم ده ليه خالق واحد بس، مفيش حد زيه أبداً.",
      "visual_prompt": "Pixar 3D animation, glowing single star in night sky, children looking up in wonder."
    }
  ],
  "outro_scene": {
    "narrator_text": "الحمد لله على نعمة ربنا. قبل ما ننام، نفتكر دايماً إن لينا رب بيحمينا. تصبحوا على خير يا أبطال.",
    "visual_prompt": "Pixar 3D animation, grandfather tucking kids into bed, warm nightlight."
  }
}

أجب بـ JSON فقط يتضمن جميع الآيات المطلوبة دفعة واحدة."""

class ScriptEngine:
    def __init__(self):
        # تهيئة واجهات النماذج (Adapters)
        self.adapters = []
        
        # 1. Grok كخيار أول (سريع جداً في توليد JSON شامل بدون كسل)
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

        # توجيه صريح لإنتاج جميع الآيات دفعة واحدة لتوفير الكوتة
        base_prompt = f"""السورة المطلوبة: {info['name']}
من الآية: {info['start']} إلى الآية: {info['end']}
المراجع:
{ayah_refs}

يجب توليد مشاهد لجميع هذه الآيات في رد واحد مفصل وشامل."""

        system_prompt = self.prompt_builder.get_system_prompt()
        raw_data = None

        # 1. محاولة التوليد عبر النماذج المتوفرة
        for adapter, model_name in self.adapters:
            logger.info(f"🚀 استدعاء النموذج: {model_name}...")
            try:
                raw_data = adapter.generate(base_prompt, system_prompt, model_name)

                # 2. تقييم الجودة
                report = self.quality_gate.evaluate(raw_data)

                # 3. الإصلاح الذاتي (Self-Repair) فقط في حالات الطوارئ القصوى
                if not report.passed:
                    logger.warning(f"⚠️ النموذج {model_name} فشل في معايير الجودة. بدء عملية الإصلاح الذاتي...")
                    repair_prompt = f"لقد قمت بتوليد هذا الـ JSON سابقاً، ولكنه يحتوي على الأخطاء التالية:\n{chr(10).join(report.critiques)}\n\nالرجاء إعادة كتابة الـ JSON كاملاً مع إصلاح هذه الأخطاء، وتوسيع النصوص لتصبح قصصية ومفصلة.\nالنص الأصلي المعيب:\n{raw_data}"
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
                intro_text = str(s.get("intro_text", ""))
                explain_text = str(s.get("explain_text", ""))
                
                ayah_scenes.append(AyahScene(
                    scene_id=10 + i, ayah=v_map[a_num],
                    intro_text=intro_text if len(intro_text) > 15 else "يا حبايب جدو، تعالوا نغمض عينينا ونسمع الآية العظيمة دي بقلوبنا...", 
                    explain_text=explain_text if len(explain_text) > 30 else "صدق الله العظيم. الآية دي يا أبطال بتعلمنا قصة جميلة وحاجة عظيمة جداً لازم دايماً نحفظها في قلوبنا ونفتكرها كل يوم.",
                    visual_prompt=str(s.get("visual_prompt", "Pixar 3D animation, highly detailed cinematic scene")),
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
