"""
engines/script_engine.py — VALUE / QEEMA v16.0
==================================================================================
[v16 — Philosophical Rebuild]

REMOVED:
- "جدو أبو زياد" character entirely
- Story-with-named-children pattern (كريم/نور/سارة)
- _STORY_OPENERS templates
- character_name seeding

ADDED:
- Hook-first methodology: insight + curiosity gap in opening 5 seconds
- Insight-driven explanation (not narrative storytelling)
- Real-world analogies WITHOUT fictional characters
- Universal appeal: child + parent both engaged
- TED-Ed inspired structure: question → reveal → connect → apply

KEPT FROM v15:
- Parallel ayah generation across Gemini keys
- Consolidated meta call (intro + outro + SEO in one)
- Self-correcting retries
- Quality gate
- SSML metadata generation
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import APIKeysConfig, EngineConfig, PathsConfig
from core.exceptions import (
    ConfigurationError,
    PermanentError,
    QualityGateError,
    ScriptGenerationError,
    ValidationError,
)
from core.interfaces import LLMProvider, QualityValidator
from core.models import EpisodeScript
from core.resilience import CircuitBreakerConfig, ProviderPool
from data.curriculum import SurahInfo, get_episode_info
from infrastructure.llm_adapters import GeminiJsonAdapter, GroqJsonAdapter
from infrastructure.quran_text_api import fetch_verified_ayahs

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# v16 — Insight-First System Prompt (NO character — universal voice)
# ════════════════════════════════════════════════════════════════
SYSTEM_PROMPT: str = """\
أنت كاتب محتوى قرآني تعليمي عالي الجودة، أسلوبك يجمع:
- وضوح TED-Ed التعليمي
- بساطة "ناشيونال جيوغرافيك للأطفال"
- عمق المفسرين الكبار

جمهورك: أطفال 6-12 سنة + أهلهم اللي بيتفرجوا معاهم.
هدفك: كل حلقة تكشف معنى آية بطريقة تخلي الطفل يقول "آه! فهمت!" والكبار يقولوا "ما كنتش عارف الزاوية دي".

═══ المنهج: Hook-First Methodology ═══

كل آية تتشرح بالـ 5 طبقات دي بالترتيب:

[1] HOOK (الخطاف) — أول 5 ثواني
سؤال أو حقيقة مذهلة تخلق "curiosity gap":
✅ "تعرف إيه الكنز اللي اتذكر في آيتين بس في القرآن؟"
✅ "في الكون فيه ٢٠٠ مليار نجم، الآية دي بتقول حاجة عنهم..."
✅ "ليه ساعات بنحس إن وقت الفرح بيمر بسرعة جداً؟"
❌ "السلام عليكم يا أحبائي" (كليشيه)
❌ "النهارده هنتكلم عن..." (مباشر مفيش curiosity)

[2] INTRO (التمهيد) — جسر بين الـ hook والآية
جملتين يربطوا السؤال بالموضوع:
"الآية اللي هنفهمها دلوقتي بتجاوب على ده بطريقة عجيبة..."

[3] ANALOGY (المثال الحقيقي) — من العالم الواقعي
مثال من الطبيعة، العلم، الحياة اليومية — **بدون شخصيات وهمية، بدون أسماء أطفال**:
✅ "زي البذرة لما تتزرع، مش بتطلع شجرة فوراً، فيه وقت تحت التراب..."
✅ "زي الإنترنت، فيه إشارات بتمر ما نشوفهاش بس بتوصل..."
✅ "النحلة لما بتروح للورد، بتشتغل لكن مش لنفسها..."
❌ "كان فيه ولد اسمه كريم..." (ممنوع)
❌ "في يوم من الأيام..." (ممنوع)

[4] EXPLAIN (الشرح) — المعنى المباشر
شرح الآية بكلمتين-3 جمل، واضح ومرتبط بالـ analogy

[5] TAKEAWAY (الخلاصة) — اللي يفضل في دماغ الطفل
جملة واحدة قوية، قابلة للحفظ، تربط الكل

═══ قواعد اللهجة الصارمة ═══

