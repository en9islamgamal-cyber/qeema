"""
engines/batch_engines.py — VALUE / QEEMA v22.6
=========================================================================
Batch engines that process all 7 ayahs in a SINGLE Gemini call,
with dedicated key per task to eliminate race conditions.

Architecture:
  Phase 1 (Day 1):
    Key 1 → BatchScriptEngine    (1 call, all 7 ayahs)
    Key 1 → BatchTafsirReviewer  (1 call, all 7 reviews)
  Phase 2 (Day 2):
    Key 2 → BatchTTSDirector     (1 call, all 7 SSML directions)
    Key 3 → BatchVisualPromptEngine  (1 call, all 7 visual prompts)

Total Gemini calls per episode: 4
Daily quota: 60 calls (3 keys × 20)
Episode = 7% of daily quota → 14 episodes/day theoretical max

Why this works (where multi-task failed):
  1. Pydantic response_schema with strict Field(description=...)
     forces Gemini to return EXACTLY the structure we need
  2. SDK auto-validates response → returns typed objects via response.parsed
  3. max_output_tokens explicit per task (not blindly 4096)
  4. Dedicated key per task → no rate limiter race conditions
  5. Salvage layer (regex) only as last resort — schema usually wins
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Pydantic Schemas — Strict, descriptive, per-task
# ════════════════════════════════════════════════════════════════

class AyahScriptOut(BaseModel):
    """Schema for a single ayah's script (one of 7 in batch output)."""
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
        min_length=1,
        max_length=10,
    )


