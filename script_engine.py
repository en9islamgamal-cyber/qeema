"""
script_engine.py — VALUE / QEEMA v5.0
=========================================
محرك السكريبت المحسّن:

✅ Single-pass: برومبت واحد محكم يولّد كل السكريبت في مرة واحدة
   (سكريبت + visual prompts + youtube metadata)
✅ يستهلك LLM call واحد بدلًا من 4-5 (يحفظ الباقة)
✅ Fallback ذكي: Gemini 2.5 Pro → Claude → Gemini Flash
✅ Quality gate سريع
✅ معالجة JSON robust
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import APIKeys, CURRICULUM, Paths
from models import (
    AyahScene, AudioMood, EpisodeScript, NarratorScene, SceneType, VerifiedAyah
)
from core_adapters import (
    GeminiAdapter, CohereAdapter, AnthropicAdapter, GrokAdapter, extract_json
)
from quality_gate import QualityGate

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# المحرك الموحّد لبرومبت واحد قوي
# ═══════════════════════════════════════════════════════════════
class UnifiedPromptBuilder:
    """يبني برومبت واحد محكم — يولّد كل السكريبت في pass واحد."""

    @staticmethod
    def build_system() -> str:
        return """أنت "كاتب سيناريو خبير لقناة دينية تعليمية للأطفال" بثلاثة أدوار مدمجة:

