"""
engines/batch_engines.py — VALUE / QEEMA v22.6.1
=========================================================================
Batch engines that process all 7 ayahs in a SINGLE Gemini call,
with dedicated key per task, multi-key rotation on quota exhaustion,
and rate limiter integration.

v22.6.1 IMPROVEMENTS over v22.6:
  ✓ Multi-key rotation on 429 RESOURCE_EXHAUSTED (was: fail to legacy)
  ✓ Rate limiter integration (shared per-key sliding window)
  ✓ Retry on transient failures (503, network errors)
  ✓ Better diagnostic logging (which key, which retry, why fail)
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Pydantic Schemas — Strict, descriptive, per-task
# ════════════════════════════════════════════════════════════════

class AyahScriptOut(BaseModel):
    """Schema for a single ayah's script."""
    ayah_number: int = Field(description="Ayah number (1-7)")
    hook_text: str = Field(
        description="Opening hook (Egyptian Arabic, 1-2 sentences, "
                    "amazing fact or question)",
        max_length=300,
    )
    explain_text: str = Field(
        description="Main explanation (Egyptian Arabic, 2-4 sentences, "
                    "matches authentic tafsir, age 6-12)",
        max_length=600,
    )
    story_text: str = Field(
        description="Concrete analogy/story (Egyptian Arabic, 2-3 sentences)",
        max_length=500,
    )
    moral_text: str = Field(
        description="Take-away moral (Egyptian Arabic, 1 sentence)",
        max_length=200,
    )
    scene_emotion: str = Field(
        description="One of: warm, reverent, playful, peaceful, excited",
    )


class BatchScriptOut(BaseModel):
    """Full episode script — all 7 ayahs + meta."""
    title: str = Field(description="Episode title (Arabic, < 60 chars)", max_length=80)
    youtube_title: str = Field(
        description="YouTube title (Arabic, hook-driven, < 80 chars)",
        max_length=100,
    )
    youtube_description: str = Field(
        description="YouTube description (Arabic, 2-3 paragraphs)",
        max_length=2000,
    )
    intro_text: str = Field(
        description="Episode opening narration (Egyptian Arabic, 2-3 sentences)",
        max_length=400,
    )
    outro_text: str = Field(
        description="Episode closing narration (Egyptian Arabic, 2-3 sentences)",
        max_length=400,
    )
    ayahs: List[AyahScriptOut] = Field(
        description="One entry per ayah, in order (1-7)",
        min_length=1, max_length=10,
    )


class AyahReviewOut(BaseModel):
    """Religious review for one ayah."""
    ayah_number: int = Field(description="Ayah number being reviewed")
    passed: bool = Field(
        description="True if explanation matches authentic tafsir",
    )
    confidence: float = Field(
        description="Confidence 0.0-1.0", ge=0.0, le=1.0,
    )
    concerns: List[str] = Field(
        default_factory=list,
        description="Specific issues found, in Arabic. Empty if passed.",
        max_length=5,
    )


class BatchReviewOut(BaseModel):
    """Batch tafsir review — all 7 ayahs."""
    reviews: List[AyahReviewOut] = Field(
        description="Reviews for each ayah, in order",
        min_length=1, max_length=10,
    )


class AyahTTSOut(BaseModel):
    """TTS direction for one ayah."""
    ayah_number: int = Field(description="Ayah number")
    voice_speed: float = Field(
        description="Speech rate (0.85-1.10)", ge=0.85, le=1.10,
    )
    voice_stability: float = Field(
        description="ElevenLabs stability (0.30-0.70)", ge=0.30, le=0.70,
    )
    voice_style: float = Field(
        description="ElevenLabs style (0.30-0.70)", ge=0.30, le=0.70,
    )
    emphasis_words: List[str] = Field(
        default_factory=list, max_length=10,
        description="Words to emphasize (in Arabic)",
    )


class BatchTTSOut(BaseModel):
    """Batch TTS directions for all 7 ayahs."""
    directions: List[AyahTTSOut] = Field(
        description="TTS direction per ayah",
        min_length=1, max_length=10,
    )


