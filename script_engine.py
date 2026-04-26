"""
script_engine.py — VALUE / QEEMA v10.0 (Egyptian Dialect + Semantic Visual Picker)
==================================================================================
- لهجة مصرية خالصة (مش شامية، مش فصحى ثقيلة)
- اختيار visual_scene تلقائي بناءً على محتوى الآية
- استخراج keywords للـ word-level animations
- Load balancer للـ APIs
"""
import json
import logging
import os
import re
import requests
from typing import Dict, List, Any
from config import CURRICULUM, Paths
from models import (
    EpisodeScript, AyahScene, NarratorScene, SceneType,
    VerifiedAyah, VisualScene
)
from core_adapters import GeminiAdapter, GroqAdapter

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Semantic mapping: keywords → VisualScene
# يستخدم لاختيار المشهد الإجرائي المناسب لكل آية
# ════════════════════════════════════════════════════════════════
SCENE_KEYWORDS: Dict[VisualScene, List[str]] = {
    VisualScene.GARDEN: [
        "جنة", "حديقة", "زرع", "ثمر", "شجر", "ورد", "نبات",
        "نعمة", "نعيم", "رزق", "خير", "بركة"
    ],
    VisualScene.SKY: [
        "سماء", "سماوات", "نجم", "نجوم", "قمر", "شمس",
        "كون", "مجرة", "فضاء", "علو", "رفع"
    ],
    VisualScene.HOUSE: [
        "بيت", "بيوت", "أم", "أب", "أهل", "أسرة",
        "أخ", "أخت", "والدين", "أولاد"
    ],
    VisualScene.MOSQUE: [
        "صلاة", "ركوع", "سجود", "مسجد", "مساجد",
        "عبادة", "خشوع", "أذان", "محراب"
    ],
    VisualScene.OCEAN: [
        "بحر", "ماء", "نهر", "أنهار", "مطر",
        "غيث", "سفينة", "موج", "سحاب"
    ],
    VisualScene.DESERT: [
        "صحراء", "إبل", "ناقة", "رحلة", "سفر",
        "قافلة", "رمل"
    ],
    VisualScene.MOUNTAINS: [
        "جبل", "جبال", "صخر", "حجر"
    ],
    VisualScene.CHILD_PRAYING: [
        "طفل يصلي", "أطفال", "ذكر", "تسبيح", "حمد"
    ],
    VisualScene.FAMILY: [
        "محبة", "رحمة", "مودة", "تعاون", "صدقة",
        "إحسان", "بر", "صلة"
    ],
}

# Palette mapping بناءً على المزاج
MOOD_PALETTES = {
    "warm":     "warm_sunset",
    "calm":     "calm_blue",
    "joyful":   "lush_green",
    "reverent": "night_stars",
    "majestic": "golden_hour",
}


def pick_visual_scene(text: str) -> VisualScene:
    """يختار VisualScene الأنسب بناءً على محتوى النص."""
    text_lower = text.lower()
    scores: Dict[VisualScene, int] = {}

    for scene, keywords in SCENE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[scene] = score

    if not scores:
        return VisualScene.ABSTRACT_WARM
    # رجّع الأعلى score
    return max(scores, key=scores.get)


def extract_keywords(text: str, max_words: int = 5) -> List[str]:
    """يستخرج كلمات مفتاحية من النص للـ animations."""
    # كلمات stop words
    stop = {"في", "من", "إلى", "على", "عن", "مع", "هو", "هي",
            "هم", "أن", "إن", "كان", "يكون", "ذا", "ذلك", "هذا",
            "هذه", "كل", "ما", "لا", "ثم", "أو", "و", "ف", "ال"}
    # امسح علامات الترقيم
    clean = re.sub(r'[،.؟!:؛"\'\(\)\[\]\{\}]', ' ', text)
    words = [w for w in clean.split() if w and w not in stop and len(w) > 2]
    # خد أهم الكلمات
    return words[:max_words]


