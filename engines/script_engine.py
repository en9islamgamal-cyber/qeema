"""
engines/script_engine.py — VALUE / QEEMA v13.0 (Full Human‑like Speech + Smart Crafting)
============================================================================================
- Smart batch prompt crafting (one template per surah)
- Diacritic removal + natural pauses
- Optional SSML wrapping for ElevenLabs professional TTS
"""
from __future__ import annotations

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
from core.resilience import (
    CircuitBreakerConfig,
    ProviderPool,
)
from data.curriculum import SurahInfo, get_episode_info
from infrastructure.llm_adapters import GeminiJsonAdapter, GroqJsonAdapter
from infrastructure.quran_text_api import fetch_verified_ayahs

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Egyptian system prompt (children's storyteller)
# ════════════════════════════════════════════════════════════════
EGYPTIAN_SYSTEM_PROMPT: str = """\
أنت "الجد أبو زياد"، حكواتي مصري الأصل، تحكي للأطفال المصريين (5-8 سنوات).

[قواعد اللهجة — صارمة]:
✅ استخدم: إيه، إزاي، عايز، فين، كده، يلا، خلاص، علشان، عشان، حلو، أهو، طيب، هنا، هناك
✅ استخدم: بنحب، بنعمل، بيقولوا، عاوزين، فاهمين
❌ تجنب: كيف، ماذا، أين، الآن، حسناً، حقاً، بالطبع، يا ترى
❌ تجنب: شو، هلق، هيك، كتير (شامية)

[نمط السرد]:
- جملة قصيرة (8-15 كلمة).
- نبرة دافئة كأنك تحكي قصة قبل النوم.
- استخدم التشبيهات البسيطة من حياة الطفل.
- تجنب كلمات العقاب (نار، عذاب، جحيم).
- لا تكرر نفس الكلمة في فقرة واحدة.

[التنسيق]:
- أجب بـ JSON صالح فقط — مفيش markdown, مفيش شرح خارجي.
- النصوص العربية بالعامية المصرية.
- visual_prompt قصير بالإنجليزية (2D flat children illustration style).
"""


# ════════════════════════════════════════════════════════════════
# Semantic mapping for visual scene selection
# ════════════════════════════════════════════════════════════════
_SCENE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "garden":        ("جنة", "حديقة", "زرع", "ثمر", "شجر", "ورد", "نبات",
                      "نعمة", "نعيم", "رزق", "خير", "بركة"),
    "sky":           ("سماء", "سماوات", "نجم", "نجوم", "قمر", "شمس",
                      "كون", "مجرة", "فضاء", "علو", "رفع"),
    "house":         ("بيت", "بيوت", "أم", "أب", "أهل", "أسرة",
                      "أخ", "أخت", "والدين", "أولاد"),
    "mosque":        ("صلاة", "ركوع", "سجود", "مسجد", "مساجد",
                      "عبادة", "خشوع", "أذان", "محراب"),
    "ocean":         ("بحر", "ماء", "نهر", "أنهار", "مطر",
                      "غيث", "سفينة", "موج", "سحاب"),
    "desert":        ("صحراء", "إبل", "ناقة", "رحلة", "سفر", "قافلة", "رمل"),
    "mountains":     ("جبل", "جبال", "صخر", "حجر"),
    "child_praying": ("طفل يصلي", "أطفال", "ذكر", "تسبيح", "حمد"),
    "family":        ("محبة", "رحمة", "مودة", "تعاون", "صدقة",
                      "إحسان", "بر", "صلة"),
}

_ARABIC_STOPWORDS: frozenset[str] = frozenset({
    "في", "من", "إلى", "على", "عن", "مع", "هو", "هي", "هم",
    "أن", "إن", "كان", "يكون", "ذا", "ذلك", "هذا", "هذه",
    "كل", "ما", "لا", "ثم", "أو", "و", "ف", "ال",
})

_PUNCT_RE = re.compile(r'[،.؟!:؛"\'()\[\]{}]')
_TASHKEEL_RE = re.compile(r'[\u064B-\u0652]')  # Arabic diacritics
_PUNCTUATION_SET = {'.', '،', '؛', '؟', '!', ':'}


def pick_visual_scene(text: str) -> str:
    """Choose a procedural scene type by keyword match."""
    if not text:
        return "abstract_warm"
    lower = text.lower()
    best = ("abstract_warm", 0)
    for scene, keywords in _SCENE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in lower)
        if score > best[1]:
            best = (scene, score)
    return best[0]


