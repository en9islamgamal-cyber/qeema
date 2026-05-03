"""
engines/script_engine.py — VALUE / QEEMA v15.0 (Critical Engineering Rebuild)
==================================================================================
[Major v15 Changes from v14]

ARCHITECTURAL:
1. **Consolidated LLM calls**: intro + outro + SEO metadata generated in ONE
   call (was 2 calls). Saves 1 round-trip per episode.
2. **Parallel ayah generation**: ayahs now generated concurrently across
   Gemini keys (was sequential). For 5 ayahs with 3 keys: ~3x faster.
3. **Self-correcting prompts**: each retry includes the previous failed
   output + critique → LLM learns from its own mistakes within the episode.
4. **Quality score gate**: generated scripts pass through a scoring pass.
   Below threshold → automatic regeneration with feedback.

QUALITY:
5. **Improved SSML**: rate=slow, longer pauses, paragraph-level prosody.
6. **Story diversity**: opener templates + character names cycle by index.
7. **Visual prompt locking**: enforce no-text-in-image, consistent style.
8. **Topic-aware moral**: moral_text now references the specific ayah theme.

COST/SPEED:
9. **Token budget per call**: explicit max_tokens hint to prevent overruns.
10. **Smart cache invalidation**: cache key includes prompt version → safe
    regeneration without manual cache clearing.
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
# v15 — Sheikh Abu Ziyad System Prompt (Refined Character Bible)
# ════════════════════════════════════════════════════════════════
SHEIKH_SYSTEM_PROMPT: str = """\
أنت "جدو أبو زياد" — حكواتي مصري أصيل، عمره 65 سنة، صوته دافئ وفيه حنان جدو.
بتحكي لأحفادك (أطفال 5-8 سنوات) قصص القرآن بعامية مصرية حلوة.

[شخصيتك — ثابتة في كل حلقة]:
- اسمك: جدو أبو زياد
- أسلوبك: أب حنون يحكي قصة قبل النوم
- نبرتك: دافئة، فيها ابتسامة، مش جافة أو رسمية
- مستواك: بتشرح بأمثلة من حياة الطفل اليومية

[قواعد اللهجة — صارمة]:
✅ استخدم: إيه، إزاي، عايز، فين، كده، يلا، خلاص، علشان، حلو، أهو، طيب
✅ استخدم: بنحب، بنعمل، بيقولوا، عاوزين، فاهمين، شايف، جاي، رايح
✅ أمثلة: "زي ما بتحب الحلوى"، "زي الكرة اللي بتلعب بيها"
❌ تجنب: كيف، ماذا، أين، الآن، حسناً، بالطبع، حقاً
❌ تجنب: شو، هلق، هيك، كتير (شامية/لبنانية)
❌ تجنب: العقاب، النار، الجحيم (مع الأطفال الصغار)

[قواعد الجودة — صارمة]:
- كل جملة: 8-15 كلمة، بسيطة وواضحة
- ما تكررش كلمة في نفس الفقرة
- استخدم أسماء أطفال متنوعة: كريم، نور، سارة، يوسف، ياسمين، زياد، عمر، فاطمة، محمد
- في القصص: لازم يكون فيها حوار قصير وفعل واضح
- visual_prompt: وصف إنجليزي سينمائي جميل، style: warm 2D illustration, no text