✅ مصري معاصر بسيط: إيه، إزاي، عايز، فين، كده، يعني، أهو، ليه
✅ كلمات حديثة: الإنترنت، الكون، الذرة، الموجات، النجوم، الكواكب
✅ تعابير: "تخيّل معايا"، "خلينا نشوف"، "اللي بيحصل إن..."
❌ ممنوع: حواديت بشخصيات وهمية، "جدو"، "حدوتة"، "كان يا ما كان"
❌ ممنوع: لهجات تانية (شو، هلق، كتير، منيح)
❌ ممنوع: كلمات معقدة (العقاب، الجحيم، البرزخ، إلا في سياقات محسوبة)
❌ ممنوع: كليشيهات دينية (يا أحبائي، يا أحباب الله، يا بنيا)

═══ قواعد الجودة ═══

- كل جملة: 8-15 كلمة، مفهومة من أول قراية
- ما تكررش كلمة في نفس الفقرة
- استخدم أمثلة من: علم، طبيعة، تكنولوجيا، حياة يومية
- visual_prompt: إنجليزي سينمائي، بدون أي بشر يتكلمون أو شخصيات رئيسية ثابتة
- النصوص العربية كلها بدون تشكيل (إلا visual_prompt الإنجليزي)

═══ التنسيق ═══
- JSON صالح فقط، مفيش markdown، مفيش شرح خارج الـ JSON
"""


# ════════════════════════════════════════════════════════════════
# v16 — Hook strategy patterns (for prompt diversity)
# ════════════════════════════════════════════════════════════════
_HOOK_STRATEGIES: List[str] = [
    "سؤال علمي مذهل (مثال: 'تعرف إن في الكون...؟')",
    "إحصائية أو رقم لافت ('فيه ٧.٥ مليار إنسان، والآية بتقول...')",
    "تناقض أو مفارقة ('عجيب إن أصغر حاجة في الكون بتعمل...')",
    "تحدي ذهني ('لو قلتلك إن في كنز في كلمتين بس، تصدق؟')",
    "ملاحظة من الحياة اليومية ('لاحظت قبل كده إن لما...')",
    "حقيقة من الطبيعة ('النملة الصغيرة دي بتعمل حاجة في غاية الذكاء...')",
]

_ANALOGY_DOMAINS: List[str] = [
    "الفضاء والنجوم (الكون، الكواكب، السرعة، النور)",
    "العلوم البسيطة (الذرة، الموجات، المغناطيس، الحرارة)",
    "الطبيعة (الشجر، النحل، الأنهار، الأمطار، الفصول)",
    "التكنولوجيا (الإنترنت، الموبايل، الإشارات، التواصل)",
    "الحياة اليومية (الزرع، الأكل، السفر، البيت، المدرسة)",
    "علم الأحياء (الجسم، الخلايا، النبض، التنفس)",
]


def get_episode_seed(episode_number: int, scene_index: int) -> Tuple[str, str]:
    """v16: returns (hook_strategy_hint, analogy_domain_hint) for diversity."""
    hook_idx = (episode_number * 7 + scene_index * 3) % len(_HOOK_STRATEGIES)
    domain_idx = (episode_number * 5 + scene_index * 2) % len(_ANALOGY_DOMAINS)
    return _HOOK_STRATEGIES[hook_idx], _ANALOGY_DOMAINS[domain_idx]


# ════════════════════════════════════════════════════════════════
# Scene mappings (kept from v15)
# ════════════════════════════════════════════════════════════════
_SCENE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "golden_field":  ("زرع", "قمح", "حصاد", "مزرعة", "بذرة", "نبات", "شجرة", "ثمر", "خير"),
    "garden":        ("جنة", "حديقة", "ورد", "رزق", "نعمة", "بركة"),
    "starry_night":  ("نجم", "نجوم", "قمر", "ليل", "سماء", "فضاء", "كون"),
    "sky":           ("سماوات", "شمس", "علو", "رفع", "سحاب"),
    "house":         ("بيت", "أم", "أب", "أهل", "أسرة", "أخ", "أخت"),
    "mosque":        ("صلاة", "ركوع", "سجود", "مسجد", "عبادة", "خشوع"),
    "ocean":         ("بحر", "ماء", "نهر", "مطر", "سفينة", "موج"),
    "child_reading": ("كتاب", "علم", "تعلم", "قراءة", "حفظ", "مدرسة"),
    "child_praying": ("تسبيح", "حمد", "ذكر", "دعاء"),
    "family":        ("محبة", "رحمة", "مودة", "تعاون", "صدقة", "بر"),
    "rainbow":       ("ألوان", "فرح", "بهجة", "نور", "ضياء"),
    "flowers":       ("ورد", "زهر", "جمال", "عطر", "روضة"),
    "desert":        ("صحراء", "إبل", "رحلة", "سفر", "قافلة"),
    "mountains":     ("جبل", "صخر", "قوة", "ثبات"),
}

_SCENE_EMOTION: Dict[str, str] = {
    "golden_field": "warm", "garden": "peaceful", "starry_night": "reverent",
    "sky": "reverent", "house": "warm", "mosque": "reverent", "ocean": "peaceful",
    "child_reading": "playful", "child_praying": "reverent", "family": "warm",
    "rainbow": "excited", "flowers": "playful", "desert": "peaceful",
    "mountains": "reverent", "abstract_warm": "warm",
}

_SCENE_PALETTE: Dict[str, str] = {
    "golden_field": "golden_hour", "garden": "lush_green", "starry_night": "night_stars",
    "sky": "night_stars", "house": "warm_sunset", "mosque": "golden_hour",
    "ocean": "calm_blue", "child_reading": "soft_morning", "child_praying": "golden_hour",
    "family": "warm_sunset", "rainbow": "soft_morning", "flowers": "lush_green",
    "desert": "golden_hour", "mountains": "deep_teal", "abstract_warm": "warm_sunset",
}

_ARABIC_STOPWORDS: frozenset = frozenset({
    "في", "من", "إلى", "على", "عن", "مع", "هو", "هي", "هم",
    "أن", "إن", "كان", "يكون", "ذا", "ذلك", "هذا", "هذه",
    "كل", "ما", "لا", "ثم", "أو", "و", "ف", "ال",
})

_PUNCT_RE = re.compile(r'[،.؟!:؛"\'\(\)\[\]{}]')
_TASHKEEL_RE = re.compile(r'[\u064B-\u0652\u0670\u0640]')
_PUNCTUATION_SET = {'.', '،', '؛', '؟', '!', ':'}


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════
def pick_visual_scene(text: str) -> str:
    if not text:
        return "abstract_warm"
    best = ("abstract_warm", 0)
    for scene, keywords in _SCENE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best[1]:
            best = (scene, score)
    return best[0]


def pick_emotion(scene_type: str) -> str:
    return _SCENE_EMOTION.get(scene_type, "warm")


def pick_palette(scene_type: str) -> str:
    return _SCENE_PALETTE.get(scene_type, "warm_sunset")


def extract_keywords(text: str, *, max_words: int = 5) -> List[str]:
    if not text:
        return []
    cleaned = _PUNCT_RE.sub(" ", text)
    out: List[str] = []
    for w in cleaned.split():
        if w in _ARABIC_STOPWORDS or len(w) <= 2:
            continue
        out.append(w)
        if len(out) >= max_words:
            break
    return out


def humanize_arabic(text: str) -> str:
    """Full tashkeel strip + smart punctuation insertion."""
    if not text:
        return text
    cleaned = _TASHKEEL_RE.sub('', text)
    cleaned = unicodedata.normalize('NFKC', cleaned)
    cleaned = ''.join(c for c in cleaned if not unicodedata.combining(c))
    if cleaned and cleaned[-1] not in _PUNCTUATION_SET:
        cleaned += '.'
    if len(cleaned) > 40 and '،' not in cleaned and '.' not in cleaned[:-1]:
        words = cleaned.split()
        if len(words) > 5:
            insert_pos = len(' '.join(words[:3]))
            cleaned = cleaned[:insert_pos] + '، ' + cleaned[insert_pos:]
    return cleaned.strip()


def to_elevenlabs_ssml(text: str) -> str:
    """Build SSML with natural pauses. Used for metadata only."""
    ssml_text = text.replace("،", "،<break time='400ms'/>")
    ssml_text = ssml_text.replace(".", ".<break time='650ms'/>")
    ssml_text = ssml_text.replace("؟", "؟<break time='500ms'/>")
    ssml_text = ssml_text.replace("!", "!<break time='400ms'/>")
    return f"<speak><prosody rate='slow'>{ssml_text}</prosody></speak>"


# ════════════════════════════════════════════════════════════════
# v16 Consolidated Episode Meta Prompt — universal opener (no character)
# ════════════════════════════════════════════════════════════════
def _build_episode_meta_prompt(surah_name: str, surah_number: int, ayah_count: int) -> str:
    return f"""