class AyahVisualOut(BaseModel):
    """Visual prompt for one ayah."""
    ayah_number: int = Field(description="Ayah number")
    subject: str = Field(
        description="Main subject (English, for Leonardo.ai)",
        max_length=400,  # v22.6.2: was 200, Gemini occasionally returns
                         # rich watercolor descriptions that exceed it
    )
    environment: str = Field(
        description="Environment/setting (English)", max_length=400,
    )
    mood_lighting: str = Field(
        description="Mood + lighting (English)", max_length=300,
    )
    color_palette: str = Field(
        description="Color palette (English)", max_length=250,
    )


class BatchVisualOut(BaseModel):
    """Batch visual prompts for all 7 ayahs."""
    prompts: List[AyahVisualOut] = Field(
        description="Visual prompt per ayah",
        min_length=1, max_length=10,
    )


# ════════════════════════════════════════════════════════════════
# Multi-key client with rotation + rate limiting
# ════════════════════════════════════════════════════════════════

class _KeyClient:
    """A Gemini client paired with its rate limiter for the same key."""
    def __init__(
        self, name: str, client: Any, rate_limiter: Any = None,
    ) -> None:
        self.name = name
        self.client = client
        self.rate_limiter = rate_limiter  # already key-bound by adapter


def _build_clients_from_adapters(
    adapters: Dict[str, Any],
    preferred_order: Tuple[str, ...] = ("gemini-1", "gemini-2", "gemini-3"),
) -> List[_KeyClient]:
    """Extract clients in priority order from adapter dict.

    Each adapter has _client (genai.Client) and _rate_limiter (already
    bound to that adapter's API key). We use those directly — no need
    to look up rate limiters by key string.
    """
    clients: List[_KeyClient] = []
    for name in preferred_order:
        adapter = adapters.get(name)
        if adapter is None:
            continue
        client = getattr(adapter, "_client", None)
        rate_limiter = getattr(adapter, "_rate_limiter", None)
        if client is None:
            continue
        clients.append(_KeyClient(
            name=name, client=client, rate_limiter=rate_limiter,
        ))
    return clients


def _is_quota_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return (
        "resource_exhausted" in err_str
        or "429" in err_str
        or "quota" in err_str
        or "rate limit" in err_str
    )


def _is_transient_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return (
        "503" in err_str
        or "service unavailable" in err_str
        or "deadline exceeded" in err_str
        or "internal error" in err_str
        or "connection" in err_str
        or "timeout" in err_str
    )


def _call_gemini_with_rotation(
    key_clients: List[_KeyClient],
    prompt: str,
    schema_class: Type[BaseModel],
    *,
    model: str = "gemini-2.5-flash",
    temperature: float = 0.7,
    max_tokens: int = 8192,
    transient_max_retries: int = 2,
) -> Optional[BaseModel]:
    """Call Gemini with multi-key rotation + rate limiting + retries.

    Order per key:
      1. Acquire rate limiter slot (4 RPM sliding window per key)
      2. Make Gemini call with response_schema
      3. On 429: rotate to next key
      4. On 503/network: retry SAME key up to transient_max_retries times
      5. On schema validation failure: try next key

    Returns the parsed BaseModel instance, or None on total failure.
    """
    if not key_clients:
        logger.error("❌ No Gemini clients available for batch call")
        return None

    from google.genai import types as genai_types

    config = genai_types.GenerateContentConfig(
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema_class,
        max_output_tokens=max_tokens,
    )

    last_error: Optional[Exception] = None

    for key_idx, kc in enumerate(key_clients, start=1):
        logger.info(f"🔑 Trying {kc.name} (key {key_idx}/{len(key_clients)})")

        # Use the rate limiter that was already bound to this adapter's key
        limiter = kc.rate_limiter

        # Retry loop for transient errors on this key
        for attempt in range(1, transient_max_retries + 2):
            try:
                if limiter is not None:
                    limiter.acquire(max_wait_seconds=60.0)

                response = kc.client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )

                result = _parse_response(response, schema_class)
                if result is not None:
                    logger.info(
                        f"✅ {kc.name} succeeded "
                        f"({type(result).__name__}, attempt {attempt})"
                    )
                    return result

                logger.warning(
                    f"⚠️ {kc.name}: response unparseable, trying next key"
                )
                last_error = ValueError("Response did not match schema")
                break

            except Exception as e:
                last_error = e

                if _is_quota_error(e):
                    logger.warning(
                        f"⚠️ {kc.name}: daily quota exhausted (429), rotating"
                    )
                    break

                if _is_transient_error(e) and attempt <= transient_max_retries:
                    wait_s = 2.0 ** attempt
                    logger.warning(
                        f"⚠️ {kc.name}: transient error "
                        f"(attempt {attempt}/{transient_max_retries+1}), "
                        f"waiting {wait_s}s: {e}"
                    )
                    time.sleep(wait_s)
                    continue

                logger.warning(
                    f"⚠️ {kc.name}: non-retryable error "
                    f"({type(e).__name__}: {e}), trying next key"
                )
                break

    logger.error(
        f"❌ All {len(key_clients)} keys exhausted for batch call. "
        f"Last error: {last_error}"
    )
    return None


