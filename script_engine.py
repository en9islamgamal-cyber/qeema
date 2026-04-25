"""
script_engine.py — VALUE / QEEMA v4.0
======================================
[CHANGELOG v4.0]
- Style Bible: وصف ثابت وتفصيلي لكل عنصر مرئي (شخصية، مكان، إضاءة، لوحة ألوان)
  يُضمَّن في كل visual_prompt لضمان الاتساق الكامل بين جميع المشاهد.
- Detailed Prompts: النموذج ملزم بتوليد visual_prompt لا يقل عن 120 كلمة لكل مشهد.
- Single-Shot: توليد السكريبت كاملاً في استدعاء واحد للحفاظ على الكوتة.
"""
from __future__ import annotations
import json, logging, os
from pathlib import Path
from typing import Optional
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
# يُضمَّن في كل prompt لضمان أن كل صورة تبدو من نفس العالم
# ══════════════════════════════════════════════════════════════════
STYLE_BIBLE = """
[VISUAL CONSISTENCY BIBLE — MUST FOLLOW IN EVERY visual_prompt]:

CHARACTER — ABU ZIYAD (الجد أبو زياد):
  Elderly Egyptian grandfather, 70s, warm kind face with deep smile wrinkles,
  long full white beard neatly groomed, white Al-Azhar turban (عمامة بيضاء),
  flowing white Egyptian thobe (جلباب أبيض), warm honey-brown eyes full of wisdom,
  slightly stocky build, large gentle hands often gesturing warmly.

CHILDREN (الأحفاء):
  2-3 children aged 4-7, Arabic features, cozy warm pajamas in soft colors
  (mint green, dusty pink, pale blue), wide curious bright eyes, sitting cross-legged
  on the carpet around grandfather, expressions of wonder and joy.

SETTING — THE GRANDFATHER'S STUDY (حجرة الجد):
  Warm intimate Islamic study room, late evening atmosphere,
  wooden bookshelves lined with leather-bound Qurans and old books,
  Arabic calligraphy tapestries on warm ochre plastered walls,
  hand-woven geometric carpet in deep reds and blues,
  wooden mashrabiya window lattice with moonlight filtering through,
  brass oil lanterns casting honeyed amber glow,
  large floor cushions in deep teal and burgundy velvet,
  a small carved wooden Quran stand (رحل) at center,
  subtle Islamic geometric tile border at floor level.

LIGHTING:
  Primary: warm amber candlelight (3000K) from brass lanterns,
  Fill: soft cool moonlight blue from mashrabiya window,
  Accent: golden rim light outlining characters,
  Atmosphere: volumetric light rays, floating dust motes, cozy intimate glow.

COLOR PALETTE (must dominate every scene):
  Warm amber #F5A623, Deep teal #1A6B7A, Ivory #F5F0E6,
  Rich crimson #8B1A2F, Antique gold #C9A84C, Warm brown #7B4F2E.

RENDER QUALITY (non-negotiable):
  Pixar/DreamWorks quality 3D CGI, ultra-detailed subsurface skin scattering,
  ray-traced global illumination, 4K texture maps, cinematic depth of field,
  soft film grain, professional color grading, studio render.

CAMERA RULES:
  - Intro/Outro: wide establishing shot showing full room and all characters
  - Ayah intro narration: medium shot of grandfather leaning toward children
  - Quran recitation: close-up on grandfather's face, eyes closed, serene
  - Explanation: medium-wide showing grandfather gesturing at scene element
  - Background element: environmental shot (sky/nature/element from ayah meaning)
  - NEVER show text, watermarks, or UI elements in the frame.
"""