اكتب MetaData كاملة لحلقة قصيرة عن سورة {surah_name} ({ayah_count} آية).
الأسلوب: TED-Ed للأطفال، insight-first، بدون شخصية وهمية.

أجب بـ JSON:
{{
  "title": "عنوان عربي حلو يثير الفضول (max 50 حرف، يفضل سؤال أو حقيقة لافتة)",

  "youtube_title": "عنوان يوتيوب جذاب لـSEO max 60 حرف. أمثلة جيدة: 'سورة {surah_name} - الكنز اللي مالحظوش' أو 'أعجب آية في سورة {surah_name}'",

  "youtube_description": "وصف 200-300 كلمة. أول سطرين: hook قوي + اسم السورة. وسط الوصف: ملخص الفكرة الرئيسية. آخره hashtags: #تفسير_للأطفال #قرآن #سورة_{surah_name} #تدبر",

  "youtube_tags": ["وسم1", "وسم2", "تفسير قرآن", "{surah_name}", "قرآن للأطفال", "تدبر", "إعجاز"],

  "intro_text": "افتتاحية قوية max 30 كلمة. لازم تبدأ بـ hook (سؤال/حقيقة مذهلة)، مش 'السلام عليكم' ولا 'مرحباً'. مثال: 'تعرف إن جوا سورة {surah_name} فيه إجابة على سؤال محيّر العلماء؟'",

  "cta_text": "جملة قصيرة max 18 كلمة. تذكير ودي بالاشتراك والجرس. مفيش كليشيهات. مثال: 'لو الفكرة دي عجبتك، فعّل الجرس عشان توصلك الحلقات الجديدة'",

  "outro_text": "خاتمة فيها الـ takeaway الرئيسي للحلقة في جملتين. ثم دعاء قصير. max 35 كلمة. بدون 'وداعاً يا أحبائي'.",

  "intro_visual": "Wide cinematic establishing shot related to surah {surah_name} theme: cosmic scale, natural beauty, or thematic landscape. NO human figures speaking. Style: warm 2D illustration, soft pastel colors, no text.",

  "outro_visual": "Peaceful contemplative scene: starry night, calm horizon, or symbolic abstract. NO human figures. Style: warm 2D illustration, soft pastel colors, golden glow, no text."
}}
"""


# ════════════════════════════════════════════════════════════════
# v16 Per-Ayah Prompt — hook-first, no character, real-world analogies
# ════════════════════════════════════════════════════════════════
def _build_ayah_prompt(
    ayah_text: str,
    ayah_number: int,
    surah_name: str,
    scene_index: int,
    total_scenes: int,
    episode_number: int,
) -> str:
    is_first = scene_index == 0
    is_last = scene_index == total_scenes - 1
    position_hint = (
        "أول آية في الحلقة — الـ hook لازم يكون أقوى ما يكون" if is_first
        else "آخر آية — الـ takeaway يكون خاتمة جميلة للحلقة" if is_last
        else f"آية {scene_index + 1} من {total_scenes}"
    )

    hook_strategy, analogy_domain = get_episode_seed(episode_number, scene_index)

    return f"""