def _parse_response(
    response: Any,
    schema_class: Type[BaseModel],
) -> Optional[BaseModel]:
    """Parse Gemini response with progressive fallback.

    Layer 1: response.parsed (SDK auto-validation)
    Layer 2: json.loads + Pydantic.model_validate
    Layer 3: regex salvage + smart-quote norm + Pydantic
    """
    # Layer 1
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        if isinstance(parsed, schema_class):
            return parsed
        if isinstance(parsed, dict):
            try:
                return schema_class.model_validate(parsed)
            except Exception as e:
                logger.debug(f"Layer 1 dict→model failed: {e}")

    # Layer 2
    text = getattr(response, "text", "") or ""
    if not text:
        logger.warning("⚠️ Empty response.text from Gemini")
        return None

    text = re.sub(r'^\s*```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```\s*$', '', text)
    text = text.strip()

    text = (text
        .replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2018", "'").replace("\u2019", "'")
        .replace("\u00ab", '"').replace("\u00bb", '"')
    )

    try:
        data = json.loads(text)
        return schema_class.model_validate(data)
    except json.JSONDecodeError as e:
        logger.warning(
            f"⚠️ JSON parse failed at pos {e.pos}: trying regex salvage"
        )
    except Exception as e:
        logger.warning(f"⚠️ Pydantic validation failed: {e}")
        return None

    # Layer 3
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        logger.error(f"❌ No JSON object found in: {text[:200]}")
        return None

    salvage_str = match.group(0)
    salvage_str = re.sub(r',\s*([\]}])', r'\1', salvage_str)

    try:
        data = json.loads(salvage_str)
        return schema_class.model_validate(data)
    except Exception as e:
        logger.error(f"❌ Salvage layer failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# BatchScriptEngine — Phase 1
# ════════════════════════════════════════════════════════════════

class BatchScriptEngine:
    """Generate full episode (7 ayahs + intro/outro) in 1 Gemini call.

    Phase 1: Tries Key 1 → 2 → 3 on rotation.
    """

    def __init__(self, adapters: Dict[str, Any]) -> None:
        self._key_clients = _build_clients_from_adapters(
            adapters, preferred_order=("gemini-1", "gemini-2", "gemini-3"),
        )
        if not self._key_clients:
            raise ValueError(
                "BatchScriptEngine requires at least one Gemini adapter"
            )

    def generate_episode(
        self,
        surah_name: str,
        surah_number: int,
        ayahs: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> Optional[BatchScriptOut]:
        prompt = self._build_prompt(surah_name, surah_number, ayahs, tafsirs)
        logger.info(
            f"📝 BatchScriptEngine: generating {len(ayahs)} ayahs in 1 call "
            f"(surah {surah_number} {surah_name}, "
            f"{len(self._key_clients)} keys available)"
        )
        t0 = time.monotonic()

        result = _call_gemini_with_rotation(
            self._key_clients, prompt, BatchScriptOut,
            temperature=0.7, max_tokens=8192,
        )

        elapsed = time.monotonic() - t0
        if result is None:
            logger.error(
                f"❌ BatchScriptEngine failed in {elapsed:.1f}s — "
                f"caller will fall back to legacy per-ayah"
            )
            return None

        logger.info(
            f"✅ BatchScriptEngine: {len(result.ayahs)} ayahs done "
            f"in {elapsed:.1f}s (1 Gemini call instead of {len(ayahs)})"
        )
        return result

    @staticmethod
    def _build_prompt(
        surah_name: str,
        surah_number: int,
        ayahs: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> str:
        ayah_blocks = []
        for a in ayahs:
            num = a["number"]
            tafsir = tafsirs.get(num, "")
            ayah_blocks.append(
                f"━━━ آية {num} ━━━\n"
                f"النص: {a['text']}\n"
                f"التفسير المعتمد: {tafsir[:500]}"
            )
        ayah_section = "\n\n".join(ayah_blocks)

        return f"""اكتب حلقة تعليمية كاملة عن سورة {surah_name} ({len(ayahs)} آيات).

═══ الجمهور والأسلوب ═══
🎯 الجمهور: أطفال 6-12 سنة
🎬 الأسلوب: TED-Ed insight-first، شرح بسيط
🗣️ اللهجة: عامية مصرية حديثة (مش فصحى)

═══ الآيات والتفاسير المعتمدة ═══
{ayah_section}

═══ قواعد دقة دينية إلزامية (مهمة جداً) ═══

1. الشرح يطابق التفسير المعتمد فوق — مش يضيف معاني من خياله
2. الآيات اللي عن "اليوم الآخر" أو "يوم الحساب": ممنوع تشبيهها بالعمليات
   البيولوجية للجسم. اليوم الآخر يوم محدد، مش حالة دائمة.
3. العبادة والاستعانة: ممنوع تشبيهها بقوى ميكانيكية (مغناطيس، جاذبية).
   العبادة اختيار حر بمحبة، مش انجذاب آلي.
4. "غضب الله" أو "الضالين" (آية 7): ممنوع تشبيهها بأكل صحي/غير صحي.
   المعنى أعمق من ذلك بكتير.
5. "بسم الله": البركة والاستعانة، مش "كود سري" أو "كلمة سحرية".

═══ قواعد الكتابة ═══

1. لغة: عامية مصرية أصيلة
   ✓ "ربنا قال لينا في الآية دي حاجة جميلة"
   ✗ "إن الله تعالى يخبرنا"

2. جملة قصيرة: حد أقصى 12 كلمة. الطفل بيفقد التركيز في الجمل الطويلة.

3. تشبيهات ملموسة وصحيحة:
   ✓ آية 1: "زي البذرة لما تنمو، بنبدأ كل حاجة باسم ربنا"
   ✓ آية 6: "زي الخريطة بتدلنا على أحسن طريق نوصل بيه"

4. هياكل ممنوعة:
   ✗ "ربنا أمرنا أن نفعل كذا"
   ✗ "تعالوا نتدبر سويا"
   ✗ "نسأل الله أن يجعلنا..."

═══ الـ Output ═══

ارجع JSON مطابق للـ schema بالظبط:
- title, youtube_title, youtube_description
- intro_text, outro_text
- ayahs: array of {len(ayahs)} entries، كل واحدة فيها:
  - ayah_number (1 إلى {len(ayahs)})
  - hook_text (1-2 جملة جذابة)
  - explain_text (2-4 جمل، يطابق التفسير)
  - story_text (تشبيه ملموس وصحيح دينياً)
  - moral_text (جملة واحدة take-away)
  - scene_emotion (واحد من: warm, reverent, playful, peaceful, excited)
"""


# ════════════════════════════════════════════════════════════════
# BatchTafsirReviewer — Phase 1
# ════════════════════════════════════════════════════════════════

class BatchTafsirReviewer:
    """Review all 7 ayah scripts against authentic tafsir in 1 call.

    Phase 1: Tries Key 1 → 2 → 3 on rotation.
    """

    def __init__(self, adapters: Dict[str, Any]) -> None:
        self._key_clients = _build_clients_from_adapters(
            adapters, preferred_order=("gemini-1", "gemini-2", "gemini-3"),
        )
        if not self._key_clients:
            raise ValueError(
                "BatchTafsirReviewer requires at least one Gemini adapter"
            )

    def review_episode(
        self,
        ayah_scripts: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> Optional[BatchReviewOut]:
        prompt = self._build_prompt(ayah_scripts, tafsirs)
        logger.info(
            f"🔍 BatchTafsirReviewer: reviewing {len(ayah_scripts)} ayahs "
            f"in 1 call ({len(self._key_clients)} keys available)"
        )
        t0 = time.monotonic()

        result = _call_gemini_with_rotation(
            self._key_clients, prompt, BatchReviewOut,
            temperature=0.1, max_tokens=4096,
        )

        elapsed = time.monotonic() - t0
        if result is None:
            logger.error(f"❌ BatchTafsirReviewer failed in {elapsed:.1f}s")
            return None

        passed = sum(1 for r in result.reviews if r.passed)
        logger.info(
            f"✅ BatchTafsirReviewer: {passed}/{len(result.reviews)} passed "
            f"in {elapsed:.1f}s (1 call instead of {len(ayah_scripts)})"
        )
        return result

    @staticmethod
    def _build_prompt(
        ayah_scripts: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> str:
        blocks = []
        for s in ayah_scripts:
            num = s["number"]
            tafsir = tafsirs.get(num, "")
            blocks.append(
                f"━━━ آية {num} ━━━\n"
                f"التفسير المعتمد:\n{tafsir[:600]}\n\n"
                f"الشرح المقترح:\n{s.get('explain', '')}\n\n"
                f"التشبيه المقترح:\n{s.get('story', '')}\n"
            )
        body = "\n\n".join(blocks)

        return f"""أنت مراجع ديني خبير. راجع شروحات هذه الآيات للأطفال
وقارنها بالتفاسير المعتمدة.

{body}

═══ معايير القبول ═══
1. الشرح لا يضيف معاني خارج التفسير
2. التشبيه دقيق دينياً — مش يخلط بين أمور الدنيا والآخرة
3. لا يستخدم تشبيهات ميكانيكية للعبادة (مغناطيس، جاذبية)
4. لا يستخدم تشبيهات تقلل من جسامة المعنى الديني

لكل آية، قرر:
- passed: true إذا كل المعايير مستوفاة، false وإلا
- confidence: 0.0-1.0
- concerns: قائمة قصيرة بالمشاكل المحددة (بالعربي)، فارغة لو passed

ارجع JSON مطابق للـ schema بالظبط، مع review لكل آية.
"""


# ════════════════════════════════════════════════════════════════
# BatchTTSDirector — Phase 2
# ════════════════════════════════════════════════════════════════

class BatchTTSDirector:
    """Generate TTS voice directions for all 7 ayahs in 1 call.

    Phase 2: Uses Key 2 first (dedicated), falls back to 3 → 1.
    """

    def __init__(self, adapters: Dict[str, Any]) -> None:
        self._key_clients = _build_clients_from_adapters(
            adapters, preferred_order=("gemini-2", "gemini-3", "gemini-1"),
        )
        if not self._key_clients:
            raise ValueError(
                "BatchTTSDirector requires at least one Gemini adapter"
            )

    def direct_episode(
        self,
        ayah_scripts: List[Dict[str, Any]],
    ) -> Optional[BatchTTSOut]:
        prompt = self._build_prompt(ayah_scripts)
        logger.info(
            f"🎙️ BatchTTSDirector: directing {len(ayah_scripts)} ayahs "
            f"in 1 call (Phase 2, {len(self._key_clients)} keys)"
        )
        t0 = time.monotonic()

        result = _call_gemini_with_rotation(
            self._key_clients, prompt, BatchTTSOut,
            temperature=0.3, max_tokens=2048,
        )

        elapsed = time.monotonic() - t0
        if result is None:
            logger.error(f"❌ BatchTTSDirector failed in {elapsed:.1f}s")
            return None

        logger.info(
            f"✅ BatchTTSDirector: {len(result.directions)} directions "
            f"in {elapsed:.1f}s"
        )
        return result

    @staticmethod
    def _build_prompt(ayah_scripts: List[Dict[str, Any]]) -> str:
        blocks = []
        for s in ayah_scripts:
            num = s["number"]
            emotion = s.get("emotion", "warm")
            text = s.get("text", "")[:200]
            blocks.append(f"━━━ آية {num} (emotion: {emotion}) ━━━\n{text}")
        body = "\n\n".join(blocks)

        return f"""أنت مخرج صوتي للقصص الإسلامية للأطفال.
لكل آية، حدد إعدادات صوتية تناسب المشهد.

{body}

═══ القواعد ═══
- voice_speed: 0.85-1.10 (أبطأ للهيبة، أسرع للحماس)
- voice_stability: 0.30-0.70 (أعلى للثبات الموقر)
- voice_style: 0.30-0.70 (أعلى للتعبير العاطفي)
- emphasis_words: 0-5 كلمات للتأكيد عليها

ارجع JSON مع direction لكل آية في array واحدة.
"""


# ════════════════════════════════════════════════════════════════
# BatchVisualPromptEngine — Phase 2
# ════════════════════════════════════════════════════════════════

class BatchVisualPromptEngine:
    """Generate visual prompts for all 7 ayahs in 1 call.

    Phase 2: Uses Key 3 first (dedicated), falls back to 1 → 2.
    """

    def __init__(self, adapters: Dict[str, Any]) -> None:
        self._key_clients = _build_clients_from_adapters(
            adapters, preferred_order=("gemini-3", "gemini-1", "gemini-2"),
        )
        if not self._key_clients:
            raise ValueError(
                "BatchVisualPromptEngine requires at least one Gemini adapter"
            )

    def generate_visuals(
        self,
        ayah_scripts: List[Dict[str, Any]],
    ) -> Optional[BatchVisualOut]:
        prompt = self._build_prompt(ayah_scripts)
        logger.info(
            f"🎨 BatchVisualPromptEngine: prompting {len(ayah_scripts)} ayahs "
            f"in 1 call (Phase 2, {len(self._key_clients)} keys)"
        )
        t0 = time.monotonic()

        result = _call_gemini_with_rotation(
            self._key_clients, prompt, BatchVisualOut,
            temperature=0.6, max_tokens=4096,
        )

        elapsed = time.monotonic() - t0
        if result is None:
            logger.error(f"❌ BatchVisualPromptEngine failed in {elapsed:.1f}s")
            return None

        logger.info(
            f"✅ BatchVisualPromptEngine: {len(result.prompts)} prompts "
            f"in {elapsed:.1f}s"
        )
        return result

    @staticmethod
    def _build_prompt(ayah_scripts: List[Dict[str, Any]]) -> str:
        blocks = []
        for s in ayah_scripts:
            num = s["number"]
            text = s.get("explain", "")[:300]
            story = s.get("story", "")[:200]
            blocks.append(f"━━━ آية {num} ━━━\nشرح: {text}\nتشبيه: {story}")
        body = "\n\n".join(blocks)

        return f"""You are a visual director for an Islamic children's
educational video. For each ayah, generate Leonardo.ai-friendly visual
prompts (in English) that capture the meaning visually.

Style: Watercolor, paper texture, soft lighting, peaceful, child-friendly.
Avoid: Human faces with detail, religious symbols controversies, dark themes.

{body}

═══ For each ayah, provide ═══
- subject: main visual element (e.g., "a single seed sprouting in soil")
- environment: setting (e.g., "warm garden at golden hour")
- mood_lighting: emotional tone + light (e.g., "peaceful, soft golden light")
- color_palette: dominant colors (e.g., "warm yellows, soft greens")

Return JSON with prompts array, one entry per ayah.
"""