class PromptBuilder:
    @staticmethod
    def get_system_prompt() -> str:
        return f"""أنت "الجد أبو زياد"، حكّاء قرآني بارع وعالم أزهري حنون.
أنت جد مصري يجلس مع أحفاده الصغار (5-8 سنوات) ليحكي لهم قبل النوم.

{STYLE_BIBLE}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[قواعد السرد — يُطبَّق على كل مشهد بدون استثناء]:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. VISUAL PROMPT (الأهم):
   كل visual_prompt يجب أن:
   ✦ يبدأ دائماً بـ: "Pixar/DreamWorks quality 3D CGI render —"
   ✦ يُحدد الكاميرا: (wide shot / medium shot / close-up / POV)
   ✦ يصف أبو زياد بالتفصيل من STYLE BIBLE
   ✦ يصف الأطفال ومشاعرهم بدقة
   ✦ يصف العنصر البصري الرئيسي للمشهد (ما يشرحه الجد)
   ✦ يصف الإضاءة والألوان من STYLE BIBLE
   ✦ يُنهى بـ: "ultra-detailed, ray-traced, cinematic, 4K, no text, no watermarks"
   ✦ لا يقل عن 80 كلمة إنجليزية
   ✦ يكون متسقاً تماماً مع بقية المشاهد (نفس الغرفة، نفس الشخصيات)

2. NARRATOR TEXT:
   ✦ عامية مصرية راقية ودافئة جداً
   ✦ يُمنع: "اشتركوا، لايك، كومنت"
   ✦ الخاتمة: دعاء دافئ وقصة نوم

3. INTRO/EXPLAIN TEXT:
   ✦ intro_text: ينتهي بـ "تعالوا نغمض عينينا ونسمع ربنا بيقول..."
   ✦ explain_text: يبدأ دائماً بـ "صدق الله العظيم" ثم قصة تبسيطية (50+ كلمة)
   ✦ يُمنع التفسير الجاف أو المدرسي

4. JSON GENERATION:
   ✦ أجب بـ JSON فقط — بدون أي نص قبله أو بعده
   ✦ وَلِّد جميع الآيات المطلوبة دفعة واحدة كاملة
   ✦ لا اختصار، لا حذف، لا "..."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[FEW-SHOT EXAMPLE — التزم بهذا المستوى من التفصيل]:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "title": "حكاية سورة الإخلاص",
  "youtube_title": "حواديت الجد أبو زياد | سورة الإخلاص 🌙",
  "youtube_description": "قبل النوم، جدنا أبو زياد يحكيلنا سورة الإخلاص بطريقة ما هتنساها...",
  "intro_scene": {{
    "narrator_text": "يا هلا بحبايبي وأبطالي الصغيرين! ايه ده؟ عينيكم بتتعمل صغيرة كده؟ أيوه، جه وقت الحواديت! النهاردة عندنا سورة ثوابها زي ثلث القرآن كله!",
    "visual_prompt": "Pixar/DreamWorks quality 3D CGI render — wide establishing shot of Abu Ziyad's warm Islamic study room, late evening. Abu Ziyad (elderly Egyptian grandfather, white Al-Azhar turban, white thobe, long white beard, warm honey-brown eyes, kind smile) sitting cross-legged on large burgundy velvet floor cushion, arms spread wide in warm welcome gesture. Three children (ages 4-7, Arabic features, cozy mint-green and dusty-pink pajamas, wide curious bright eyes) rushing into the room and settling around him on the hand-woven geometric carpet. Brass oil lanterns casting honeyed amber glow on ochre walls covered in Arabic calligraphy tapestries. Wooden mashrabiya window showing crescent moon and stars outside. Volumetric warm light rays. Magical intimate atmosphere. Ultra-detailed ray-traced render, cinematic depth of field, 4K, no text, no watermarks."
  }},
  "ayah_scenes": [
    {{
      "ayah_number": 1,
      "intro_text": "يا حبايبي، في يوم من الأيام، ناس كتير كانوا بيسألوا النبي ﷺ سؤال صعب جداً: يا محمد، ربنا ده مين؟ صفهولنا! فربنا بنفسه نزّل الجواب. تعالوا نغمض عينينا ونسمع ربنا بيقول...",
      "explain_text": "صدق الله العظيم. يا نور عينيّ، الآية دي بتقولنا إن ربنا 'أحد' — يعني واحد بس ومفيش غيره. تخيلوا معايا لو كانت في شمسين في السما؟ الأولى بتقول أنا اللي هادفي الدنيا، والتانية بتقول لأ أنا! وبعدين يحصل إيه؟ الدنيا كلها هتتحرق وهتبقى خراب! ربنا سبحانه جعل نفسه واحداً بس عشان الكون كله يمشي بنظام. زي ما البيت بيبقى عامل لما يكون فيه أب واحد بيحب وبيدبر — ربنا هو الأب الأكبر للكون كله.",
      "visual_prompt": "Pixar/DreamWorks quality 3D CGI render — medium shot, Abu Ziyad (white Al-Azhar turban, white thobe, long white beard, honey-brown eyes closed in reverence) sitting with one hand raised open toward heaven, face illuminated by warm golden light, expression of deep peace and awe. Two children (boy in mint pajamas, girl in dusty-pink pajamas) sitting before him with eyes wide and mouths slightly open in wonder. Behind grandfather through mashrabiya lattice window: breathtaking night sky showing a single brilliant golden star radiating light across dark blue velvet sky — representing divine oneness. Brass lanterns glow amber. Arabic calligraphy on walls. Warm volumetric light. Ultra-detailed, ray-traced lighting, cinematic, 4K, no text, no watermarks."
    }}
  ],
  "outro_scene": {{
    "narrator_text": "الحمد لله يا حبايبي. دلوقتي وانتوا بتناموا، افتكروا إن ربنا واحد أحد بيحميكوا وبيشوفكوا. قولوا معايا: 'اللهم احفظنا ووالدينا وجميع المسلمين'. تصبحوا على خير يا أبطالي.",
    "visual_prompt": "Pixar/DreamWorks quality 3D CGI render — wide warm shot of Abu Ziyad's study room, late night atmosphere. Abu Ziyad (white turban, white thobe, long white beard) gently tucking children under soft patchwork quilts on floor cushions, face glowing with grandfatherly love, one hand raised in dua (supplication). Children (eyes drooping sleepily, soft smiles) curled under blankets. Room lit only by single brass lantern casting deep amber warmth. Moonlight through mashrabiya creating soft geometric shadow patterns on carpet. A Quran open gently on carved wooden stand. Deeply peaceful, holy, intimate atmosphere. Ultra-detailed, ray-traced, cinematic 4K, no text, no watermarks."
  }}
}}

أجب بـ JSON فقط يتضمن جميع الآيات المطلوبة بنفس مستوى التفصيل."""


