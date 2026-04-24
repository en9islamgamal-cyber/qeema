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
        return f"""أنت "لجنة إنتاج محتوى إبداعي للأطفال" متخصصة في سورة {surah_name}، وتتكون من 3 خبراء:

1. [الشيخ والعالم الأزهري]:
   - يضمن التفسير الصحيح والموثوق.
2. [أديب وكاتب الأطفال]:
   - يحوّل التفسير إلى حكاية دافئة بلهجة مصرية بسيطة.
   - يستخدم كلمات مثل: "يا حبايبي"، "شوّفوا الجمال".
   - كل جملة لا تتجاوز 20 كلمة.
3. [خبير مخرج وإنفوجرافيك]:
   - يصف المشاهد بالإنجليزية، النمط: "flat vector graphic, educational infographic style for kids, pastel background".

مهمتك: توليد JSON فقط يطابق البنية المطلوبة لـ EpisodeScript."""

    @staticmethod
    def repair_prompt(old_text: str, critiques: List[str]) -> str:
        # ✅ تم إصلاح الخطأ: استخدام ثلاث علامات اقتباس
        return f"""النص JSON أدناه يحتوي على بعض الأخطاء:
{chr(10).join(f'- {c}' for c in critiques)}

الرجاء إعادة كتابة JSON كامل مع مراعاة:
- جمل قصيرة جداً (10–20 كلمة).
- تجنب التشكيل على أواخر الكلمات.
- الحفاظ على نفس البنية.

النص الحالي:
{old_text}"""


class SceneRefiner:
    @staticmethod
    def _shorten_text(text: str, max_words: int = 20) -> str:
        parts = text.strip().split()
        if len(parts) > max_words:
            return " ".join(parts[:max_words])
        return text

    @staticmethod
    def refine_scene(scene_text: str, field_type: str) -> str:
        if not scene_text:
            defaults = {"intro": "يلا بينا نركز في الآية دي", "explain": "الآية دي بتعلمنا حاجة عظيمة جدا"}
            return defaults.get(field_type, "أهلاً بكم!")
        return SceneRefiner._shorten_text(scene_text)


class ScriptEngine:
    def __init__(self):
        self.adapters = []
        self.quality_gate = QualityGate()
        self.prompt_builder = PromptBuilder()
        self.scene_refiner = SceneRefiner()

        if os.getenv("GROK_API_KEY"):
            self.adapters.append((GrokAdapter(os.getenv("GROK_API_KEY")), "grok-4.20-beta-0309-non-reasoning"))
        if APIKeys.GEMINI:
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-flash"))
            self.adapters.append((GeminiAdapter(APIKeys.GEMINI), "gemini-2.5-pro"))
        if os.getenv("COHERE_API_KEY"):
            self.adapters.append((CohereAdapter(os.getenv("COHERE_API_KEY")), "command-a-03-2025"))
        if os.getenv("ANTHROPIC_API_KEY"):
            self.adapters.append((AnthropicAdapter(os.getenv("ANTHROPIC_API_KEY")), "claude-3-opus-20240229"))

        if not self.adapters:
            raise ValueError("❌ لم يتم توفير أي مفاتيح API للنماذج.")

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        verified_ayahs = self._fetch_ayahs(info)

        base_prompt = f"""الحلقة: {episode_num} — سورة {info['name']}
المراجع القرآنية:
{chr(10).join(f'[AYAH_{a.number}]' for a in verified_ayahs)}

مطلوب توليد JSON دقيق لحلقة للأطفال عن سورة {info['name']}."""
        system = self.prompt_builder.get_system_prompt(info["name"])
        raw_data = None

        for adapter, model_name in self.adapters:
            logger.info("🚀 استدعاء نموذج: %s", model_name)
            try:
                raw_data = adapter.generate(base_prompt, system)
                if not raw_data:
                    continue
                report = self.quality_gate.evaluate(raw_data)
                if report.passed:
                    logger.info("✅ تمرير أولي لـ %s", model_name)
                    break
                repair = self.prompt_builder.repair_prompt(raw_data, report.critiques)
                raw_data = adapter.generate(repair, system)
                if raw_data and self.quality_gate.evaluate(raw_data).passed:
                    logger.info("🏆 تم إصلاح ذاتي عبر %s", model_name)
                    break
            except Exception as e:
                logger.error("❌ خطأ في نموذج %s: %s", model_name, str(e))
        if not raw_data:
            raise RuntimeError("🚨 فشلت كافة النماذج في إنتاج نص سليم.")
        script = self._build_script_object(episode_num, info, raw_data, verified_ayahs)
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        logger.info("🎉 تم إنشاء الحلقة %s (%s)", episode_num, info["name"])
        return script

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_ayahs(self, info) -> List[VerifiedAyah]:
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = f"https://api.qurancdn.com/api/qdc/verses/by_key/{info['surah']}:{n}?words=false&fields=text_uthmani"
            resp = requests.get(url).json()
            ayahs.append(VerifiedAyah(surah=info["surah"], number=n, text=resp["verse"]["text_uthmani"], source="quran_api"))
        return ayahs

    def _build_script_object(self, ep_num, info, raw_json, verified) -> EpisodeScript:
        v_map = {a.number: a for a in verified}
        data = raw_json if isinstance(raw_json, dict) else json.loads(raw_json)
        ayah_scenes = []
        for i, s in enumerate(data.get("ayah_scenes", [])):
            a_num = s.get("ayah_number", info["start"] + i)
            if a_num not in v_map:
                continue
            aya = v_map[a_num]
            intro_text = self.scene_refiner.refine_scene(s.get("intro_text", ""), "intro")
            explain_text = self.scene_refiner.refine_scene(s.get("explain_text", ""), "explain")
            vis = s.get("visual_prompt", "flat vector graphic, minimal infographic, pastel colors")
            ayah_scenes.append(AyahScene(scene_id=10+i, ayah=aya, intro_text=intro_text, explain_text=explain_text, visual_prompt=vis, repetitions=3, duration_sec=15))

        intro_data = data.get("intro_scene", {})
        outro_data = data.get("outro_scene", {})
        return EpisodeScript(
            episode_number=ep_num,
            surah_name=info["name"],
            surah_number=info["surah"],
            title=data.get("title", f"سورة {info['name']}"),
            youtube_title=data.get("youtube_title", f"تفسير سورة {info['name']}"),
            youtube_description=data.get("youtube_description", ""),
            youtube_tags=data.get("youtube_tags", []),
            intro_scene=NarratorScene(scene_id=1, scene_type=SceneType.INTRO, duration_sec=10, narrator_text=self.scene_refiner.refine_scene(intro_data.get("narrator_text", "أهلاً بكم!"), "intro"), visual_prompt=intro_data.get("visual_prompt", "flat vector graphic, grandfather with kids"), mood=AudioMood.INTRO),
            ayah_scenes=ayah_scenes,
            mid_scenes=[],
            outro_scene=NarratorScene(scene_id=99, scene_type=SceneType.OUTRO, duration_sec=10, narrator_text=self.scene_refiner.refine_scene(outro_data.get("narrator_text", "إلى اللقاء."), "outro"), visual_prompt=outro_data.get("visual_prompt", "flat vector graphic, waving goodbye"), mood=AudioMood.OUTRO),
            total_duration_sec=300,
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            try:
                return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
            except Exception as e:
                logger.error("❌ خطأ في قراءة ملف السيناريو: %s", e)
        return None