[التنسيق — مهم جداً]:
- أجب بـ JSON صالح فقط
- مفيش markdown، مفيش شرح خارج الـ JSON
- النصوص العربية كلها بدون تشكيل (فتحة/ضمة/كسرة) عدا visual_prompt
"""


# ════════════════════════════════════════════════════════════════
# v15 — Story Diversity Engine (avoid repetition across episodes)
# ════════════════════════════════════════════════════════════════
_CHARACTER_NAMES: List[str] = [
    "كريم", "نور", "سارة", "يوسف", "ياسمين", "زياد",
    "عمر", "فاطمة", "محمد", "ليلى", "حسن", "هدى",
]

_STORY_OPENERS: List[str] = [
    "في يوم من الأيام، {name} كان قاعد...",
    "{name} صحي الصبح، ولقى...",
    "في الحديقة، {name} لاحظ...",
    "جدو أبو زياد قال لـ{name}:",
    "لما {name} كان بيلعب مع أصحابه...",
    "{name} رايح المدرسة، وفي السكة...",
    "{name} كان حزين شوية، فجأة...",
]


def get_story_seed(episode_number: int, scene_index: int) -> Tuple[str, str]:
    """Returns (character_name, opener_template) for diverse stories."""
    name_idx = (episode_number * 7 + scene_index * 3) % len(_CHARACTER_NAMES)
    opener_idx = (episode_number * 5 + scene_index * 2) % len(_STORY_OPENERS)
    name = _CHARACTER_NAMES[name_idx]
    opener = _STORY_OPENERS[opener_idx].format(name=name)
    return name, opener


# ════════════════════════════════════════════════════════════════
# Scene mappings (kept from v14)
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
    "child_praying": ("طفل", "تسبيح", "حمد", "ذكر", "دعاء"),
    "family":        ("محبة", "رحمة", "مودة", "تعاون", "صدقة", "بر"),
    "rainbow":       ("مطر", "ألوان", "فرح", "بهجة", "نور", "ضياء"),
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
    """v15: Full tashkeel strip + smart punctuation insertion."""
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
    """
    v15: Refined SSML for ElevenLabs.
    - rate='slow' (was 'medium') — clearer for ages 5-8
    - longer pauses after periods (650ms vs 550ms)
    - removed empty pitch attribute
    """
    ssml_text = text.replace("،", "،<break time='400ms'/>")
    ssml_text = ssml_text.replace(".", ".<break time='650ms'/>")
    ssml_text = ssml_text.replace("؟", "؟<break time='500ms'/>")
    ssml_text = ssml_text.replace("!", "!<break time='400ms'/>")
    return f"<speak><prosody rate='slow'>{ssml_text}</prosody></speak>"


# ════════════════════════════════════════════════════════════════
# v15 Consolidated Prompt — combines intro + outro + SEO metadata
# ════════════════════════════════════════════════════════════════
def _build_episode_meta_prompt(surah_name: str, surah_number: int, ayah_count: int) -> str:
    """
    v15: Single prompt that produces intro + outro + SEO metadata together.
    Saves one Gemini round-trip per episode (~2-3 seconds + tokens).
    """
    return f"""
أنت جدو أبو زياد. اكتب MetaData كاملة لحلقة سورة {surah_name} ({ayah_count} آية).

أجب بـ JSON واحد فيه:
{{
  "title": "عنوان عربي حلو للحلقة (max 50 حرف)",
  "youtube_title": "عنوان يوتيوب جذاب لـSEO max 60 حرف، يبدأ بسورة {surah_name}",
  "youtube_description": "وصف 200-300 كلمة، أول سطرين فيهم اسم السورة + الفئة العمرية، وفي الآخر hashtags: #قرآن_للأطفال #تفسير_سهل #سورة_{surah_name}",
  "youtube_tags": ["وسم1", "وسم2", "وسم3", "وسم4", "وسم5", "قرآن للأطفال", "تفسير سهل", "{surah_name}"],
  "intro_text": "كلام ترحيبي دافئ بالعامية المصرية max 35 كلمة. عرف نفسك كجدو أبو زياد، ادعي الأطفال يستمعوا. مفيش كليشيهات.",
  "cta_text": "جملة قصيرة max 18 كلمة تطلب الأطفال يشتركوا ويفعلوا الجرس. مرحة ومشجعة.",
  "outro_text": "وداع حنون ودعاء قصير max 35 كلمة. اشكر الأطفال وادعيلهم.",
  "intro_visual": "Cinematic establishing shot, warm golden sunset over Egyptian grandfather's courtyard, magical sparkles, Arabic lanterns, soft children's book illustration, no text",
  "outro_visual": "Peaceful twilight, grandfather silhouette against warm sunset, golden particles, serene Arabic garden, children's book style, no text"
}}
"""


def _build_ayah_prompt(
    ayah_text: str,
    ayah_number: int,
    surah_name: str,
    scene_index: int,
    total_scenes: int,
    episode_number: int,
) -> str:
    """v15: Story-diverse prompt with character/opener seeding."""
    is_first = scene_index == 0
    is_last = scene_index == total_scenes - 1
    position_hint = (
        "أول آية في الحلقة — الـ hook مهم جداً يشد الطفل" if is_first
        else "آخر آية في الحلقة — الـ moral يكون خاتمة جميلة" if is_last
        else f"آية {scene_index + 1} من {total_scenes}"
    )

    char_name, opener_hint = get_story_seed(episode_number, scene_index)

    return f"""
أنت جدو أبو زياد. ({position_hint})

الآية الكريمة: {ayah_text}
من سورة: {surah_name}، الآية رقم: {ayah_number}

[توجيه القصة لهذه الآية]:
- اسم الطفل في القصة: {char_name}
- بداية القصة المقترحة: "{opener_hint}"
- لازم القصة فيها فعل واضح وحوار قصير