class AyahReviewOut(BaseModel):
    """Religious review for one ayah."""
    ayah_number: int = Field(description="Ayah number being reviewed")
    passed: bool = Field(
        description="True if explanation matches authentic tafsir, "
                    "no doctrinal issues",
    )
    confidence: float = Field(
        description="Confidence 0.0-1.0",
        ge=0.0, le=1.0,
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


class SegmentTTSOut(BaseModel):
    """Voice direction for ONE segment (intro / hook / story / explain / moral / outro).

    v22.6: Maps 1:1 to legacy SegmentDirection. The orchestrator consumes
    `{segment_id: {directed_text, pace, pronunciation_notes}}` so we mirror
    that shape. Per-ayah voice knobs (speed/stability/style) are derived
    from `scene_emotion` via voice_emotion_mapper at synthesis time, not
    from this LLM call — that keeps audio identity stable across episodes.
    """
    segment_id: str = Field(
        description="Segment ID, e.g. 'intro_text', 'ayah_1.hook', "
                    "'ayah_3.story', 'outro_text'",
        max_length=80,
    )
    directed_text: str = Field(
        description="The original Arabic text WITH inserted <break time=\"NNNms\"/> "
                    "tags at natural pause points. Words must NOT be changed. "
                    "Use 300ms for normal commas, 500ms before key insights, "
                    "800ms before a moral.",
        max_length=2000,
    )
    pace: str = Field(
        description="One of: slow | normal | fast",
    )
    pace_reason: str = Field(
        default="",
        description="Brief justification (Arabic), 1 sentence",
        max_length=200,
    )
    pronunciation_notes: List[str] = Field(
        default_factory=list,
        description="Difficult Arabic words needing pronunciation hints. "
                    "Each item is a single word.",
        max_length=10,
    )


class BatchTTSOut(BaseModel):
    """Batch TTS directions — one entry per segment across all ayahs."""
    directions: List[SegmentTTSOut] = Field(
        description="Per-segment direction. Typical episode: 2 episode-level "
                    "(intro, outro) + 7 ayahs × 3 segments (hook, story, moral) "
                    "= 23 segments. Bound is generous to allow flexibility.",
        min_length=1, max_length=80,
    )


class AyahVisualOut(BaseModel):
    """Visual prompt for one ayah — 14 fields matching DeepVisualPromptResult.

    v22.6: We ask Gemini to fill all 3 cinematic layers (primitives + aesthetic
    + composition) in a single call, so this maps 1:1 to the legacy
    DeepVisualPromptResult.merge_to_leonardo_prompt() pipeline. The downstream
    VisualPromptEngineer.build_from_deep_result() consumes this directly.
    """
    ayah_number: int = Field(description="Ayah number")

    # ── Layer 1: scene primitives ─────────────────────────────────────
    subject: str = Field(
        description="Main subject of the scene, concrete and visualizable "
                    "(English, for Leonardo.ai). E.g., 'a single seed sprouting'",
        max_length=200,
    )
    action: str = Field(
        description="What the subject is doing (English). "
                    "E.g., 'gently breaking through soil'",
        max_length=200,
    )
    environment: str = Field(
        description="Setting/location (English). "
                    "E.g., 'warm garden, dew on leaves'",
        max_length=200,
    )
    time_of_day: str = Field(
        description="Time of day (English). E.g., 'golden hour', 'dawn'",
        max_length=80,
    )

    # ── Layer 2: aesthetic ────────────────────────────────────────────
    mood: str = Field(
        description="Emotional tone (English single phrase). "
                    "E.g., 'peaceful and reverent'",
        max_length=120,
    )
    color_palette: str = Field(
        description="Dominant colors (English). "
                    "E.g., 'warm ochre, soft sage, cream highlights'",
        max_length=200,
    )
    lighting_direction: str = Field(
        description="Lighting direction & quality (English). "
                    "E.g., 'soft side-lighting from upper left, diffused'",
        max_length=200,
    )
    atmospheric_elements: str = Field(
        description="Atmospheric details (English). "
                    "E.g., 'gentle dust motes, faint morning mist'",
        max_length=200,
    )

    # ── Layer 3: cinematic composition ────────────────────────────────
    camera_angle: str = Field(
        description="Camera angle (English). "
                    "E.g., 'low-angle close-up', 'wide overhead shot'",
        max_length=120,
    )
    depth_of_field: str = Field(
        description="Depth of field (English). "
                    "E.g., 'shallow DoF, background softly blurred'",
        max_length=150,
    )
    foreground: str = Field(
        description="Foreground element (English). E.g., 'cracked earth'",
        max_length=200,
    )
    midground: str = Field(
        description="Midground element (English, the main subject area)",
        max_length=200,
    )
    background: str = Field(
        description="Background element (English). E.g., 'distant hills'",
        max_length=200,
    )
    focal_point: str = Field(
        description="Where the eye lands first (English). "
                    "E.g., 'the sprout's emerging tip, lit from above'",
        max_length=200,
    )


class BatchVisualOut(BaseModel):
    """Batch visual prompts for all 7 ayahs."""
    prompts: List[AyahVisualOut] = Field(
        description="Visual prompt per ayah, in order (1..N)",
        min_length=1, max_length=10,
    )


# ════════════════════════════════════════════════════════════════
# Common helpers
# ════════════════════════════════════════════════════════════════

def _dump_failed_response(
    text: str,
    prompt: str,
    schema_class: type,
    error_summary: str,
) -> Optional[str]:
    """Save a malformed response to disk for post-mortem.

    Returns the path written to, or None if dumping failed (we never let
    a debug-aid failure crash the actual pipeline).
    """
    import os as _os
    import time as _t
    from pathlib import Path as _P
    try:
        out_dir = _P(_os.environ.get("QEEMA_DEBUG_DIR", "logs/gemini_failures"))
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = _t.strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"{schema_class.__name__}_{ts}.txt"
        path.write_text(
            f"=== Schema: {schema_class.__name__} ===\n"
            f"=== Error: {error_summary} ===\n"
            f"=== Prompt length: {len(prompt)} chars ===\n"
            f"=== Response length: {len(text)} chars ===\n"
            f"\n--- RAW RESPONSE ---\n{text}\n"
            f"--- END RAW RESPONSE ---\n",
            encoding="utf-8",
        )
        logger.info(f"💾 Failure dump → {path}")
        return str(path)
    except Exception as dump_err:
        logger.debug(f"Could not dump response: {dump_err}")
        return None


def _aggressive_json_clean(text: str) -> str:
    """Apply every known fix for Gemini's JSON-with-Arabic failure modes.

    Each transformation is independent and additive — earlier ones don't
    break later ones. Order matters for correctness, not for safety.

    Failure modes observed in production v22.6 (and what we do about them):
      1. Markdown code fences           → strip leading/trailing ```json ... ```
      2. Unicode smart quotes in Arabic → normalize to ASCII " and '
      3. Trailing commas before } or ]  → remove
      4. Unescaped raw newlines INSIDE strings (Arabic LLMs often add line
         breaks for readability — these break json.loads at the literal \n
         inside a quoted string) → escape them
      5. Unescaped raw \\t / \\r inside strings → escape them
      6. Stray BOM / zero-width chars   → strip
      7. Leading/trailing chatter ("تمام، هكتبلك:") → extract { … } block
    """
    # 0. BOM and zero-width characters at any position
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "")

    # 1. Markdown fences
    text = re.sub(r'^\s*```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```\s*$', '', text)
    text = text.strip()

    # 2. Smart quotes (Arabic LLMs often emit these around Arabic strings)
    text = (text
        .replace("\u201c", '"').replace("\u201d", '"')   # " "
        .replace("\u2018", "'").replace("\u2019", "'")   # ' '
        .replace("\u00ab", '"').replace("\u00bb", '"')   # « »
    )

    # 3. Extract the outermost {…} so leading/trailing chatter is dropped
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        text = match.group(0)

    # 4. Escape unescaped raw newlines/CR/tab INSIDE strings.
    # We walk the string and track whether we're inside a "..." string,
    # respecting backslash escapes. This is the most common Gemini-Arabic
    # failure: the model puts a literal newline inside a long Arabic
    # concern, breaking json.loads.
    out = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            out.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            out.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch == "\n":
            out.append("\\n")
            continue
        if in_string and ch == "\r":
            out.append("\\r")
            continue
        if in_string and ch == "\t":
            out.append("\\t")
            continue
        out.append(ch)
    text = "".join(out)

    # 5. Trailing commas before closing brackets/braces
    text = re.sub(r',\s*([\]}])', r'\1', text)

    return text