def extract_keywords(text: str, *, max_words: int = 5) -> List[str]:
    """Extract animation keywords (Arabic content words)."""
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


# ════════════════════════════════════════════════════════════════
# Humanization for TTS
# ════════════════════════════════════════════════════════════════
def humanize_arabic(text: str) -> str:
    """
    Remove all diacritics, ensure final punctuation,
    and add a natural mid-sentence pause for long sentences.
    """
    if not text:
        return text

    # Remove tashkeel
    cleaned = _TASHKEEL_RE.sub('', text)
    # Normalize combining marks
    cleaned = unicodedata.normalize('NFKD', cleaned)
    cleaned = ''.join(c for c in cleaned if not unicodedata.combining(c))

    # Ensure a sentence-ending pause mark
    if cleaned and cleaned[-1] not in _PUNCTUATION_SET:
        cleaned += '.'

    # Insert a breath comma if the sentence is long and lacks internal punctuation
    if len(cleaned) > 40 and '،' not in cleaned and '.' not in cleaned[:-1]:
        words = cleaned.split()
        if len(words) > 5:
            insert_pos = len(' '.join(words[:3]))
            cleaned = cleaned[:insert_pos] + '، ' + cleaned[insert_pos:]

    return cleaned.strip()


def to_elevenlabs_ssml(text: str, voice_style: str = "conversational") -> str:
    """
    Wrap humanized Arabic text in SSML suitable for ElevenLabs.
    - Replaces commas and periods with break tags for realistic breathing.
    - Wraps in <prosody> with default conversational settings.
    """
    # Insert breaks at natural punctuation
    ssml_text = text.replace("،", "،<break time='400ms'/>")
    ssml_text = ssml_text.replace(".", ".<break time='600ms'/>")
    ssml_text = ssml_text.replace("؟", "؟<break time='500ms'/>")
    ssml_text = ssml_text.replace("!", "!<break time='500ms'/>")

    # Wrap with speak and prosody
    ssml = (
        f'<speak>'
        f'<prosody rate="medium" pitch="+0%" volume="+0dB">'
        f'{ssml_text}'
        f'</prosody>'
        f'</speak>'
    )
    return ssml


# ════════════════════════════════════════════════════════════════
# Mood → Palette mapping
# ════════════════════════════════════════════════════════════════
def mood_to_palette(scene_type: str) -> str:
    """Pick a palette name based on scene semantic."""
    return {
        "sky": "night_stars",
        "mosque": "golden_hour",
        "ocean": "calm_blue",
        "garden": "lush_green",
        "child_praying": "golden_hour",
        "family": "warm_sunset",
    }.get(scene_type, "warm_sunset")


# ════════════════════════════════════════════════════════════════
# Meta‑prompts for intelligent prompt crafting
# ════════════════════════════════════════════════════════════════
META_SYSTEM_PROMPT = (
    "أنت مهندس أوامر (Prompt Engineer) خبير في كتابة تعليمات لمحرك ذكاء اصطناعي "
    "يعمل كحكواتي مصري للأطفال (5-8 سنوات). مهمتك هي صياغة برومبت تفصيلي بالعامية المصرية "
    "يُستخدم كمدخل للحكواتي ليولّد سكريبت قصة قرآنية عالي الجودة. اكتب البرومبت فقط بلا أي شرح."
)

META_INTRO_TEMPLATE = (
    "اكتب برومبت للحكواتي عشان يقدم مقدمة حلقة عن سورة {surah_name} للأطفال. "
    "البرومبت لازم يطلب: عنوان حلو وجذاب، وصف يوتيوب مشوق (~150 كلمة)، 5 وسوم عربية، "
    "مقدمة دافئة بالعامية المصرية (max 30 كلمة)، ومشهد بصري بديع (visual_prompt) بالإنجليزي. "
    "أضف تعليمات عن النبرة الدافئة وربط اسم السورة بحياة الطفل. التنسيق المطلوب JSON "
    "(title, youtube_title, youtube_description, youtube_tags, intro_text, visual_prompt)."
)