({position_hint})

الآية: {ayah_text}
السورة: {surah_name}، رقم الآية: {ayah_number}

[توجيهات هذه الآية تحديداً]:
- استراتيجية الـ Hook المقترحة: {hook_strategy}
- مجال الـ Analogy المقترح: {analogy_domain}

أجب بـ JSON بالحقول دي بالظبط:

{{
  "hook_text": "الخطاف. سؤال أو حقيقة مذهلة (max 25 كلمة). يخلق curiosity gap. ممنوع 'يا أحبائي'، 'تعالوا'، 'النهارده'.",

  "intro_text": "جسر بين الـ hook والآية في جملتين (max 25 كلمة). يقدم الموضوع بدون كشف الإجابة كاملة.",

  "analogy_text": "مثال من الواقع لتوضيح المعنى (max 60 كلمة). لازم يكون من {analogy_domain}. ممنوع تماماً: شخصيات وهمية، أسماء أطفال، 'كان فيه ولد'، 'في يوم من الأيام'. ابدأ بـ 'تخيل...' أو 'فكر معايا...' أو 'لو بصينا للـ...'",

  "explain_text": "الشرح المباشر للآية في جملتين (max 40 كلمة). اربطه بالـ analogy.",

  "moral_text": "الـ takeaway. جملة واحدة قوية وقابلة للحفظ (max 20 كلمة). تربط الآية بحياة الطفل/الكبار.",

  "scene_emotion": "اختار من: warm / reverent / playful / peaceful / excited",

  "visual_subject": "What is in the scene (English, max 8 words). Focus on: nature, cosmos, abstract symbolism. NEVER human faces or named characters. Examples: 'vast galaxy with swirling stars', 'ancient olive tree in golden field', 'silver fish swimming in ocean depths'",

  "visual_action": "What is happening (English, max 6 words). Examples: 'gentle wind moves leaves', 'stars slowly rotating', 'water flowing peacefully'",

  "visual_environment": "Where (English, max 8 words). Examples: 'Mediterranean countryside at sunset', 'deep space with colorful nebulae', 'serene mountain valley'",

  "visual_scene_hint": "اختار من: golden_field / garden / sky / house / mosque / ocean / child_reading / child_praying / family / rainbow / flowers / desert / mountains / starry_night / abstract_warm"
}}

