"""
engines/script_engine.py — VALUE / QEEMA v11.0 (Production)
=================================================================
LLM-driven script generation.

[Pipeline]
1. Validate episode_number against curriculum
2. Check on-disk cache (atomic resume)
3. Fetch verified ayah text from Quran API
4. Generate intro / per-ayah / outro via LLM pool
5. Run quality gate
6. Persist atomically

[Key Improvements vs v10]
- ProviderPool replaces shared-state self.ptr (thread-safe, circuit-breaker)
- Iterative retry per ayah (no recursion)
- Quality gate IS wired in (was dead code before)
- Atomic file persistence (no half-written caches)
- Stable typing via core.models (Pydantic validation at every step)
"""
from __future__ import annotations

import json
import logging
import re
import time
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
# ScriptEngine
# ════════════════════════════════════════════════════════════════
class ScriptEngine:
    """Production script generation engine."""

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

    # ───────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────
    def load_from_disk(self, episode_number: int) -> Optional[EpisodeScript]:
        """Load + validate cached script. Returns None if missing or invalid."""
        path = self._script_path(episode_number)
        if not path.exists():
            return None
        try:
            return EpisodeScript.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as e:
            logger.warning(
                f"⚠️ Cached script {episode_number} invalid; regenerating: {e}"
            )
            return None

    def generate(self, episode_number: int) -> EpisodeScript:
        """
        Generate (or load) a complete EpisodeScript.

        Raises:
            ValidationError, ScriptGenerationError, QualityGateError
        """
        try:
            info: SurahInfo = get_episode_info(episode_number)
        except KeyError as e:
            raise ValidationError(
                str(e),
                episode_number=episode_number,
                stage="script.lookup",
            ) from e

        cached = self.load_from_disk(episode_number)
        if cached:
            logger.info(f"♻️ Episode {episode_number}: cached script loaded")
            return cached

        logger.info(
            f"🚀 Generating episode {episode_number} "
            f"(Surah {info['name']}, ayahs {info['start']}-{info['end']})"
        )

        try:
            ayahs = fetch_verified_ayahs(
                info["surah"], info["start"], info["end"],
            )
            intro_data = self._generate_intro(info)
            ayah_scenes_data = self._generate_ayah_scenes(
                ayahs, episode_number,
            )
            outro_data = self._generate_outro()
            script_dict = self._assemble(
                episode_number, info, intro_data, ayah_scenes_data, outro_data,
            )

            # Pydantic validation (catches LLM schema issues)
            script = EpisodeScript.model_validate(script_dict)

            # Quality gate
            if self._quality_validator is not None:
                report = self._quality_validator.validate(script_dict)
                if not report.passed:
                    raise QualityGateError(
                        f"Quality below threshold "
                        f"(score={report.overall_score:.1f}/100)",
                        score=report.overall_score,
                        critiques=report.critiques,
                        episode_number=episode_number,
                        stage="script.quality",
                    )
                logger.info(
                    f"✅ Quality: {report.overall_score:.1f}/100"
                )

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
    def _call_llm(
        self,
        prompt: str,
        system: str = EGYPTIAN_SYSTEM_PROMPT,
    ) -> Dict[str, Any]:
        """Call LLM via pool. Failover handled inside ProviderPool."""
        def _invoke(provider_name: str) -> Dict[str, Any]:
            return self._adapters[provider_name].generate_json(prompt, system)
        return self._pool.execute(_invoke)

    # ───────────────────────────────────────────────────────────
    # Stage prompts
    # ───────────────────────────────────────────────────────────
    def _generate_intro(self, info: SurahInfo) -> Dict[str, Any]:
        prompt = (
            f"اكتب مقدمة بالعامية المصرية لحلقة عن سورة {info['name']} للأطفال.\n"
            "أجب بـ JSON صالح بالحقول:\n"
            "- title (string)\n"
            "- youtube_title (string, max 60 chars)\n"
            "- youtube_description (string, ~150 words)\n"
            "- youtube_tags (array of 5 Arabic tags)\n"
            "- intro_text (string, max 30 words)\n"
            "- visual_prompt (English short, 2D flat illustration)\n"
        )
        return self._call_llm(prompt)

    def _generate_outro(self) -> Dict[str, Any]:
        prompt = (
            "اكتب خاتمة بالعامية المصرية تتضمن دعاء قبل النوم لطفل.\n"
            "أجب بـ JSON بالحقول:\n"
            "- narrator_text (string, max 30 words)\n"
            "- visual_prompt (English short)\n"
        )
        return self._call_llm(prompt)

    def _generate_ayah_scenes(
        self,
        ayahs: List[Dict[str, Any]],
        episode_number: int,
    ) -> List[Dict[str, Any]]:
        scenes: List[Dict[str, Any]] = []
        for i, ayah in enumerate(ayahs):
            logger.info(
                f"📖 [ep{episode_number}] generating ayah {ayah['number']}"
            )
            data = self._generate_one_ayah(
                ayah, episode_number, attempts=self._engine_cfg.script_max_ayah_attempts,
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

    def _generate_one_ayah(
        self,
        ayah: Dict[str, Any],
        episode_number: int,
        *,
        attempts: int,
    ) -> Dict[str, Any]:
        prompt = (
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
                data = self._call_llm(prompt)
                # Verify schema
                if not data.get("intro_text") or not data.get("explain_text"):
                    raise ValidationError(
                        f"Missing intro_text/explain_text in ayah {ayah['number']}"
                    )
                return data
            except Exception as e:
                last_err = e
                logger.warning(
                    f"⚠️ Ayah {ayah['number']} attempt {attempt}/{attempts}: {e}"
                )
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
    # Assembly
    # ───────────────────────────────────────────────────────────
    def _assemble(
        self,
        episode_number: int,
        info: SurahInfo,
        intro: Dict[str, Any],
        ayah_scenes: List[Dict[str, Any]],
        outro: Dict[str, Any],
    ) -> Dict[str, Any]:
        intro_text: str = intro.get("intro_text", "")
        outro_text: str = outro.get("narrator_text", "")
        intro_scene_type = pick_visual_scene(intro_text + " " + info["name"])

        return {
            "episode_number": episode_number,
            "surah_name": info["name"],
            "title": intro.get("title", f"سورة {info['name']}"),
            "youtube_title": intro.get(
                "youtube_title", f"سورة {info['name']} للأطفال"
            ),
            "youtube_description": intro.get("youtube_description", ""),
            "youtube_tags": intro.get(
                "youtube_tags", [info["name"], "قرآن", "أطفال"]
            ),
            "intro_scene": {
                "scene_id": 1,
                "scene_type": "intro",
                "narrator_text": intro_text,
                "visual_prompt": intro.get("visual_prompt", ""),
                "visual_scene": intro_scene_type,
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

    # ───────────────────────────────────────────────────────────
    # Persistence
    # ───────────────────────────────────────────────────────────
    def _script_path(self, episode_number: int) -> Path:
        return self._paths.temp_episodes / f"episode_{episode_number:03d}.json"

    def _save_atomic(self, episode_number: int, script: EpisodeScript) -> None:
        path = self._script_path(episode_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            script.model_dump_json(indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.info(f"✅ Script saved: {path}")