# ════════════════════════════════════════════════════════════════
# Egyptian System Prompt — لهجة مصرية أصيلة
# ════════════════════════════════════════════════════════════════
EGYPTIAN_SYSTEM_PROMPT = """أنت "الجد أبو زياد"، حكواتي مصري الأصل، تحكي للأطفال المصريين (5-8 سنوات).

[قواعد اللهجة — صارمة]:
✅ استخدم: "إيه، إزاي، عايز، فين، كده، يلا، خلاص، علشان، عشان، حلو، أهو، طيب، هنا، هناك"
✅ استخدم: "بنحب، بنعمل، بيقولوا، عاوزين، فاهمين"
❌ تجنب تماماً: "كيف، ماذا، أين، الآن، حسناً، حقاً، بالطبع، يا ترى"
❌ تجنب الفصحى الثقيلة والكلمات الشامية ("شو، هلق، هيك، كتير")

[نمط السرد]:
- جملة قصيرة (8-15 كلمة).
- نبرة دافئة كأنك تحكي قصة قبل النوم.
- استخدم التشبيهات البسيطة من حياة الطفل (مثل: "زي ما إنت بتحب ماما").
- تجنب كلمات العقاب (نار، عذاب، جحيم) — استبدلها بـ "اللي مش بيسمع كلام ربنا".
- لا تكرر نفس الكلمة في فقرة واحدة.

[النظام التربوي]:
- اجعل الطفل يحس إن ربنا بيحبه جداً.
- ربط كل آية بموقف يومي بسيط (المدرسة، الأهل، اللعب، الأكل).
- الترغيب أهم من الترهيب.

[التنسيق]:
- أجب بـ JSON صالح فقط.
- النصوص العربية بالعامية المصرية.
- الـ visual_prompt قصير بالإنجليزية (للأرشفة فقط — لن يُستخدم).
"""