class ScriptEngine:
    def __init__(self):
        self.adapters = []

        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            try:
                self.adapters.append((GroqAdapter(groq_key), "llama-3.3-70b-versatile"))
                logger.info("✅ [1] Groq → llama-3.3-70b-versatile")
            except Exception as e:
                logger.warning(f"⚠️ Groq: {e}")

        gemini_keys = [
            ("GEMINI_API_KEY",   "Gmail-1"),
            ("GEMINI_API_KEY_2", "Gmail-2"),
            ("GEMINI_API_KEY_3", "Gmail-3"),
        ]
        for slot, (env_var, label) in enumerate(gemini_keys, start=2):
            key = os.getenv(env_var) or (APIKeys.GEMINI if env_var == "GEMINI_API_KEY" else None)
            if key:
                try:
                    self.adapters.append((GeminiAdapter(key), "gemini-2.5-pro"))
                    logger.info(f"✅ [{slot}] Gemini [{label}] → gemini-2.5-pro")
                except Exception as e:
                    logger.warning(f"⚠️ Gemini [{label}]: {e}")

        cohere_key = os.getenv("COHERE_API_KEY")
        if cohere_key:
            self.adapters.append((CohereAdapter(cohere_key), "command-r-plus-08-2024"))
            self.adapters.append((CohereAdapter(cohere_key), "command-r-08-2024"))

        if not self.adapters:
            raise ValueError("❌ لم يتم توفير أي مفاتيح API.")

        logger.info(f"✅ Fallback chain جاهز — {len(self.adapters)} نموذج")
        self.quality_gate = QualityGate()
        self.prompt_builder = PromptBuilder()

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        verified_ayahs = self._fetch_ayahs(info)
        ayah_refs = "\n".join([f"  [AYAH_{a.number}]: {a.text[:60]}..." for a in verified_ayahs])

        base_prompt = f"""السورة المطلوبة: سورة {info['name']} (رقم {info['surah']})
الآيات: من الآية {info['start']} إلى الآية {info['end']} ({info['end'] - info['start'] + 1} آيات)

مراجع الآيات (للتسلسل فقط — لا تكتب النص القرآني):
{ayah_refs}

المطلوب: وَلِّد سكريبت الحلقة كاملاً في رد واحد شامل ومفصل.
✦ كل visual_prompt لا يقل عن 80 كلمة إنجليزية
✦ استخدم STYLE BIBLE في كل مشهد
✦ الاتساق الكامل بين جميع المشاهد
✦ لا اختصار، لا حذف أي آية"""

        system_prompt = self.prompt_builder.get_system_prompt()
        raw_data = None

        for adapter, model_name in self.adapters:
            logger.info(f"🚀 {model_name}...")
            try:
                raw_data = adapter.generate(base_prompt, system_prompt, model_name)
                report = self.quality_gate.evaluate(raw_data)

                if not report.passed:
                    logger.warning(f"⚠️ {model_name} فشل في الجودة — إصلاح ذاتي...")
                    repair_prompt = (
                        f"JSON معيب بهذه الأخطاء:\n{chr(10).join(report.critiques)}\n\n"
                        f"أعد كتابة JSON كاملاً مع:\n"
                        f"1. إصلاح الأخطاء\n"
                        f"2. كل visual_prompt لا يقل عن 80 كلمة إنجليزية\n"
                        f"3. استخدام STYLE BIBLE في كل مشهد\n"
                        f"النص الأصلي:\n{raw_data}"
                    )
                    raw_data = adapter.generate(repair_prompt, system_prompt, model_name)
                    report = self.quality_gate.evaluate(raw_data)

                if report.passed:
                    logger.info(f"🏆 {model_name} — مقبول.")
                    break
                else:
                    logger.error(f"❌ {model_name} — فشل الإصلاح. التالي...")
                    raw_data = None

            except Exception as e:
                logger.error(f"خطأ في {model_name}: {e}")
                continue

        if not raw_data:
            raise RuntimeError("🚨 فشلت كافة النماذج.")

        script = self._build_script_object(episode_num, info, raw_data, verified_ayahs)
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        return script

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_ayahs(self, info):
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = (f"https://api.qurancdn.com/api/qdc/verses/by_key/"
                   f"{info['surah']}:{n}?words=false&fields=text_uthmani")
            resp = requests.get(url, timeout=15).json()
            ayahs.append(VerifiedAyah(
                surah=info["surah"], number=n,
                text=resp["verse"]["text_uthmani"], source="quran_api"
            ))
        return ayahs

    def _build_script_object(self, ep_num, info, data, verified):
        v_map = {a.number: a for a in verified}
        ayah_scenes = []

        for i, s in enumerate(data.get("ayah_scenes", [])):
            a_num = s.get("ayah_number", info["start"] + i)
            if a_num in v_map:
                intro_text   = str(s.get("intro_text",   ""))
                explain_text = str(s.get("explain_text", ""))
                ayah_scenes.append(AyahScene(
                    scene_id=10 + i, ayah=v_map[a_num],
                    intro_text=(intro_text if len(intro_text) > 20
                                else "يا حبايب جدو، تعالوا نغمض عينينا ونسمع ربنا..."),
                    explain_text=(explain_text if len(explain_text) > 40
                                  else "صدق الله العظيم. الآية دي يا أبطال بتعلمنا حاجة عظيمة."),
                    visual_prompt=str(s.get("visual_prompt",
                        "Pixar/DreamWorks quality 3D CGI render — Abu Ziyad with children in warm study room")),
                    repetitions=3, duration_sec=35
                ))

        return EpisodeScript(
            episode_number=ep_num, surah_name=info["name"], surah_number=info["surah"],
            title=str(data.get("title", f"سورة {info['name']}")),
            youtube_title=str(data.get("youtube_title", f"حواديت الجد | سورة {info['name']}")),
            youtube_description=str(data.get("youtube_description", "")),
            youtube_tags=[], total_duration_sec=300,
            intro_scene=NarratorScene(
                scene_id=1, scene_type=SceneType.INTRO, duration_sec=25,
                narrator_text=str(data.get("intro_scene", {}).get("narrator_text", "أهلاً بأبطالي!")),
                visual_prompt=str(data.get("intro_scene", {}).get("visual_prompt",
                    "Pixar 3D CGI, wide shot, Abu Ziyad welcoming children in warm Islamic study room")),
                mood=AudioMood.INTRO),
            ayah_scenes=ayah_scenes, mid_scenes=[],
            outro_scene=NarratorScene(
                scene_id=99, scene_type=SceneType.OUTRO, duration_sec=25,
                narrator_text=str(data.get("outro_scene", {}).get("narrator_text", "إلى اللقاء يا أبطال.")),
                visual_prompt=str(data.get("outro_scene", {}).get("visual_prompt",
                    "Pixar 3D CGI, grandfather tucking sleepy children into bed, warm lantern light")),
                mood=AudioMood.OUTRO))

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
        return None
