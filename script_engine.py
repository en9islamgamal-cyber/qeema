from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, List, Union, Dict, Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import APIKeys, CURRICULUM, Paths
from models import (
    AyahScene, AudioMood, EpisodeScript, NarratorScene, SceneType, VerifiedAyah
)
from core_adapters import (
    GeminiAdapter, CohereAdapter, AnthropicAdapter, GrokAdapter
)
from quality_gate import QualityGate

logger = logging.getLogger(__name__)


class PromptBuilder:
    @staticmethod
    def get_system_prompt(surah_name: str) -> str:
        """
        هندسة الأوامر المتقدمة: لجنة إنتاج محتوى للأطفال مكونة من 3 شخصيات
        مع قوانين صارمة للإيقاع والصوت والوصف البصري.
        """
        return f"""أنت "لجنة إنتاج محتوى إبداعي للأطفال" متخصصة في سورة {surah_name}، وتتكون من 3 خبراء:

1. [الشيخ والعالم الأزهري]:
   - يضمن التفسير الصحيح، الدقيق، والموثوق للآيات، ويختر القيمة التربوية.
   - يعتمد على نص الآية كما يرد في المصادر الموثوقة.

2. [أديب وكاتب الأطفال]:
   - يحوّل التفسير إلى حكاية دافئة ومشوقة بلهجة مصرية بسيطة وراقية.
   - يستخدم كلمات مثل: "يا حبايبي"، "شوّفوا الجمال"، "يلا بينا نركز".
   - يمنع استخدام اللهجات الشامية أو السورية.
   - كل جملة لا تتجاوز 20 كلمة، ويُفضّل أن تكون 10–15 كلمة فقط لتسريع الإيقاع.

3. [خبير مخرج وإنفوجرافيك]:
   - يحوّل كل جملة/أداة سرد إلى وصف بصري بالإنجليزية.
   - النمط ثابت: "flat vector graphic, educational infographic style for kids, minimalist, clean solid pastel background, no text".
   - ممنوع استخدام كلمات مثل: Pixar, 3D, realistic, photograph, complex patterns.

[القواعد الصارمة للإنتاج]:
- **الإيقاع السريع (Micro‑segmentation)**:
  - مقاطع (intro_text) و(explain_text) لا تتجاوز 20 كلمة، ويفضّل 10–15 كلمة.
  - يُمنع إعادة نفس الجملة في أكثر من مكان.
- **سلامة TTS**:
  - ممنوع وضع التشكيل (الحركات) على أواخر الكلمات.
- **الهيكل الاحترافي**:
  - توليد JSON فقط، بدون أي نص قبل/بعد JSON.
  - تطابق البنية الدقيقة لـ EpisodeScript.
  - إذا لم تتمكن من توليد حقل: استخدم نصًا قصيرًا افتراضيًا متوافقًا مع السلامة.

[Few‑Shot Structure (الالتزام بالبنية لا المحتوى فقط) — يُستخدم هذا فقط كقالب]:
{{
  "title": "قصة سورة الإخلاص",
  "youtube_title": "تفسير سورة الإخلاص | إنفوجرافيك ممتع للأطفال",
  "youtube_description": "حلقة ممتعة نتعلم فيها عن سورة الإخلاص وأسرارها...",
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

مهمتك: توليد JSON فقط للسورة المطلوبة، ملتزمًا بالبنية أعلاه، وقواعد الإيقاع والسلامة."""

    @staticmethod
    def repair_prompt(old_text: str, critiques: List[str]) -> str:
        """
        إعادة كتابة JSON مع إصلاح الأخطاء التي حددها QualityGate.
        """
        # ✅ التصحيح: استخدام ثلاث علامات اقتباس للسلسلة متعددة الأسطر
        return f"""النص JSON أدناه يحتوي على بعض الأخطاء:
{chr(10).join(f'- {c}' for c in critiques)}

الرجاء إعادة كتابة JSON كامل، مع مراعاة أن:
- الجمل قصيرة جداً (10–20 كلمة كحد أقصى).
- تجنب التكرار.
- لا تستخدم تشكيل على أواخر الكلمات.
- حافظ على نفس البنية الدقيقة لـ EpisodeScript.

النص الحالي:
{old_text}"""