class ScriptEngine:
    def __init__(self):
        self.adapters = []
        self._setup_load_balancer()
        self.ptr = 0

    def _setup_load_balancer(self):
        keys = [os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3")]
        for k in keys:
            if k:
                self.adapters.append((GeminiAdapter(k), "gemini-2.5-flash"))
        if os.getenv("GROQ_API_KEY"):
            self.adapters.append((GroqAdapter(os.getenv("GROQ_API_KEY")), "llama-3.3-70b-versatile"))
        if not self.adapters:
            raise RuntimeError("❌ لا توجد مفاتيح LLM API!")
        logger.info(f"✅ Script Engine: {len(self.adapters)} adapters loaded")

    def _call_ai(self, prompt: str, system: str, retries: int = 0) -> dict:
        """تبديل المفتاح فوراً عند كل طلب."""
        if retries > len(self.adapters) * 2:
            raise RuntimeError("فشلت كل محاولات LLM")

        adapter, model = self.adapters[self.ptr]
        self.ptr = (self.ptr + 1) % len(self.adapters)

        try:
            res = adapter.generate(prompt, system, model)
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if not match:
                raise ValueError("لم يتم العثور على JSON")
            cleaned = match.group()
            cleaned = re.sub(r',\s*}', '}', cleaned)
            cleaned = re.sub(r',\s*]', ']', cleaned)
            return json.loads(cleaned)
        except Exception as e:
            logger.warning(f"⚠️ {model} فشل: {e} | محاولة التالي...")
            return self._call_ai(prompt, system, retries + 1)

    def load_from_disk(self, ep_num: int) -> EpisodeScript:
        """تحميل سكريبت موجود من الـ disk."""
        save_path = Paths.SCRIPT_DIR / f"episode_{ep_num:03d}.json"
        if not save_path.exists():
            return None
        try:
            data = json.loads(save_path.read_text(encoding="utf-8"))
            return EpisodeScript.model_validate(data)
        except Exception as e:
            logger.warning(f"⚠️ فشل تحميل السكريبت من القرص: {e}")
            return None

    def generate(self, ep_num: int) -> EpisodeScript:
        info = CURRICULUM[ep_num]
        ayahs = self._fetch_ayahs(info)

        logger.info(f"🚀 توليد سكريبت سورة {info['name']} باللهجة المصرية...")

        # 1) المقدمة
        intro_prompt = (
            f"اكتب مقدمة بالعامية المصرية لحلقة عن سورة {info['name']} للأطفال. "
            "أجب بـ JSON بالحقول: title, youtube_title, youtube_description, "
            "youtube_tags (array of 5 Arabic tags), intro_text (max 30 words), visual_prompt."
        )
        intro_data = self._call_ai(intro_prompt, EGYPTIAN_SYSTEM_PROMPT)

        # 2) الآيات
        ayah_scenes = []
        for i, a in enumerate(ayahs):
            logger.info(f"📖 الآية {a.number}...")
            ayah_prompt = (
                f"الآية: {a.text}\n"
                "اشرحها بالعامية المصرية لطفل صغير. اربطها بموقف من حياته اليومية. "
                "أجب بـ JSON بالحقول: intro_text (max 25 words), explain_text (max 35 words), visual_prompt."
            )
            try:
                a_data = self._call_ai(ayah_prompt, EGYPTIAN_SYSTEM_PROMPT)

                # ✅ اختيار visual_scene تلقائي بناءً على محتوى الآية + الشرح
                combined = f"{a.text} {a_data.get('explain_text', '')}"
                vscene = pick_visual_scene(combined)
                keywords = extract_keywords(a_data.get('explain_text', ''))

                ayah_scenes.append(AyahScene(
                    scene_id=10 + i,
                    ayah=a,
                    intro_text=a_data.get('intro_text', ''),
                    explain_text=a_data.get('explain_text', ''),
                    visual_prompt=a_data.get('visual_prompt', ''),
                    visual_scene=vscene,
                    palette=MOOD_PALETTES.get("warm", "warm_sunset"),
                    keywords=keywords,
                ))
            except Exception as e:
                logger.error(f"❌ فشل توليد الآية {a.number}: {e}")
                continue

        if not ayah_scenes:
            raise RuntimeError("لم يتم توليد أي مشهد آية صالح")

        # 3) الخاتمة
        outro_prompt = (
            "اكتب خاتمة بالعامية المصرية تتضمن دعاء قبل النوم لطفل. "
            "أجب بـ JSON: narrator_text (max 30 words), visual_prompt."
        )
        outro_data = self._call_ai(outro_prompt, EGYPTIAN_SYSTEM_PROMPT)

        # تحديد visual_scene للـ intro & outro
        intro_vscene = pick_visual_scene(intro_data.get('intro_text', '') + " " + info['name'])
        outro_vscene = VisualScene.SKY  # خاتمة بسماء النجوم دائماً

        script = EpisodeScript(
            episode_number=ep_num,
            surah_name=info['name'],
            title=intro_data.get('title', f"سورة {info['name']}"),
            youtube_title=intro_data.get('youtube_title', f"سورة {info['name']} للأطفال"),
            youtube_description=intro_data.get('youtube_description', ''),
            youtube_tags=intro_data.get('youtube_tags', [info['name'], "قرآن", "أطفال"]),
            intro_scene=NarratorScene(
                scene_id=1,
                scene_type=SceneType.INTRO,
                narrator_text=intro_data.get('intro_text', ''),
                visual_prompt=intro_data.get('visual_prompt', ''),
                visual_scene=intro_vscene,
                palette="golden_hour",
                keywords=extract_keywords(intro_data.get('intro_text', '')),
            ),
            ayah_scenes=ayah_scenes,
            outro_scene=NarratorScene(
                scene_id=99,
                scene_type=SceneType.OUTRO,
                narrator_text=outro_data.get('narrator_text', ''),
                visual_prompt=outro_data.get('visual_prompt', ''),
                visual_scene=outro_vscene,
                palette="night_stars",
                keywords=extract_keywords(outro_data.get('narrator_text', '')),
            ),
        )

        save_path = Paths.SCRIPT_DIR / f"episode_{ep_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        logger.info(f"✅ السكريبت محفوظ: {save_path}")
        return script

    def _fetch_ayahs(self, info: dict) -> List[VerifiedAyah]:
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = f"https://api.qurancdn.com/api/qdc/verses/by_key/{info['surah']}:{n}?words=false&fields=text_uthmani"
            try:
                d = requests.get(url, timeout=15).json()
                text = d.get("verse", {}).get("text_uthmani", "")
                if text:
                    ayahs.append(VerifiedAyah(surah=info["surah"], number=n, text=text))
            except Exception as e:
                logger.error(f"❌ فشل تنزيل الآية {info['surah']}:{n}: {e}")
        return ayahs