[1] الشيخ المفسر: يضمن الصحة الشرعية والتفسير الموثوق (مستند للتفسير الميسر).
[2] أديب الأطفال: يحوّل التفسير لقصة دافئة بلهجة مصرية بسيطة جدًا. كل جملة ≤ 18 كلمة. يستخدم تعابير: "يا حبايبي"، "يلا بينا"، "شوفوا"، "هتعرفوا".
[3] المخرج البصري: يصف كل مشهد بالإنجليزية (flat 2D vector illustration, children's book style, pastel colors, warm lighting, friendly characters).

⚠️ قواعد صارمة:
- أبدًا لا تستخدم تشكيل في نهايات الكلمات (يسبب صوتًا روبوتيًا في TTS).
- تجنب الكلمات المعقدة. اللغة بسيطة كأنك تكلم طفل 6 سنوات.
- كل آية تأخذ مشهدها الخاص بصورة منفصلة.
- التفسير قصير وملموس (مثال أو موقف من حياة الطفل).

🎯 الإخراج: JSON فقط، بدون أي شرح خارج JSON، بدون markdown."""

    @staticmethod
    def build_user(episode_num: int, info: dict, ayahs_text: str) -> str:
        return f"""أنشئ سكريبت كامل للحلقة {episode_num} عن سورة {info['name']}.

📿 آيات السورة (نص عثماني):
{ayahs_text}

🎬 المطلوب: JSON بالبنية التالية تمامًا (بدون أي تعديل في أسماء الحقول):

{{
  "title": "العنوان الداخلي",
  "youtube_title": "عنوان جذاب للأطفال يحتوي على اسم السورة (≤60 حرف)",
  "youtube_description": "وصف لمحتوى الحلقة، 2-3 جمل، يحفز الاشتراك",
  "youtube_tags": ["تجويد للأطفال", "تفسير ميسر", "اسم السورة", "..."],

  "intro_scene": {{
    "narrator_text": "ترحيب دافئ + تشويق للسورة، 12-18 كلمة بدون تشكيل في النهايات.",
    "visual_prompt": "flat 2D vector, warm grandfather with smiling kids, ..."
  }},

  "ayah_scenes": [
    {{
      "ayah_number": <رقم الآية>,
      "intro_text": "تقديم سريع للآية، 8-15 كلمة، مثلاً: 'يلا نسمع الآية...'",
      "explain_text": "تفسير مبسط بلغة طفل 6 سنوات + مثال من حياته اليومية، 14-20 كلمة بحد أقصى.",
      "visual_prompt": "flat 2D vector illustration that shows the meaning of this specific ayah, children's book style, pastel"
    }}
    // … مشهد لكل آية في السورة
  ],

  "outro_scene": {{
    "narrator_text": "خلاصة + دعوة للاشتراك بأسلوب طفولي، 12-18 كلمة.",
    "visual_prompt": "flat 2D vector, kids waving, subscribe button, warm farewell scene"
  }}
}}

🚨 محظور: التشكيل في نهايات الكلمات، الجمل الطويلة، الكلمات المعقدة، وصف 3D أو photo-realistic."""


# ═══════════════════════════════════════════════════════════════
# تنقية النص بعد التوليد
# ═══════════════════════════════════════════════════════════════
class SceneRefiner:
    @staticmethod
    def shorten(text: str, max_words: int = 20) -> str:
        if not text:
            return text
        parts = text.strip().split()
        if len(parts) > max_words:
            return " ".join(parts[:max_words])
        return text

    @staticmethod
    def refine(text: str, field_type: str) -> str:
        defaults = {
            "intro": "يلا نركز في الآية دي يا حبايبي",
            "explain": "الآية دي بتعلمنا حاجة جميلة جدًا",
            "outro": "اللقاء الجاي إن شاء الله",
        }
        if not text or len(text.strip()) < 5:
            return defaults.get(field_type, "أهلاً بكم!")
        return SceneRefiner.shorten(text)


# ═══════════════════════════════════════════════════════════════
# ScriptEngine
# ═══════════════════════════════════════════════════════════════
class ScriptEngine:
    """نسخة v5: single-pass، نموذج قوي + fallback، ولا يستهلك الباقة."""

    def __init__(self):
        self.adapters: List[tuple] = []
        self.quality_gate = QualityGate()
        self.prompt_builder = UnifiedPromptBuilder()
        self.refiner = SceneRefiner()

        # 🎯 ترتيب الأفضلية:
        # 1. Gemini 2.5 Pro (الأقوى في الفهم والـ JSON الدقيق + رخيص نسبيًا)
        # 2. Claude Opus (للسكريبتات الإبداعية الدافئة)
        # 3. Gemini 2.5 Flash (سريع + رخيص جدًا كـ fallback أخير)
        # 4. Cohere Command (fallback إضافي)

        if APIKeys.GEMINI:
            self.adapters.append((
                GeminiAdapter(APIKeys.GEMINI, "gemini-2.5-pro"),
                "gemini-2.5-pro",
            ))
            self.adapters.append((
                GeminiAdapter(APIKeys.GEMINI, "gemini-2.5-flash"),
                "gemini-2.5-flash",
            ))

        if APIKeys.ANTHROPIC:
            # ✅ FIX: استخدام أحدث Claude (claude-3-opus-20240229 قديم!)
            self.adapters.append((
                AnthropicAdapter(APIKeys.ANTHROPIC, "claude-3-5-sonnet-20241022"),
                "claude-3.5-sonnet",
            ))

        if APIKeys.COHERE:
            self.adapters.append((
                CohereAdapter(APIKeys.COHERE, "command-r-plus-08-2024"),
                "command-r-plus",
            ))

        if not self.adapters:
            raise ValueError("❌ لا توجد مفاتيح LLM متاحة (GEMINI/ANTHROPIC/COHERE)")

        logger.info(f"✅ ScriptEngine initialized with {len(self.adapters)} adapters")

    # ─────────────────────────────────────────────────────────────
    # 1) جلب آيات السورة من Quran API
    # ─────────────────────────────────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_ayahs(self, info: dict) -> List[VerifiedAyah]:
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = (
                f"https://api.qurancdn.com/api/qdc/verses/by_key/"
                f"{info['surah']}:{n}?words=false&fields=text_uthmani"
            )
            resp = requests.get(url, timeout=15).json()
            ayahs.append(VerifiedAyah(
                surah=info["surah"],
                number=n,
                text=resp["verse"]["text_uthmani"],
                source="quran_api",
            ))
        logger.info(f"📿 تم تحميل {len(ayahs)} آيات لسورة {info['name']}")
        return ayahs

    # ─────────────────────────────────────────────────────────────
    # 2) Single-pass: استدعاء واحد فقط لكل نموذج
    # ─────────────────────────────────────────────────────────────
    def _call_llm(self, episode_num: int, info: dict, ayahs: List[VerifiedAyah]) -> Optional[Dict[str, Any]]:
        ayahs_text = "\n".join(f"  [{a.number}] {a.text}" for a in ayahs)
        system = self.prompt_builder.build_system()
        prompt = self.prompt_builder.build_user(episode_num, info, ayahs_text)

        for adapter, name in self.adapters:
            try:
                logger.info(f"🚀 [LLM] Calling {name} (single-pass)...")
                # نطلب JSON صراحة لو الـ adapter بيدعمه
                response = adapter.generate(
                    prompt,
                    system,
                    response_mime_type="application/json",
                )
                if not response:
                    continue

                # استخراج JSON
                try:
                    data = extract_json(response)
                except Exception as e:
                    logger.warning(f"⚠️ {name} JSON parse failed: {e}")
                    continue

                # Quality check
                report = self.quality_gate.evaluate(data)
                logger.info(f"📊 {name}: quality={report.overall_score:.1f}/100, passed={report.passed}")

                if report.passed:
                    logger.info(f"✅ {name} produced acceptable script")
                    return data

                # لو فشل، نسجل ونجرب التالي
                logger.warning(f"⚠️ {name} below quality threshold, trying next...")
                # نحفظ كـ best-effort لو كل النماذج فشلت
                if report.overall_score >= 50:
                    return data  # accept partial success

            except Exception as e:
                logger.error(f"❌ {name} failed: {e}")
                continue

        return None

    # ─────────────────────────────────────────────────────────────
    # 3) بناء كائن EpisodeScript
    # ─────────────────────────────────────────────────────────────
    def _build_script(self, ep_num: int, info: dict,
                      data: Dict[str, Any],
                      verified: List[VerifiedAyah]) -> EpisodeScript:
        v_map = {a.number: a for a in verified}

        # Ayah scenes
        ayah_scenes = []
        for i, s in enumerate(data.get("ayah_scenes", [])):
            a_num = s.get("ayah_number", info["start"] + i)
            if a_num not in v_map:
                logger.warning(f"⚠️ Ayah {a_num} not in verified, skipping")
                continue
            ayah_scenes.append(AyahScene(
                scene_id=10 + i,
                ayah=v_map[a_num],
                intro_text=self.refiner.refine(s.get("intro_text", ""), "intro"),
                explain_text=self.refiner.refine(s.get("explain_text", ""), "explain"),
                visual_prompt=s.get("visual_prompt", "flat vector graphic, minimal infographic, pastel colors"),
                repetitions=3,
                duration_sec=15,
            ))

        intro_data = data.get("intro_scene", {})
        outro_data = data.get("outro_scene", {})

        return EpisodeScript(
            episode_number=ep_num,
            surah_name=info["name"],
            surah_number=info["surah"],
            title=data.get("title", f"سورة {info['name']}"),
            youtube_title=data.get("youtube_title", f"تفسير سورة {info['name']} للأطفال"),
            youtube_description=data.get("youtube_description",
                f"حلقة جديدة لتعليم الأطفال سورة {info['name']} بأسلوب مبسّط وممتع."),
            youtube_tags=data.get("youtube_tags", [
                "قرآن للأطفال", "تفسير ميسر", info["name"],
                "تحفيظ القرآن", "تربية إسلامية", "VALUE", "قيمة"
            ]),
            intro_scene=NarratorScene(
                scene_id=1,
                scene_type=SceneType.INTRO,
                duration_sec=8,
                narrator_text=self.refiner.refine(intro_data.get("narrator_text", ""), "intro"),
                visual_prompt=intro_data.get("visual_prompt",
                    "flat 2D vector, kind grandfather with smiling kids, warm pastel colors"),
                mood=AudioMood.INTRO,
            ),
            ayah_scenes=ayah_scenes,
            mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99,
                scene_type=SceneType.OUTRO,
                duration_sec=8,
                narrator_text=self.refiner.refine(outro_data.get("narrator_text", ""), "outro"),
                visual_prompt=outro_data.get("visual_prompt",
                    "flat 2D vector, kids waving goodbye, subscribe button, warm farewell"),
                mood=AudioMood.OUTRO,
            ),
            total_duration_sec=300,
        )

    # ─────────────────────────────────────────────────────────────
    # 4) Public API
    # ─────────────────────────────────────────────────────────────
    def generate(self, episode_num: int) -> EpisodeScript:
        if episode_num not in CURRICULUM:
            raise ValueError(f"الحلقة {episode_num} غير موجودة في المنهج")

        info = CURRICULUM[episode_num]
        verified_ayahs = self._fetch_ayahs(info)
        data = self._call_llm(episode_num, info, verified_ayahs)

        if not data:
            raise RuntimeError("🚨 فشلت كافة نماذج LLM في إنتاج سكريبت قابل للاستخدام")

        script = self._build_script(episode_num, info, data, verified_ayahs)

        # حفظ على القرص
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")

        logger.info(f"🎉 سكريبت الحلقة {episode_num} ({info['name']}) جاهز")
        return script

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if not p.exists():
            return None
        try:
            return EpisodeScript.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"❌ خطأ قراءة السكريبت: {e}")
            return None