[تذكير حاسم]:
- ممنوع كلياً: شخصيات (جدو/كريم/نور/سارة/إلخ)
- ممنوع كلياً: "كان يا ما كان" / "في يوم من الأيام"
- مطلوب: hook قوي + analogy من العالم الحقيقي + takeaway قابل للحفظ
"""


# ════════════════════════════════════════════════════════════════
# ScriptEngine v16
# ════════════════════════════════════════════════════════════════
class ScriptEngine:
    """v16: Insight-driven script generation, no fictional character."""

    PROMPT_VERSION: str = "v18.0"  # bump invalidates cached scripts

    def __init__(
        self,
        *,
        api_keys: APIKeysConfig,
        paths: PathsConfig,
        engine_cfg: EngineConfig,
        quality_validator: Optional[QualityValidator] = None,
        tafsir_validator: Optional[Any] = None,  # v18 NEW: religious validator
        hook_optimizer: Optional[Any] = None,  # v18 NEW: data-driven hook selection
    ) -> None:
        self._paths = paths
        self._engine_cfg = engine_cfg
        self._quality_validator = quality_validator
        self._tafsir_validator = tafsir_validator  # v18
        self._hook_optimizer = hook_optimizer  # v18
        self._add_ssml: bool = getattr(engine_cfg, "add_ssml", True)

        self._adapters: Dict[str, LLMProvider] = {}
        self._adapter_names: List[str] = []
        self._pool = ProviderPool("script_llm", strategy="round_robin")
        self._setup_providers(api_keys)
        if tafsir_validator:
            logger.info("✅ Religious validator wired (Claude Opus reviewer)")
        if hook_optimizer:
            logger.info("✅ Hook optimizer wired (data-driven selection)")

    def _setup_providers(self, api_keys: APIKeysConfig) -> None:
        # v22.5: use script_pool_keys (excludes the dedicated tafsir key)
        # v22.5 RATE LIMIT FIX: Gemini free tier = 5 RPM = 1 call per 12 seconds
        # OLD: rate_limit=(1.0, 5) → 1 RPS + burst 5 = ~12x faster than allowed
        # NEW: rate_limit=(0.067, 1) → 1 token per 15s, no burst
        #   0.067 tokens/sec × 60s = 4 tokens/min (safety margin under 5 RPM cap)
        for i, key in enumerate(api_keys.script_pool_keys, start=1):
            try:
                name = f"gemini-{i}"
                self._adapters[name] = GeminiJsonAdapter(
                    key, model="gemini-2.5-flash", instance_name=name,
                )
                self._adapter_names.append(name)
                self._pool.register(
                    name,
                    breaker_config=CircuitBreakerConfig(
                        failure_threshold=4, recovery_timeout_sec=45.0,
                    ),
                    rate_limit=(0.067, 1),  # 4 RPM with no burst (safe under 5 RPM)
                )
            except Exception as e:
                logger.warning(f"⚠️ Gemini #{i} init failed: {e}")

        if api_keys.groq:
            try:
                self._adapters["groq"] = GroqJsonAdapter(
                    api_keys.groq, instance_name="groq",
                )
                self._adapter_names.append("groq")
                self._pool.register(
                    "groq",
                    breaker_config=CircuitBreakerConfig(
                        failure_threshold=4, recovery_timeout_sec=30.0,
                    ),
                    rate_limit=(2.0, 10),
                )
            except Exception as e:
                logger.warning(f"⚠️ Groq init failed: {e}")

        if not self._adapters:
            raise ConfigurationError(
                "No LLM providers. Set GEMINI_API_KEY or GROQ_API_KEY."
            )
        logger.info(
            f"✅ ScriptEngine v16: {len(self._adapters)} providers: "
            f"{list(self._adapters.keys())}"
        )

    def _call_llm(self, prompt: str, system: str = SYSTEM_PROMPT) -> Dict[str, Any]:
        def _invoke(name: str) -> Dict[str, Any]:
            return self._adapters[name].generate_json(prompt, system)
        return self._pool.execute(_invoke)

    def _call_llm_direct(
        self, adapter_name: str, prompt: str, system: str = SYSTEM_PROMPT,
    ) -> Dict[str, Any]:
        return self._adapters[adapter_name].generate_json(prompt, system)

    # ─── Public API ──────────────────────────────────────────────
    def load_from_disk(self, episode_number: int) -> Optional[EpisodeScript]:
        path = self._script_path(episode_number)
        if not path.exists():
            return None
        try:
            return EpisodeScript.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as e:
            logger.warning(f"⚠️ Cached script {episode_number} invalid: {e}")
            return None

    def generate(self, episode_number: int) -> EpisodeScript:
        try:
            info: SurahInfo = get_episode_info(episode_number)
        except KeyError as e:
            raise ValidationError(str(e), episode_number=episode_number, stage="script.lookup") from e

        cached = self.load_from_disk(episode_number)
        if cached:
            logger.info(f"♻️ Episode {episode_number}: cached script loaded")
            return cached

        logger.info(
            f"🚀 Generating insight-driven episode {episode_number} "
            f"(Surah {info['name']}, ayahs {info['start']}-{info['end']})"
        )

        try:
            t0 = time.monotonic()

            ayahs = fetch_verified_ayahs(info["surah"], info["start"], info["end"])
            t_fetch = time.monotonic() - t0

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as exe:
                meta_future = exe.submit(self._generate_meta_consolidated, info, len(ayahs))
                ayahs_future = exe.submit(
                    self._generate_ayah_scenes_parallel, ayahs, episode_number, info
                )
                meta_data = meta_future.result()
                ayah_scenes_data = ayahs_future.result()

            t_llm = time.monotonic() - t0 - t_fetch

            script_dict = self._assemble(
                episode_number, info, meta_data, ayah_scenes_data
            )
            script = EpisodeScript.model_validate(script_dict)

            if self._quality_validator is not None:
                report = self._quality_validator.validate(script_dict)
                if not report.passed:
                    raise QualityGateError(
                        f"Quality below threshold (score={report.overall_score:.1f}/100)",
                        score=report.overall_score,
                        critiques=report.critiques,
                        episode_number=episode_number,
                        stage="script.quality",
                    )
                logger.info(f"✅ Quality: {report.overall_score:.1f}/100")

            self._save_atomic(episode_number, script)

            t_total = time.monotonic() - t0
            logger.info(
                f"⏱️ Episode {episode_number} script: "
                f"fetch={t_fetch:.1f}s llm={t_llm:.1f}s total={t_total:.1f}s "
                f"({len(ayahs)} ayahs)"
            )
            return script

        except (PermanentError, QualityGateError):
            raise
        except Exception as e:
            raise ScriptGenerationError(
                f"Script generation failed: {e}",
                episode_number=episode_number,
                stage="script",
                cause=e,
            ) from e

    def _generate_meta_consolidated(
        self, info: SurahInfo, ayah_count: int,
    ) -> Dict[str, Any]:
        prompt = _build_episode_meta_prompt(info["name"], info["surah"], ayah_count)
        return self._call_llm(prompt)

    def _generate_ayah_scenes_parallel(
        self,
        ayahs: List[Dict[str, Any]],
        episode_number: int,
        info: SurahInfo,
    ) -> List[Dict[str, Any]]:
        total = len(ayahs)
        adapter_count = max(1, len(self._adapter_names))
        # v22.5 FINAL: Force serial execution when only 1 Gemini adapter is in
        # the pool. Parallelism here would race the rate limiter and trigger
        # 429s. With 1 adapter at 4 RPM, 7 ayahs take ~105s. That's still
        # well under any reasonable Phase 1 budget.
        gemini_count = sum(1 for n in self._adapter_names if n.startswith("gemini-"))
        if gemini_count <= 1:
            max_workers = 1
        else:
            max_workers = min(adapter_count, 4)

        logger.info(
            f"🎬 Generating {total} ayahs (max_workers={max_workers}, "
            f"adapters={self._adapter_names})"
        )

        scenes: List[Optional[Dict[str, Any]]] = [None] * total

        def _gen_one(i: int) -> Tuple[int, Dict[str, Any]]:
            ayah = ayahs[i]
            adapter_name = self._adapter_names[i % adapter_count]
            data = self._generate_one_ayah_with_retry(
                ayah=ayah,
                episode_number=episode_number,
                surah_name=info["name"],
                scene_index=i,
                total_scenes=total,
                attempts=self._engine_cfg.script_max_ayah_attempts,
                preferred_adapter=adapter_name,
            )
            return i, data

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ayah"
        ) as executor:
            futures = [executor.submit(_gen_one, i) for i in range(total)]
            for fut in concurrent.futures.as_completed(futures):
                i, data = fut.result()
                ayah = ayahs[i]
                # v16: combined text uses analogy_text (not story_text)
                combined_text = " ".join([
                    ayah["text"],
                    data.get("analogy_text", "") or data.get("story_text", ""),
                    data.get("explain_text", ""),
                ])
                scene_type = data.get("visual_scene_hint") or pick_visual_scene(combined_text)
                from core.models import VisualScene
                valid_scenes = {s.value for s in VisualScene}
                if scene_type not in valid_scenes:
                    scene_type = pick_visual_scene(combined_text)

                emotion = data.get("scene_emotion", pick_emotion(scene_type))
                palette = pick_palette(scene_type)

                # v16: store as story_text for backward compat (data model unchanged)
                # but conceptually it's "analogy_text"
                analogy = data.get("analogy_text") or data.get("story_text", "")

                # v18: build structured visual prompt via VisualPromptEngineer
                # Falls back to legacy if LLM didn't return structured fields
                from engines.visual_prompt_engineer import VisualPromptEngineer
                engineer = VisualPromptEngineer()
                if data.get("visual_subject") and data.get("visual_action"):
                    visual_pos, visual_neg = engineer.build_prompt(
                        subject=data.get("visual_subject", ""),
                        action=data.get("visual_action", ""),
                        environment=data.get("visual_environment", ""),
                        emotion=emotion,
                    )
                else:
                    # Legacy fallback
                    visual_pos, visual_neg = engineer.build_from_legacy(
                        data.get("visual_prompt", ""),
                        emotion=emotion,
                    )

                scenes[i] = {
                    "scene_id": 10 + i,
                    "ayah": ayah,
                    "hook_text": humanize_arabic(data.get("hook_text", "")),
                    "intro_text": humanize_arabic(data.get("intro_text", "")),
                    "story_text": humanize_arabic(analogy),  # holds analogy in v16
                    "explain_text": humanize_arabic(data.get("explain_text", "")),
                    "moral_text": humanize_arabic(data.get("moral_text", "")),
                    "scene_emotion": emotion,
                    "visual_prompt": visual_pos,  # v18: locked-style prompt
                    "visual_negative_prompt": visual_neg,  # v18: NEW field
                    "visual_subject": data.get("visual_subject", ""),
                    "visual_action": data.get("visual_action", ""),
                    "visual_environment": data.get("visual_environment", ""),
                    "visual_scene": scene_type,
                    "palette": palette,
                    "keywords": extract_keywords(data.get("explain_text", "")),
                }

        result = [s for s in scenes if s is not None]
        if len(result) != total:
            raise ScriptGenerationError(
                f"Lost {total - len(result)} ayahs during parallel generation",
                episode_number=episode_number, stage="script.parallel",
            )
        return result

    def _generate_one_ayah_with_retry(
        self,
        ayah: Dict[str, Any],
        episode_number: int,
        surah_name: str,
        scene_index: int,
        total_scenes: int,
        attempts: int,
        preferred_adapter: Optional[str] = None,
    ) -> Dict[str, Any]:
        prompt = _build_ayah_prompt(
            ayah_text=ayah["text"],
            ayah_number=ayah["number"],
            surah_name=surah_name,
            scene_index=scene_index,
            total_scenes=total_scenes,
            episode_number=episode_number,
        )

        last_err: Optional[Exception] = None
        last_output: Optional[Dict[str, Any]] = None
        for attempt in range(1, attempts + 1):
            try:
                if last_output and last_err:
                    correction = (
                        f"\n\n[محاولة سابقة فشلت بسبب]: {last_err}\n"
                        f"[تجنب نفس الخطأ في هذه المحاولة]"
                    )
                    full_prompt = prompt + correction
                else:
                    full_prompt = prompt

                if preferred_adapter and preferred_adapter in self._adapters:
                    try:
                        data = self._call_llm_direct(preferred_adapter, full_prompt)
                    except Exception:
                        data = self._call_llm(full_prompt)
                else:
                    data = self._call_llm(full_prompt)

                # v16: hook_text and analogy_text are core; intro_text/explain are required
                if not data.get("hook_text"):
                    raise ValidationError(
                        f"Missing hook_text for ayah {ayah['number']} (v16 requires hook)"
                    )
                if not data.get("explain_text"):
                    raise ValidationError(
                        f"Missing explain_text for ayah {ayah['number']}"
                    )
                # Backward compat: accept story_text if model returned old field
                if not data.get("analogy_text") and data.get("story_text"):
                    data["analogy_text"] = data["story_text"]
                return data
            except Exception as e:
                last_err = e
                last_output = data if 'data' in locals() else None
                logger.warning(
                    f"⚠️ Ayah {ayah['number']} attempt {attempt}/{attempts}: {e}"
                )
                if attempt < attempts:
                    time.sleep(1.5 * attempt)

        raise ScriptGenerationError(
            f"Failed ayah {ayah['number']} after {attempts} attempts",
            episode_number=episode_number,
            stage="script.ayah",
            cause=last_err,
        )

    def _assemble(
        self,
        episode_number: int,
        info: SurahInfo,
        meta: Dict[str, Any],
        ayah_scenes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        intro_text = humanize_arabic(meta.get("intro_text", ""))
        outro_text = humanize_arabic(meta.get("outro_text", ""))
        cta_text = humanize_arabic(meta.get("cta_text", "")) or None

        result: Dict[str, Any] = {
            "episode_number": episode_number,
            "surah_name": info["name"],
            "title": meta.get("title", f"سورة {info['name']}"),
            "youtube_title": meta.get("youtube_title", f"سورة {info['name']} - تفسير عميق"),
            "youtube_description": meta.get("youtube_description", ""),
            "youtube_tags": meta.get("youtube_tags", [info["name"], "قرآن", "تفسير"]),
            "cta_text": cta_text,
            "intro_scene": {
                "scene_id": 1,
                "scene_type": "intro",
                "narrator_text": intro_text,
                "visual_prompt": meta.get("intro_visual", ""),
                "visual_scene": "starry_night",
                "palette": "night_stars",
                "keywords": extract_keywords(intro_text),
                "mood": "intro",
                "scene_emotion": "excited",
            },
            "ayah_scenes": ayah_scenes,
            "mid_scenes": [],
            "outro_scene": {
                "scene_id": 99,
                "scene_type": "outro",
                "narrator_text": outro_text,
                "visual_prompt": meta.get("outro_visual", ""),
                "visual_scene": "starry_night",
                "palette": "night_stars",
                "keywords": extract_keywords(outro_text),
                "mood": "outro",
                "scene_emotion": "peaceful",
            },
        }

        if self._add_ssml:
            result["intro_scene"]["narrator_text_ssml"] = to_elevenlabs_ssml(intro_text)
            result["outro_scene"]["narrator_text_ssml"] = to_elevenlabs_ssml(outro_text)
            if cta_text:
                result["cta_text_ssml"] = to_elevenlabs_ssml(cta_text)
            for scene in result["ayah_scenes"]:
                for field in ("hook_text", "intro_text", "story_text", "explain_text", "moral_text"):
                    if scene.get(field):
                        scene[f"{field}_ssml"] = to_elevenlabs_ssml(scene[field])

        return result

    def _script_path(self, episode_number: int) -> Path:
        return self._paths.temp_episodes / f"episode_{episode_number:03d}.json"

    def _save_atomic(self, episode_number: int, script: EpisodeScript) -> None:
        path = self._script_path(episode_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        logger.info(f"✅ Script saved: {path}")

    def health_report(self) -> dict:
        return self._pool.health_report()