def _call_gemini_with_schema(
    client: Any,
    prompt: str,
    schema_class: type,
    *,
    model: str = "gemini-2.5-flash",
    temperature: float = 0.7,
    max_tokens: int = 8192,
) -> Optional[BaseModel]:
    """Make a Gemini call with Pydantic response_schema.

    Returns the parsed BaseModel instance, or None on failure.

    On any parse failure, dumps the raw response to disk for diagnosis
    (controlled by env QEEMA_DEBUG_DIR, default logs/gemini_failures/).
    """
    from google.genai import types as genai_types

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=temperature,
                response_mime_type="application/json",
                response_schema=schema_class,
                max_output_tokens=max_tokens,
            ),
        )
    except Exception as e:
        logger.error(f"❌ Gemini call failed: {e}")
        raise

    # Layer 1: response.parsed (SDK auto-validates if schema is satisfied)
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        logger.debug(f"✅ Schema-parsed via response.parsed ({type(parsed).__name__})")
        return parsed

    raw_text = response.text or ""
    if not raw_text:
        logger.warning(f"⚠️ Empty response text from Gemini for {schema_class.__name__}")
        return None

    # Layer 2: manual JSON parse + Pydantic validate, with aggressive cleaning
    cleaned = _aggressive_json_clean(raw_text)

    try:
        data = json.loads(cleaned)
        return schema_class.model_validate(data)
    except json.JSONDecodeError as je:
        logger.warning(
            f"⚠️ JSON parse failed at pos {je.pos} (msg: {je.msg}) "
            f"for {schema_class.__name__}; trying iterative recovery"
        )
        # Layer 3: iterative recovery — try truncating at each {/[ boundary
        # walking backwards. Useful when the response is truncated
        # mid-element but the prefix is valid.
        recovered = _try_iterative_json_recovery(cleaned)
        if recovered is not None:
            try:
                return schema_class.model_validate(recovered)
            except Exception as ve:
                logger.error(
                    f"❌ Recovered JSON didn't satisfy {schema_class.__name__}: {ve}"
                )

        # All layers failed — dump for post-mortem
        _dump_failed_response(
            text=raw_text, prompt=prompt, schema_class=schema_class,
            error_summary=f"json.JSONDecodeError at pos {je.pos}: {je.msg}",
        )
        return None
    except Exception as ve:
        logger.error(
            f"❌ Pydantic validation failed for {schema_class.__name__}: {ve}"
        )
        _dump_failed_response(
            text=raw_text, prompt=prompt, schema_class=schema_class,
            error_summary=f"Pydantic ValidationError: {ve}",
        )
        return None