META_AYAH_TEMPLATE_BATCH = (
    "اكتب برومبت واحداً (template) بالعامية المصرية يُستخدم كتعليمات للحكواتي لشرح أي آية "
    "من سورة {surah_name} للأطفال. البرومبت لازم يحتوي على المتغير `{{ayah_text}}` "
    "اللي هيتم استبداله بنص الآية الحقيقية وقت التشغيل. اطلب من الحكواتي يشرح بأسلوب قصة "
    "من حياة الطفل، يستخدم ألفاظ مصرية، ويتجنب الترهيب. ويكون التنسيق JSON بحقول "
    "(intro_text, explain_text, visual_prompt)."
)

META_OUTRO_TEMPLATE = (
    "اكتب برومبت للحكواتي عشان يعمل خاتمة حلقة قرآنية للأطفال. البرومبت لازم يطلب: "
    "دعاء قصير قبل النوم بالعامية المصرية (max 30 كلمة)، ومشهد ليلي جميل (visual_prompt بالإنجليزي). "
    "التنسيق JSON (narrator_text, visual_prompt)."
)


# ════════════════════════════════════════════════════════════════
# ScriptEngine
# ════════════════════════════════════════════════════════════════
class ScriptEngine:
    """Production script generation engine with full TTS optimizations."""

    def __init__(
        self,
        *,
        api_keys: APIKeysConfig,
        paths: PathsConfig,
        engine_cfg: EngineConfig,
        quality_validator: Optional[QualityValidator] = None,
    ) -> None:
        self._paths: PathsConfig = paths
        self._engine_cfg: EngineConfig = engine_cfg
        self._quality_validator: Optional[QualityValidator] = quality_validator

        # Smart crafting flags
        self._enable_crafting: bool = getattr(engine_cfg, "enable_prompt_crafting", False)
        self._crafting_model: str = getattr(engine_cfg, "prompt_crafting_model", "gemini-2.5-flash")
        self._crafting_adapter = None

        # ElevenLabs SSML flag
        self._add_ssml: bool = getattr(engine_cfg, "add_ssml", False)

        self._adapters: Dict[str, LLMProvider] = {}
        self._pool: ProviderPool = ProviderPool(
            "script_llm", strategy="round_robin"
        )
        self._setup_providers(api_keys)

    def _setup_providers(self, api_keys: APIKeysConfig) -> None:
        for i, key in enumerate(api_keys.gemini_keys, start=1):
            try:
                name = f"gemini-{i}"
                self._adapters[name] = GeminiJsonAdapter(
                    key, model="gemini-2.5-flash", instance_name=name,
                )
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
                "No LLM providers configured. Set GEMINI_API_KEY or GROQ_API_KEY."
            )
        logger.info(
            f"✅ ScriptEngine: {len(self._adapters)} providers: "
            f"{list(self._adapters.keys())}"
        )

        if self._enable_crafting:
            if api_keys.gemini_keys:
                try:
                    self._crafting_adapter = GeminiJsonAdapter(
                        api_keys.gemini_keys[0],
                        model=self._crafting_model,
                        instance_name="crafting-gemini",
                    )
                    logger.info(
                        f"🧠 Prompt crafting ENABLED using model '{self._crafting_model}'"
                    )
                except Exception as e:
                    logger.warning(f"Failed to create crafting adapter: {e}. Crafting disabled.")
                    self._enable_crafting = False
            else:
                logger.warning("No Gemini key available for crafting. Crafting disabled.")
                self._enable_crafting = False

    # ───────────────────────────────────────────────────────────
    # Prompt crafting
    # ───────────────────────────────────────────────────────────
    def _craft_prompt(
        self,
        context_type: str,
        info: Optional[SurahInfo] = None,
    ) -> str:
        if not self._enable_crafting or self._crafting_adapter is None:
            return ""

        if context_type == "intro":
            meta_prompt = META_INTRO_TEMPLATE.format(surah_name=info["name"])
        elif context_type == "ayah_template":
            meta_prompt = META_AYAH_TEMPLATE_BATCH.format(surah_name=info["name"])
        elif context_type == "outro":
            meta_prompt = META_OUTRO_TEMPLATE
        else:
            logger.error(f"Unknown crafting context: {context_type}")
            return ""

        try:
            adapter = self._crafting_adapter
            if hasattr(adapter, "generate_text"):
                crafted = adapter.generate_text(meta_prompt, system=META_SYSTEM_PROMPT)
                if crafted:
                    logger.info(f"✨ Crafted {context_type} prompt (first 80 chars): {crafted[:80]}...")
                    return crafted.strip()
                else:
                    logger.warning(f"Crafted prompt for {context_type} is empty.")
                    return ""
            else:
                logger.error("Crafting adapter lacks generate_text(); skipping")
                return ""
        except Exception as e:
            logger.warning(f"Crafting failed for {context_type}: {e}")
            return ""

    # ───────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────
    def load_from_disk(self, episode_number: int) -> Optional[EpisodeScript]:
        path = self._script_path(episode_number)
        if not path.exists():
            return None
        try:
            return EpisodeScript.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as e:
            logger.warning(f"⚠️ Cached script {episode_number} invalid; regenerating: {e}")
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
            f"🚀 Generating episode {episode_number} "
            f"(Surah {info['name']}, ayahs {info['start']}-{info['end']})"
        )

        try:
            ayahs = fetch_verified_ayahs(info["surah"], info["start"], info["end"])
            intro_data = self._generate_intro(info)
            ayah_scenes_data = self._generate_ayah_scenes(ayahs, episode_number, info)
            outro_data = self._generate_outro()
            script_dict = self._assemble(episode_number, info, intro_data, ayah_scenes_data, outro_data)

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

    def health_report(self) -> dict:
        return self._pool.health_report()

    # ───────────────────────────────────────────────────────────
    # LLM invocation
    # ───────────────────────────────────────────────────────────
    def _call_llm(self, prompt: str, system: str = EGYPTIAN_SYSTEM_PROMPT) -> Dict[str, Any]:
        def _invoke(provider_name: str) -> Dict[str, Any]:
            return self._adapters[provider_name].generate_json(prompt, system)
        return self._pool.execute(_invoke)

    # ───────────────────────────────────────────────────────────
    # Stage prompts (crafting + fallback)
    # ───────────────────────────────────────────────────────────
    def _generate_intro(self, info: SurahInfo) -> Dict[str, Any]:
        default_prompt = (
            f"اكتب مقدمة بالعامية المصرية لحلقة عن سورة {info['name']} للأطفال.\n"
            "أجب بـ JSON صالح بالحقول:\n"
            "- title (string)\n"
            "- youtube_title (string, max 60 chars)\n"
            "- youtube_description (string, ~150 words)\n"
            "- youtube_tags (array of 5 Arabic tags)\n"
            "- intro_text (string, max 30 words)\n"
            "- visual_prompt (English short, 2D flat illustration)\n"
        )
        crafted = self._craft_prompt("intro", info=info)
        return self._call_llm(crafted if crafted else default_prompt)

    def _generate_outro(self) -> Dict[str, Any]:
        default_prompt = (
            "اكتب خاتمة بالعامية المصرية تتضمن دعاء قبل النوم لطفل.\n"
            "أجب بـ JSON بالحقول:\n"
            "- narrator_text (string, max 30 words)\n"
            "- visual_prompt (English short)\n"
        )
        crafted = self._craft_prompt("outro")
        return self._call_llm(crafted if crafted else default_prompt)

    def _generate_ayah_scenes(
        self,
        ayahs: List[Dict[str, Any]],
        episode_number: int,
        info: SurahInfo,
    ) -> List[Dict[str, Any]]:
        crafted_template = self._craft_prompt("ayah_template", info=info)

        scenes: List[Dict[str, Any]] = []
        for i, ayah in enumerate(ayahs):
            logger.info(f"📖 [ep{episode_number}] generating ayah {ayah['number']}")
            data = self._generate_one_ayah_with_template(
                ayah,
                episode_number,
                attempts=self._engine_cfg.script_max_ayah_attempts,
                crafted_template=crafted_template,
            )
            combined = f"{ayah['text']} {data.get('explain_text', '')}"
            scene_type = pick_visual_scene(combined)
            scenes.append({
                "scene_id": 10 + i,
                "ayah": ayah,
                "intro_text": data.get("intro_text", ""),
                "explain_text": data.get("explain_text", ""),
                "visual_prompt": data.get("visual_prompt", ""),
                "visual_scene": scene_type,
                "palette": mood_to_palette(scene_type),
                "keywords": extract_keywords(data.get("explain_text", "")),
            })
        return scenes

    def _generate_one_ayah_with_template(
        self,
        ayah: Dict[str, Any],
        episode_number: int,
        *,
        attempts: int,
        crafted_template: str,
    ) -> Dict[str, Any]:
        if crafted_template:
            if "{ayah_text}" not in crafted_template:
                logger.warning("Crafted template missing {ayah_text}, using it as-is with appended ayah.")
                final_prompt = crafted_template + f"\n\nالآية: {ayah['text']}"
            else:
                final_prompt = crafted_template.replace("{ayah_text}", ayah["text"])
        else:
            final_prompt = (
                f"الآية: {ayah['text']}\n\n"
                "اشرحها بالعامية المصرية لطفل صغير. اربطها بموقف من حياته اليومية.\n"
                "أجب بـ JSON بالحقول:\n"
                "- intro_text (string, max 25 words)\n"
                "- explain_text (string, max 35 words)\n"
                "- visual_prompt (English short, 2D illustration)\n"
            )

        last_err: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                data = self._call_llm(final_prompt)
                if not data.get("intro_text") or not data.get("explain_text"):
                    raise ValidationError(f"Missing fields in ayah {ayah['number']}")
                return data
            except Exception as e:
                last_err = e
                logger.warning(f"⚠️ Ayah {ayah['number']} attempt {attempt}/{attempts}: {e}")
                if attempt < attempts:
                    time.sleep(2.0 * attempt)

        raise ScriptGenerationError(
            f"Failed to generate ayah {ayah['number']} after {attempts} attempts",
            episode_number=episode_number,
            stage="script.ayah",
            context={"ayah_number": ayah["number"]},
            cause=last_err,
        )

    # ───────────────────────────────────────────────────────────
    # Assembly (with humanization & optional SSML)
    # ───────────────────────────────────────────────────────────
    def _assemble(
        self,
        episode_number: int,
        info: SurahInfo,
        intro: Dict[str, Any],
        ayah_scenes: List[Dict[str, Any]],
        outro: Dict[str, Any],
    ) -> Dict[str, Any]:
        intro_text = humanize_arabic(intro.get("intro_text", ""))
        outro_text = humanize_arabic(outro.get("narrator_text", ""))

        # Process ayah scenes: humanize narration texts
        for scene in ayah_scenes:
            scene["intro_text"] = humanize_arabic(scene.get("intro_text", ""))
            scene["explain_text"] = humanize_arabic(scene.get("explain_text", ""))

        # --- Optional SSML variants for ElevenLabs ---
        result = {
            "episode_number": episode_number,
            "surah_name": info["name"],
            "title": intro.get("title", f"سورة {info['name']}"),
            "youtube_title": intro.get("youtube_title", f"سورة {info['name']} للأطفال"),
            "youtube_description": intro.get("youtube_description", ""),
            "youtube_tags": intro.get("youtube_tags", [info["name"], "قرآن", "أطفال"]),
            "intro_scene": {
                "scene_id": 1,
                "scene_type": "intro",
                "narrator_text": intro_text,
                "visual_prompt": intro.get("visual_prompt", ""),
                "visual_scene": pick_visual_scene(intro_text + " " + info["name"]),
                "palette": "golden_hour",
                "keywords": extract_keywords(intro_text),
                "mood": "intro",
            },
            "ayah_scenes": ayah_scenes,
            "mid_scenes": [],
            "outro_scene": {
                "scene_id": 99,
                "scene_type": "outro",
                "narrator_text": outro_text,
                "visual_prompt": outro.get("visual_prompt", ""),
                "visual_scene": "sky",
                "palette": "night_stars",
                "keywords": extract_keywords(outro_text),
                "mood": "outro",
            },
        }

        if self._add_ssml:
            # Add SSML representations for all narrator texts
            result["intro_scene"]["narrator_text_ssml"] = to_elevenlabs_ssml(intro_text)
            result["outro_scene"]["narrator_text_ssml"] = to_elevenlabs_ssml(outro_text)
            for scene in result["ayah_scenes"]:
                scene["intro_text_ssml"] = to_elevenlabs_ssml(scene["intro_text"])
                scene["explain_text_ssml"] = to_elevenlabs_ssml(scene["explain_text"])

        return result

    # ───────────────────────────────────────────────────────────
    # Persistence
    # ───────────────────────────────────────────────────────────
    def _script_path(self, episode_number: int) -> Path:
        return self._paths.temp_episodes / f"episode_{episode_number:03d}.json"

    def _save_atomic(self, episode_number: int, script: EpisodeScript) -> None:
        path = self._script_path(episode_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        logger.info(f"✅ Script saved: {path}")