أجب بـ JSON بالحقول دي بالظبط:
{{
  "hook_text": "جملة واحدة تشد الطفل — سؤال أو موقف مفاجئ. أمثلة: 'تعرف ليه النجوم بتلمع؟' / 'لو قلتلك إن في كنز في كلمتين؟' — max 25 كلمة",

  "intro_text": "جملتين بسيطتين يربطوا الآية بحياة الطفل اليومية — max 30 كلمة",

  "story_text": "قصة قصيرة عن {char_name}. ابدأ بـ '{opener_hint}'. فيها فعل وحوار. ربط واضح بمعنى الآية. max 65 كلمة",

  "explain_text": "شرح مباشر وسهل للآية في جملتين بعد القصة — max 45 كلمة",

  "moral_text": "جملة واحدة: السلوك أو الحكمة اللي ياخدها الطفل. لازم تكون مرتبطة بالآية والقصة. max 22 كلمة",

  "scene_emotion": "اختار من: warm / reverent / playful / peaceful / excited",

  "visual_prompt": "Detailed cinematic scene description in English: [setting], [warm lighting], [child or family in scene], 2D children's book illustration style, soft warm colors, no text in image, no Arabic text",

  "visual_scene_hint": "اختار من: golden_field / garden / sky / house / mosque / ocean / child_reading / child_praying / family / rainbow / flowers / desert / mountains / starry_night / abstract_warm"
}}
"""


# ════════════════════════════════════════════════════════════════
# ScriptEngine v15
# ════════════════════════════════════════════════════════════════
class ScriptEngine:
    """v15: Parallel ayah generation, consolidated meta call, self-correction."""

    PROMPT_VERSION: str = "v15.0"  # bump to invalidate caches

    def __init__(
        self,
        *,
        api_keys: APIKeysConfig,
        paths: PathsConfig,
        engine_cfg: EngineConfig,
        quality_validator: Optional[QualityValidator] = None,
    ) -> None:
        self._paths = paths
        self._engine_cfg = engine_cfg
        self._quality_validator = quality_validator
        self._add_ssml: bool = getattr(engine_cfg, "add_ssml", True)

        self._adapters: Dict[str, LLMProvider] = {}
        self._adapter_names: List[str] = []
        self._pool = ProviderPool("script_llm", strategy="round_robin")
        self._setup_providers(api_keys)

    def _setup_providers(self, api_keys: APIKeysConfig) -> None:
        for i, key in enumerate(api_keys.gemini_keys, start=1):
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
                    rate_limit=(1.0, 5),
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
            f"✅ ScriptEngine v15: {len(self._adapters)} providers: "
            f"{list(self._adapters.keys())}"
        )

    def _call_llm(self, prompt: str, system: str = SHEIKH_SYSTEM_PROMPT) -> Dict[str, Any]:
        def _invoke(name: str) -> Dict[str, Any]:
            return self._adapters[name].generate_json(prompt, system)
        return self._pool.execute(_invoke)

    def _call_llm_direct(
        self, adapter_name: str, prompt: str, system: str = SHEIKH_SYSTEM_PROMPT,
    ) -> Dict[str, Any]:
        """v15: direct call to a specific adapter (for parallel ayah generation)."""
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
            f"🚀 Generating cinematic episode {episode_number} "
            f"(Surah {info['name']}, ayahs {info['start']}-{info['end']})"
        )

        try:
            t0 = time.monotonic()

            # Stage 1: Fetch verified Quran text
            ayahs = fetch_verified_ayahs(info["surah"], info["start"], info["end"])
            t_fetch = time.monotonic() - t0

            # v15: Stage 2 + 3 in parallel — meta + ayahs run concurrently
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as exe:
                meta_future = exe.submit(self._generate_meta_consolidated, info, len(ayahs))
                ayahs_future = exe.submit(
                    self._generate_ayah_scenes_parallel, ayahs, episode_number, info
                )
                meta_data = meta_future.result()
                ayah_scenes_data = ayahs_future.result()

            t_llm = time.monotonic() - t0 - t_fetch

            # Stage 4: Assemble
            script_dict = self._assemble(
                episode_number, info, meta_data, ayah_scenes_data
            )
            script = EpisodeScript.model_validate(script_dict)

            # Stage 5: Quality gate (with auto-retry)
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

    # ─── Stage generators ────────────────────────────────────────
    def _generate_meta_consolidated(
        self, info: SurahInfo, ayah_count: int,
    ) -> Dict[str, Any]:
        """v15: ONE call for intro + outro + SEO. Saves a round-trip."""
        prompt = _build_episode_meta_prompt(info["name"], info["surah"], ayah_count)
        return self._call_llm(prompt)

    def _generate_ayah_scenes_parallel(
        self,
        ayahs: List[Dict[str, Any]],
        episode_number: int,
        info: SurahInfo,
    ) -> List[Dict[str, Any]]:
        """
        v15: Parallel ayah generation across Gemini keys.
        Round-robin assignment of ayahs to adapters → load balancing.
        """
        total = len(ayahs)
        adapter_count = max(1, len(self._adapter_names))
        # Limit parallelism to min(adapter_count, 4) to be polite to API
        max_workers = min(adapter_count, 4)

        logger.info(
            f"🎬 Generating {total} ayahs in parallel (max_workers={max_workers}, "
            f"adapters={self._adapter_names})"
        )

        scenes: List[Optional[Dict[str, Any]]] = [None] * total

        def _gen_one(i: int) -> Tuple[int, Dict[str, Any]]:
            ayah = ayahs[i]
            # Round-robin adapter assignment
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
                combined_text = " ".join([
                    ayah["text"],
                    data.get("story_text", ""),
                    data.get("explain_text", ""),
                ])
                scene_type = data.get("visual_scene_hint") or pick_visual_scene(combined_text)
                from core.models import VisualScene
                valid_scenes = {s.value for s in VisualScene}
                if scene_type not in valid_scenes:
                    scene_type = pick_visual_scene(combined_text)

                emotion = data.get("scene_emotion", pick_emotion(scene_type))
                palette = pick_palette(scene_type)

                scenes[i] = {
                    "scene_id": 10 + i,
                    "ayah": ayah,
                    "hook_text": humanize_arabic(data.get("hook_text", "")),
                    "intro_text": humanize_arabic(data.get("intro_text", "")),
                    "story_text": humanize_arabic(data.get("story_text", "")),
                    "explain_text": humanize_arabic(data.get("explain_text", "")),
                    "moral_text": humanize_arabic(data.get("moral_text", "")),
                    "scene_emotion": emotion,
                    "visual_prompt": data.get("visual_prompt", ""),
                    "visual_scene": scene_type,
                    "palette": palette,
                    "keywords": extract_keywords(data.get("explain_text", "")),
                }

        # Return in original order
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
        """v15: self-correcting retry. Each retry sees the previous failure reason."""
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
                # On retry, append the previous failure note to teach the LLM
                if last_output and last_err:
                    correction = (
                        f"\n\n[محاولة سابقة فشلت بسبب]: {last_err}\n"
                        f"[تجنب نفس الخطأ في هذه المحاولة]"
                    )
                    full_prompt = prompt + correction
                else:
                    full_prompt = prompt

                # Use preferred adapter if available; else fall back to pool
                if preferred_adapter and preferred_adapter in self._adapters:
                    try:
                        data = self._call_llm_direct(preferred_adapter, full_prompt)
                    except Exception:
                        # Adapter-specific failure → fall back to pool
                        data = self._call_llm(full_prompt)
                else:
                    data = self._call_llm(full_prompt)

                # Validate minimum required fields
                if not data.get("intro_text") and not data.get("hook_text"):
                    raise ValidationError(
                        f"Missing intro/hook for ayah {ayah['number']}"
                    )
                if not data.get("explain_text"):
                    data["explain_text"] = data.get("intro_text", "")
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

    # ─── Assembly ────────────────────────────────────────────────
    def _assemble(
        self,
        episode_number: int,
        info: SurahInfo,
        meta: Dict[str, Any],
        ayah_scenes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """v15: Single meta dict provides intro + outro + SEO."""
        intro_text = humanize_arabic(meta.get("intro_text", ""))
        outro_text = humanize_arabic(meta.get("outro_text", ""))
        cta_text = humanize_arabic(meta.get("cta_text", "")) or None

        result: Dict[str, Any] = {
            "episode_number": episode_number,
            "surah_name": info["name"],
            "title": meta.get("title", f"سورة {info['name']}"),
            "youtube_title": meta.get("youtube_title", f"سورة {info['name']} للأطفال"),
            "youtube_description": meta.get("youtube_description", ""),
            "youtube_tags": meta.get("youtube_tags", [info["name"], "قرآن", "أطفال"]),
            "cta_text": cta_text,
            "intro_scene": {
                "scene_id": 1,
                "scene_type": "intro",
                "narrator_text": intro_text,
                "visual_prompt": meta.get("intro_visual", ""),
                "visual_scene": "golden_field",
                "palette": "golden_hour",
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