def _try_iterative_json_recovery(text: str) -> Optional[Any]:
    """If the response was truncated mid-array (common when output hits
    max_tokens), try parsing successively-shorter prefixes that close
    the structure cleanly.

    Strategy: find the last position where we have a balanced JSON object,
    by walking the text and tracking brace/bracket depth. If we find a
    point where depth returns to 0 at a closing brace, parse up to there.

    Returns the parsed object, or None if no clean prefix exists.
    """
    depth_curly = 0
    depth_square = 0
    in_string = False
    escape_next = False
    last_balanced = -1

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
            if depth_curly == 0 and depth_square == 0:
                last_balanced = i
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
            if depth_curly == 0 and depth_square == 0:
                last_balanced = i

    if last_balanced < 0:
        return None

    candidate = text[: last_balanced + 1]
    try:
        return json.loads(candidate)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# BatchScriptEngine — generates ALL 7 ayahs in 1 call (Key 1)
# ════════════════════════════════════════════════════════════════

class BatchScriptEngine:
    """Generate full episode (7 ayahs + intro/outro) in 1 Gemini call.

    Uses Key 1 (dedicated to script generation in v22.6 architecture).
    Pydantic schema FORCES valid JSON output — no parse errors.
    """

    def __init__(self, gemini_client: Any) -> None:
        self._client = gemini_client

    def generate_episode(
        self,
        surah_name: str,
        surah_number: int,
        ayahs: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> Optional[BatchScriptOut]:
        """Generate full episode script in 1 call.

        Args:
            surah_name: Arabic surah name
            surah_number: Surah index (1-114)
            ayahs: [{"number": N, "text": "..."}, ...]
            tafsirs: {ayah_number: tafsir_text}

        Returns:
            BatchScriptOut with all 7 ayahs + meta, or None on failure.
        """
        prompt = self._build_prompt(surah_name, surah_number, ayahs, tafsirs)
        logger.info(
            f"📝 BatchScriptEngine: generating {len(ayahs)} ayahs in 1 call "
            f"(surah {surah_number} {surah_name})"
        )
        t0 = time.monotonic()

        result = _call_gemini_with_schema(
            self._client, prompt, BatchScriptOut,
            temperature=0.7, max_tokens=8192,
        )

        elapsed = time.monotonic() - t0
        if result is None:
            logger.error(
                f"❌ BatchScriptEngine failed in {elapsed:.1f}s — "
                f"will fall back to legacy per-ayah"
            )
            return None

        logger.info(
            f"✅ BatchScriptEngine: {len(result.ayahs)} ayahs done in {elapsed:.1f}s "
            f"(1 Gemini call instead of {len(ayahs)})"
        )
        return result

    @staticmethod
    def _build_prompt(
        surah_name: str,
        surah_number: int,
        ayahs: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> str:
        """Build the multi-ayah prompt with strict religious accuracy rules."""
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

═══ قواعد دقة دينية إلزامية ═══

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
# BatchTafsirReviewer — reviews ALL 7 ayahs in 1 call (Key 1)
# ════════════════════════════════════════════════════════════════

class BatchTafsirReviewer:
    """Review all 7 ayah scripts against authentic tafsir in 1 call.

    Uses Key 1 (same as script engine — they run sequentially in Phase 1).
    """

    def __init__(self, gemini_client: Any) -> None:
        self._client = gemini_client

    def review_episode(
        self,
        ayah_scripts: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> Optional[BatchReviewOut]:
        """Review all ayah scripts in 1 call.

        Args:
            ayah_scripts: [{"number": N, "explain": "...", "story": "..."}, ...]
            tafsirs: {ayah_number: authentic_tafsir}

        Returns:
            BatchReviewOut with reviews for each ayah, or None on failure.

        v22.6.2:
          - max_tokens raised 4096 → 16384. Episode 1 first run hit the
            4096 ceiling — output JSON truncated mid-Arabic-string at
            char ~330, salvage couldn't recover. With 7 ayahs each
            potentially having multiple Arabic concerns, 4096 was tight.
          - One automatic retry on schema-validation failure with a
            simplified prompt (drops some examples to leave more output
            budget). This handles transient Gemini decoder hiccups.
        """
        prompt = self._build_prompt(ayah_scripts, tafsirs)
        logger.info(
            f"🔍 BatchTafsirReviewer: reviewing {len(ayah_scripts)} ayahs in 1 call"
        )
        t0 = time.monotonic()

        result = _call_gemini_with_schema(
            self._client, prompt, BatchReviewOut,
            temperature=0.1, max_tokens=16384,
        )

        # v22.6.2: one retry with a simplified prompt if first call fails.
        # The retry uses lower temperature (0.0) for maximum determinism
        # and a tighter prompt to free up output budget.
        if result is None:
            elapsed1 = time.monotonic() - t0
            logger.warning(
                f"⚠️ BatchTafsirReviewer first attempt failed in {elapsed1:.1f}s — "
                f"retrying with simplified prompt"
            )
            simple_prompt = self._build_simplified_prompt(ayah_scripts, tafsirs)
            result = _call_gemini_with_schema(
                self._client, simple_prompt, BatchReviewOut,
                temperature=0.0, max_tokens=16384,
            )

        elapsed = time.monotonic() - t0
        if result is None:
            logger.error(
                f"❌ BatchTafsirReviewer failed (both attempts) in {elapsed:.1f}s"
            )
            return None

        passed = sum(1 for r in result.reviews if r.passed)
        logger.info(
            f"✅ BatchTafsirReviewer: {passed}/{len(result.reviews)} passed "
            f"in {elapsed:.1f}s (1 call instead of {len(ayah_scripts)})"
        )
        return result

    @staticmethod
    def _build_simplified_prompt(
        ayah_scripts: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> str:
        """Tighter retry prompt — drops the long red-flag examples and
        relies on the schema description to convey constraints. Frees up
        roughly 1.5K input tokens for output budget."""
        blocks = []
        for s in ayah_scripts:
            num = s["number"]
            tafsir = (tafsirs.get(num) or "")[:400]  # tighter cap
            blocks.append(
                f"آية {num}:\n"
                f"تفسير: {tafsir}\n"
                f"شرح: {s.get('explain', '')[:300]}\n"
                f"تشبيه: {s.get('story', '')[:200]}"
            )
        body = "\n\n".join(blocks)
        return (
            f"راجع شرح هذه الآيات للأطفال ضد التفاسير المعتمدة.\n\n{body}\n\n"
            f"لكل آية أعطِ: passed (bool), confidence (0-1), "
            f"concerns (قائمة قصيرة بالعربي).\n"
            f"red flags ممنوعة: تشبيه يوم الدين بالبيولوجيا، "
            f"العبادة بالمغناطيس، الضلال بالأكل، البسملة بالسحر."
        )

    @staticmethod
    def _build_prompt(
        ayah_scripts: List[Dict[str, Any]],
        tafsirs: Dict[int, str],
    ) -> str:
        """Build batch review prompt."""
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

        return f"""أنت مراجع ديني خبير (مستوى عالم أزهري). راجع شروحات هذه الآيات
الموجهة للأطفال (٦-١٢ سنة) وقارنها بالتفاسير المعتمدة.

{body}

═══ Red flags عقدية يجب فحصها صراحةً قبل أي شيء آخر ═══

افحص كل آية ضد القائمة دي. لو لقيت أي red flag → passed=false:

1. **آيات اليوم الآخر / يوم الحساب** (مثل: مالك يوم الدين):
   ❌ ممنوع تشبيهها بعمليات الجسم البيولوجية الدائمة (نبض القلب،
      الخلايا، التنفس). اليوم الآخر يوم محدد، مش حالة دائمة.

2. **آيات العبادة والاستعانة** (مثل: إياك نعبد):
   ❌ ممنوع تشبيهها بقوى ميكانيكية (المغناطيس، الجاذبية، الانجذاب).
      العبادة اختيار حر بمحبة، مش انجذاب آلي.

3. **آيات الغضب والضلال** (مثل: غير المغضوب عليهم ولا الضالين):
   ❌ ممنوع تشبيهها بالأكل الصحي مقابل الأكل غير الصحي.

4. **"بسم الله"**:
   ❌ ممنوع وصفها بـ "كود سري"، "كلمة سحرية".

═══ معايير القبول الإضافية ═══

1. الشرح لا يضيف معاني خارج التفسير المعتمد
2. التشبيه دقيق دينياً — مش يخلط بين أمور الدنيا والآخرة
3. لا يستخدم تشبيهات تقلل من جسامة المعنى الديني
4. مفيش حذف لمعنى أساسي موجود في التفسير

═══ الـ Output ═══

لكل آية، قرر:
- passed: true إذا كل المعايير مستوفاة ولا red flag وقع فيه، false وإلا
- confidence: 0.0-1.0 (ارفع لو وقع red flag صريح، اخفض لو في شك)
- concerns: قائمة قصيرة بالمشاكل المحددة (بالعربي). إذكر صراحة لو
  وقع في أي red flag (مثلاً: "وقع في red flag #2 — تشبيه العبادة بالمغناطيس").
  فارغة لو passed.

ارجع JSON مطابق للـ schema بالظبط، مع review لكل آية بنفس ترتيب الإدخال.
"""


# ════════════════════════════════════════════════════════════════
# BatchTTSDirector — per-segment SSML directions in 1 call (Key 2)
# ════════════════════════════════════════════════════════════════

class BatchTTSDirector:
    """Generate SSML voice directions for an entire episode in 1 Gemini call.

    Uses Key 2 (dedicated to TTS in v22.6 architecture).

    OUTPUT SHAPE — drop-in compatible with legacy TTSDirector:
        {
            "intro_text":     {"directed_text": "...", "pace": "...", ...},
            "ayah_1.hook":    {"directed_text": "...", "pace": "...", ...},
            "ayah_1.story":   {...},
            "ayah_1.moral":   {...},
            ...
            "outro_text":     {...},
        }

    The orchestrator stores this under `episode_data["_tts_directions"]` and
    the synthesis stage looks up segments by id at TTS time.
    """

    # Segment kinds we direct, mirroring the legacy TTSDirector (per-scene)
    _PER_SCENE_SEGMENT_KINDS: tuple = ("hook_text", "story_text", "moral_text")
    _EPISODE_LEVEL_SEGMENTS: tuple = ("intro_text", "outro_text")

    def __init__(self, gemini_client: Any) -> None:
        if gemini_client is None:
            raise ValueError("BatchTTSDirector requires a Gemini client")
        self._client = gemini_client

    # ─── Public API ──────────────────────────────────────────────
    def direct_episode(
        self,
        episode_data: Dict[str, Any],
    ) -> Optional[BatchTTSOut]:
        """Generate per-segment SSML directions for the whole episode in 1 call.

        Args:
            episode_data: The episode JSON loaded from disk
                          (must have intro_text, outro_text, ayah_scenes).

        Returns:
            BatchTTSOut with directions list, or None on parse failure.
            Caller is responsible for falling back to legacy per-call
            TTSDirector on None.
        """
        segments = self._collect_segments(episode_data)
        if not segments:
            logger.warning(
                "⚠️ BatchTTSDirector: no segments to direct (empty episode_data)"
            )
            return None

        logger.info(
            f"🎙️ BatchTTSDirector: directing {len(segments)} segments "
            f"({len(episode_data.get('ayah_scenes', []))} ayahs) in 1 call"
        )
        prompt = self._build_prompt(segments)
        t0 = time.monotonic()

        result = _call_gemini_with_schema(
            self._client, prompt, BatchTTSOut,
            temperature=0.3, max_tokens=8192,
        )
        elapsed = time.monotonic() - t0

        if result is None:
            logger.error(
                f"❌ BatchTTSDirector failed in {elapsed:.1f}s — "
                f"caller should fall back to legacy per-segment path"
            )
            return None

        # Validate that we got directions for at least the segments we asked for
        # — Gemini sometimes drops a segment. Caller can still use partial output.
        returned_ids = {d.segment_id for d in result.directions}
        requested_ids = {sid for sid, _, _ in segments}
        missing = requested_ids - returned_ids
        if missing:
            logger.warning(
                f"⚠️ BatchTTSDirector: {len(missing)} segments missing from output: "
                f"{sorted(missing)[:5]}{'…' if len(missing) > 5 else ''}"
            )

        logger.info(
            f"✅ BatchTTSDirector: {len(result.directions)}/{len(segments)} "
            f"directions in {elapsed:.1f}s"
        )
        return result

    # ─── Helpers ─────────────────────────────────────────────────
    @classmethod
    def _collect_segments(
        cls,
        episode_data: Dict[str, Any],
    ) -> List[tuple]:
        """Walk episode JSON and produce (segment_id, text, segment_kind) tuples.

        Mirrors the legacy TTSDirector traversal so segment_ids match exactly.
        """
        segments: List[tuple] = []

        # Episode-level
        for field_name in cls._EPISODE_LEVEL_SEGMENTS:
            text = episode_data.get(field_name, "") or ""
            text = text.strip()
            if text:
                kind = field_name.replace("_text", "")
                segments.append((field_name, text, kind))

        # Per-scene
        for i, scene in enumerate(episode_data.get("ayah_scenes", []), start=1):
            scene_id_prefix = f"ayah_{i}"
            for kind_field in cls._PER_SCENE_SEGMENT_KINDS:
                text = (scene.get(kind_field, "") or "").strip()
                if text:
                    kind = kind_field.replace("_text", "")
                    segments.append((f"{scene_id_prefix}.{kind}", text, kind))

        return segments

    @staticmethod
    def _build_prompt(segments: List[tuple]) -> str:
        """Build a single batched prompt for ALL segments."""
        blocks = []
        for sid, text, kind in segments:
            # Truncate super-long segments defensively (shouldn't happen for
            # children's content) so the prompt stays under output budget.
            shown = text[:600]
            blocks.append(f"━━━ {sid} (kind: {kind}) ━━━\n{shown}")
        body = "\n\n".join(blocks)

        return f"""أنت مخرج صوتي خبير لقصص دينية للأطفال (٦-١٢ سنة).
مهمتك: لكل segment أدناه، أعط directed_text مع SSML breaks، و pace.

[القواعد الذهبية للأداء الصوتي]

1. **التوقفات الطبيعية**:
   - بعد فاصلة عادية: <break time="300ms"/>
   - قبل فكرة مهمة أو insight: <break time="500ms"/>
   - قبل moral / take-away: <break time="800ms"/>

2. **Pace حسب نوع الـ segment**:
   - hook    → "fast" أو "normal" (فيه طاقة، يلفت الانتباه)
   - story   → "normal" (سرد طبيعي)
   - explain → "normal" (شرح واضح)
   - moral   → "slow" (للتأمل)
   - intro   → "normal"
   - outro   → "slow"

3. **قواعد صارمة جداً**:
   ✋ ممنوع تغيّر الكلمات نفسها — أضف فقط <break/> tags
   ✋ ممنوع تختصر، ممنوع تعيد صياغة
   ✋ كل segment لازم يرجع بـ نفس الـ segment_id بالظبط

4. **النطق**:
   - لو في كلمة عربية معقدة (اسم سورة فيها ادغام، أو كلمة فصحى نادرة)،
     ضيفها في pronunciation_notes
   - الكلمات العادية ما تحتاجش notes

[الـ Segments المطلوب توجيهها]

{body}

═══ الـ Output ═══

ارجع JSON بـ schema المطلوب. لكل segment:
  - segment_id: نفس الـ ID اللي وصلك بالظبط
  - directed_text: نفس النص + <break/> tags في أماكن منطقية
  - pace: slow | normal | fast (حسب القاعدة فوق)
  - pace_reason: جملة قصيرة بالعربي
  - pronunciation_notes: list من الكلمات الصعبة (لو في)
"""

    @staticmethod
    def to_legacy_dict(result: BatchTTSOut) -> Dict[str, Dict[str, Any]]:
        """Convert BatchTTSOut → the orchestrator's `_tts_directions` shape.

        Returns:
            {segment_id: {directed_text, pace, pronunciation_notes}, ...}
        """
        out: Dict[str, Dict[str, Any]] = {}
        for d in result.directions:
            out[d.segment_id] = {
                "directed_text": d.directed_text,
                "pace": d.pace,
                "pronunciation_notes": list(d.pronunciation_notes),
            }
        return out


# ════════════════════════════════════════════════════════════════
# BatchVisualPromptEngine — visual prompts for ALL 7 (Key 3)
# ════════════════════════════════════════════════════════════════

class BatchVisualPromptEngine:
    """Generate visual prompts for all 7 ayahs in 1 call.

    Uses Key 3 (dedicated to visuals in v22.6 architecture).
    """

    def __init__(self, gemini_client: Any) -> None:
        self._client = gemini_client

    def generate_visuals(
        self,
        ayah_scripts: List[Dict[str, Any]],
    ) -> Optional[BatchVisualOut]:
        """Generate Leonardo prompts for all ayahs in 1 call."""
        prompt = self._build_prompt(ayah_scripts)
        logger.info(
            f"🎨 BatchVisualPromptEngine: prompting {len(ayah_scripts)} ayahs in 1 call"
        )
        t0 = time.monotonic()

        result = _call_gemini_with_schema(
            self._client, prompt, BatchVisualOut,
            temperature=0.6, max_tokens=4096,
        )

        elapsed = time.monotonic() - t0
        if result is None:
            logger.error(f"❌ BatchVisualPromptEngine failed in {elapsed:.1f}s")
            return None

        logger.info(
            f"✅ BatchVisualPromptEngine: {len(result.prompts)} prompts in {elapsed:.1f}s"
        )
        return result

    @staticmethod
    def _build_prompt(ayah_scripts: List[Dict[str, Any]]) -> str:
        blocks = []
        for s in ayah_scripts:
            num = s["number"]
            text = s.get("explain", "")[:300]
            story = s.get("story", "")[:200]
            emotion = s.get("emotion", "warm")
            blocks.append(
                f"━━━ Ayah {num} (emotion: {emotion}) ━━━\n"
                f"Explanation: {text}\n"
                f"Analogy: {story}"
            )
        body = "\n\n".join(blocks)

        return f"""You are a cinematic visual director for an Islamic
educational video for children (ages 6-12). For each ayah below, design
a single watercolor scene that conveys the meaning visually — without
ever depicting a face of the Prophet, sacred figures, or controversial
religious symbols.

═══ LOCKED VISUAL STYLE — applied uniformly across the whole channel ═══
- Hand-painted watercolor + ink lineart on cream paper texture
- NotebookLM-inspired multi-panel feel, not a flat illustration
- Soft natural lighting, gentle gradients, no harsh shadows
- Children's-book warmth, never photoreal, never dark or scary
- No human faces with detail (silhouettes or back-views are fine)
- No Arabic calligraphy in the image (text is added in post-production)

═══ Ayahs ═══
{body}

═══ Required output for each ayah ═══

For each ayah, fill ALL 14 fields with concrete, visualizable English text.
Empty strings are not acceptable — even brief detail is better than blank.

LAYER 1 — Scene primitives (the WHAT)
- subject: the main visualizable element
- action: what it is doing (use a verb)
- environment: where it sits
- time_of_day: golden hour / dawn / dusk / midday / night

LAYER 2 — Aesthetic (the FEEL)
- mood: single emotional phrase matching the ayah emotion above
- color_palette: 3-5 specific colors (e.g., "warm ochre, soft sage, cream")
- lighting_direction: where the light comes from, soft vs. directional
- atmospheric_elements: dust, mist, sparkles, particles (subtle, child-friendly)

LAYER 3 — Cinematic composition (the FRAME)
- camera_angle: wide / close-up / low-angle / overhead / dutch
- depth_of_field: shallow / medium / deep — describe the bokeh
- foreground / midground / background: 3 distinct depth planes
- focal_point: where the viewer's eye lands first

═══ Hard rules ═══
- Output language: English (Leonardo prompt language)
- One entry per ayah, in numeric order
- The subject MUST be a metaphor or symbol, NEVER a depiction of God,
  the Prophet, angels with detailed faces, or scenes of judgment day
  literalized as physical events
- For "بسم الله" themed ayahs: show beginnings (a seed, a sunrise, a
  page being turned), NEVER glowing magical text
- For worship-themed ayahs: show free-willed actions (hands lifted in
  prayer from behind, a child running joyfully toward light), NEVER
  mechanical attraction (magnets, pulled-along arrows)

Return JSON with `prompts` array — one entry per ayah, all 14 fields filled.
"""

    @staticmethod
    def to_legacy_dicts(result: BatchVisualOut) -> List[Dict[str, Any]]:
        """Convert BatchVisualOut → list of `_deep_visuals` dicts.

        Maps 1:1 to the shape persisted by the legacy
        DeepVisualPromptGenerator path so downstream code (LeonardoEngine,
        VisualPromptEngineer) is untouched.

        Returns:
            List of dicts in ayah order, each with the 14 fields plus
            layers_completed=3 and is_usable=True (Pydantic validation
            already guaranteed all fields are non-empty bounded strings).
        """
        out: List[Dict[str, Any]] = []
        # Sort by ayah_number defensively — Gemini usually preserves order
        # but a strict-schema response can occasionally swap entries.
        for p in sorted(result.prompts, key=lambda x: x.ayah_number):
            out.append({
                "subject": p.subject,
                "action": p.action,
                "environment": p.environment,
                "time_of_day": p.time_of_day,
                "mood": p.mood,
                "color_palette": p.color_palette,
                "lighting_direction": p.lighting_direction,
                "atmospheric_elements": p.atmospheric_elements,
                "camera_angle": p.camera_angle,
                "depth_of_field": p.depth_of_field,
                "foreground": p.foreground,
                "midground": p.midground,
                "background": p.background,
                "focal_point": p.focal_point,
                "layers_completed": 3,
                "is_usable": bool(p.subject),
            })
        return out