class SceneRefiner:
    """
    يُحسّن الإيقاع والطول في الجملة بعد توليد النموذج، دون تغيير المعنى.
    """
    @staticmethod
    def _shorten_text(text: str, max_words: int = 20) -> str:
        parts = text.strip().split()
        if len(parts) > max_words:
            return " ".join(parts[:max_words])
        return text

    @staticmethod
    def refine_scene(scene_text: str, field_type: str) -> str:
        if not scene_text:
            if field_type == "intro":
                return "يلا بينا نركز في الآية دي"
            elif field_type == "explain":
                return "الآية دي بتعلمنا حاجة عظيمة جدا"
            return "أهلاً بكم يا أبطال!"
        return SceneRefiner._shorten_text(scene_text)


class ScriptEngine:
    def __init__(self):
        self.adapters = []
        self.quality_gate = QualityGate()
        self.prompt_builder = PromptBuilder()
        self.scene_refiner = SceneRefiner()

        # إضافة النماذج المحتملة (تم تحديث أسماء النماذج)
        if os.getenv("GROK_API_KEY"):
            try:
                self.adapters.append((GrokAdapter(os.getenv("GROK_API_KEY")), "grok-4.20-beta-0309-non-reasoning"))
            except Exception as e:
                logger.warning("⚠️ Grok not available: %s", e)

        if APIKeys.GEMINI:
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-flash"))
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-pro"))

        if os.getenv("COHERE_API_KEY"):
            # تحديث النماذج Cohere إلى الإصدارات المتاحة
            self.adapters.append((CohereAdapter(os.getenv("COHERE_API_KEY")), "command-a-03-2025"))
            self.adapters.append((CohereAdapter(os.getenv("COHERE_API_KEY")), "command-r-plus-08-2024"))

        if os.getenv("ANTHROPIC_API_KEY"):
            self.adapters.append((AnthropicAdapter(os.getenv("ANTHROPIC_API_KEY")), "claude-3-opus-20240229"))

        if not self.adapters:
            raise ValueError("❌ لم يتم توفير أي مفاتيح API للنماذج.")

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        verified_ayahs = self._fetch_ayahs(info)

        base_prompt = f"""الحلقة: {episode_num} — سورة {info['name']}
السورة: {info['name']}
من الآية: {info['start']} إلى الآية: {info['end']}
المراجع القرآنية التي سيبني عليها الشيخ تفسيره:
{chr(10).join(f"[AYAH_{a.number}]" for a in verified_ayahs)}

مطلوب توليد JSON دقيق لحلقة للأطفال عن سورة {info['name']}.
اللغة الأساسية: العربية، مع ملاحظات بسيطة بالإنجليزية في الوصف البصري.
استخدم نمط لجنة الخبراء كما وصف في النظام.

أنت مُجبر بأن تُتبع أدق تفاصيل الهيكل والمعايير في الـQuality Gate."""

        system = self.prompt_builder.get_system_prompt(info["name"])
        raw_data = None

        for adapter, model_name in self.adapters:
            logger.info("🚀 استدعاء لجنة الخبراء عبر نموذج: %s", model_name)
            try:
                raw_data = adapter.generate(base_prompt, system, model_name)
                if not raw_data:
                    continue

                # 1. تقييم الجودة الأولي
                report = self.quality_gate.evaluate(raw_data)
                if report.passed:
                    logger.info("✅ تمرير أولي لـ %s", model_name)
                    break

                # 2. محاولة إصلاح ذاتي (Self‑Repair) للنموذج نفسه
                repair = self.prompt_builder.repair_prompt(raw_data, report.critiques)
                raw_data = adapter.generate(repair, system, model_name)
                if not raw_data:
                    continue

                report = self.quality_gate.evaluate(raw_data)
                if report.passed:
                    logger.info("🏆 تم إصلاح ذاتي عبر %s", model_name)
                    break
                else:
                    logger.warning("⚠️ فشل الإصلاح الذاتي لـ %s", model_name)
            except Exception as e:
                logger.error("❌ خطأ في نموذج %s: %s", model_name, str(e))
                continue

        if not raw_data:
            raise RuntimeError("🚨 انهيار المنظومة: لم تتمكن أي نموذج من إنتاج نص سليم.")

        script = self._build_script_object(episode_num, info, raw_data, verified_ayahs)

        # حفظ نسخة من السكريبت
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")

        logger.info("🎉 تم إنشاء الحلقة %s (%s) بنجاح.", episode_num, info["name"])
        return script

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_ayahs(self, info) -> List[VerifiedAyah]:
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = f"https://api.qurancdn.com/api/qdc/verses/by_key/{info['surah']}:{n}?words=false&fields=text_uthmani"
            try:
                resp = requests.get(url).json()
                ayah = VerifiedAyah(
                    surah=info["surah"],
                    number=n,
                    text=resp["verse"]["text_uthmani"],
                    source="quran_api"
                )
                ayahs.append(ayah)
            except Exception as e:
                logger.error("❌ فشل جلب الآية %s:%s: %s", info["surah"], n, str(e))
                raise
        return ayahs

    def _build_script_object(
        self,
        ep_num: int,
        info: Dict[str, Any],
        raw_json: Union[str, dict],
        verified: List[VerifiedAyah],
    ) -> EpisodeScript:
        v_map = {a.number: a for a in verified}
        data = raw_json if isinstance(raw_json, dict) else json.loads(raw_json)

        ayah_scenes = []

        raw_ayahs = data.get("ayah_scenes", [])
        for i, s in enumerate(raw_ayahs):
            a_num = s.get("ayah_number", info["start"] + i)
            if a_num not in v_map:
                continue

            aya = v_map[a_num]
            intro_text = str(s.get("intro_text", ""))
            explain_text = str(s.get("explain_text", ""))

            # إصلاح الإيقاع هنا (أقصر من 20 كلمة)
            intro_text = self.scene_refiner.refine_scene(intro_text, "intro")
            explain_text = self.scene_refiner.refine_scene(explain_text, "explain")

            default_vis = "flat vector graphic, minimal educational infographic, pastel colors, no text, children friendly"
            vis_prompt = str(s.get("visual_prompt", default_vis))

            ayah_scenes.append(
                AyahScene(
                    scene_id=10 + i,
                    ayah=aya,
                    intro_text=intro_text,
                    explain_text=explain_text,
                    visual_prompt=vis_prompt,
                    repetitions=3,
                    duration_sec=15,
                )
            )

        intro_data = data.get("intro_scene", {})
        outro_data = data.get("outro_scene", {})

        return EpisodeScript(
            episode_number=ep_num,
            surah_name=info["name"],
            surah_number=info["surah"],
            title=str(data.get("title", f"سورة {info['name']}")),
            youtube_title=str(data.get("youtube_title", f"تفسير سورة {info['name']} | إنفوجرافيك")),
            youtube_description=str(data.get("youtube_description", "")),
            youtube_tags=data.get("youtube_tags", []),
            intro_scene=NarratorScene(
                scene_id=1,
                scene_type=SceneType.INTRO,
                duration_sec=10,
                narrator_text=self.scene_refiner.refine_scene(
                    intro_data.get("narrator_text", "أهلاً بكم يا أبطال!"),
                    "intro",
                ),
                visual_prompt=str(intro_data.get("visual_prompt", "flat vector graphic, grandfather with kids")),
                mood=AudioMood.INTRO,
            ),
            ayah_scenes=ayah_scenes,
            mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99,
                scene_type=SceneType.OUTRO,
                duration_sec=10,
                narrator_text=self.scene_refiner.refine_scene(
                    outro_data.get("narrator_text", "إلى اللقاء في حلقة جديدة."),
                    "outro",
                ),
                visual_prompt=str(outro_data.get("visual_prompt", "flat vector graphic, waving goodbye")),
                mood=AudioMood.OUTRO,
            ),
            total_duration_sec=300,
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return EpisodeScript.model_validate(data)
            except Exception as e:
                logger.error("❌ خطأ في قراءة ملف السيناريو %s: %s", p, str(e))
                return None
        logger.info("💾 لا يوجد سيناريو محفوظ مسبقاً للحلقة %s", episode_num)
        return None