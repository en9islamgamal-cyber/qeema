"""
engines/tafsir_validator.py — VALUE / QEEMA v22.5 FINAL
=========================================================================

Religious accuracy validator. Prevents LLM hallucination in Quranic
explanations. THIS IS THE MOST IMPORTANT FILE IN THE PROJECT.

[Why this exists]
The script-generation LLM produces `explain_text` and `analogy_text` for
each ayah, and there is NO grounding in authentic tafsir sources without
this validator. A single wrong interpretation published to the channel =
reputation damage that may not be recoverable.

[v22.5 Architecture — Gemini-only]
Two layers, no fallbacks:

  Layer 1: Authentic tafsir fetch (HTTP — quran.com API)
           Pulls As-Saadi (#169) + Al-Muyassar (#16). HTTP only,
           no Gemini quota cost.

  Layer 2: GeminiReviewer — validates the explanation against the authentic
           tafsir. Uses gemini-2.5-flash with response_mime_type=application/json
           for structured output, smart-quote normalization for Arabic JSON,
           and 4-attempt retry with 15s/30s/60s backoffs to handle transient
           503/429 errors on Google's load balancer.

[Why no Claude]
The user has confirmed Anthropic credit will NOT be funded. The previous
chain was:  Claude → Gemini → Heuristic. With Claude unavailable and
Heuristic structurally too weak (keyword overlap can score legitimate
content at 0.40), the chain's middle link became the only link. So we
made it the ONLY link, with proper retries.

[Why no Heuristic fallback]
Heuristic was a confidence-0.40 stamp that effectively acted as
"approve everything that fails the network check". For a religious
gate, this is worse than no gate at all because it gives false
confidence. Better to fail loudly so the daily retry can pick it up
when Gemini quota refreshes.

[Day-3 quota safety]
Tafsir validation runs in Phase 1 (Day 1) on key #1 alongside script
generation. With ~14 Gemini calls (7 script + 7 tafsir) at 4 RPM,
Phase 1 takes ~3 minutes — well under the daily quota for one Google
account.

[Sources used]
- quran.com API: https://api.quran.com/api/v4/tafsirs
  - Tafsir ID 169: As-Saadi (تيسير الكريم الرحمن — مناسب للتبسيط)
  - Tafsir ID 16: Al-Muyassar (التفسير الميسر — للتحقق العام)
  - Tafsir ID 168: Al-Baghawy (تفسير البغوي — kept as constant for future)
- Google Gemini 2.5 Flash: religious cross-validation
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Tafsir source IDs from quran.com API
# ════════════════════════════════════════════════════════════════
TAFSIR_SAADI = 169       # تفسير السعدي — clear language, kid-appropriate
TAFSIR_MUYASSAR = 16     # التفسير الميسر — concise, official Saudi-approved
TAFSIR_BAGHAWY = 168     # تفسير البغوي — classical reference (unused in v22.5)


@dataclass(frozen=True)
class TafsirValidationResult:
    """Result of religious validation for a single ayah explanation."""
    passed: bool
    confidence: float  # 0.0 (definitely wrong) to 1.0 (perfectly aligned)
    concerns: List[str] = field(default_factory=list)
    authentic_excerpt: str = ""  # First 300 chars of authentic tafsir
    reviewer: str = "none"       # "gemini-2.5-flash" | "none"


# ════════════════════════════════════════════════════════════════
# AuthenticTafsirFetcher — Layer 1
# ════════════════════════════════════════════════════════════════
class AuthenticTafsirFetcher:
    """Fetches authentic tafsir from quran.com API with in-memory cache.

    Note: a separate persistent disk cache exists at core/tafsir_cache.py
    but is not yet wired here. The in-memory cache below survives only the
    lifetime of one process / one episode.
    """

    BASE_URL = "https://api.quran.com/api/v4"

    def __init__(self) -> None:
        self._cache: Dict[Tuple[int, int, int], str] = {}  # (surah, ayah, tafsir_id) → text

    def fetch(
        self,
        surah: int,
        ayah: int,
        tafsir_id: int = TAFSIR_SAADI,
    ) -> Optional[str]:
        """Fetch tafsir for a specific ayah.

        Returns plain text (HTML stripped) or None on failure.
        """
        cache_key = (surah, ayah, tafsir_id)
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"{self.BASE_URL}/tafsirs/{tafsir_id}/by_ayah/{surah}:{ayah}"
        try:
            response = requests.get(url, timeout=15)
            if response.status_code != 200:
                logger.warning(
                    f"⚠️ Tafsir fetch HTTP {response.status_code} "
                    f"for {surah}:{ayah} (tafsir={tafsir_id})"
                )
                return None

            data = response.json()
            tafsir = data.get("tafsir", {})
            text = tafsir.get("text", "")

            # Strip HTML tags (tafsir API returns formatted HTML)
            import re
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()

            if text:
                self._cache[cache_key] = text
                return text
            return None
        except requests.RequestException as e:
            logger.warning(f"⚠️ Tafsir fetch network error: {e}")
            return None

    def fetch_combined(self, surah: int, ayah: int) -> Optional[str]:
        """Fetch BOTH As-Saadi and Al-Muyassar.

        Returns concatenated text for cross-validation, or None if neither
        source succeeded.
        """
        saadi = self.fetch(surah, ayah, TAFSIR_SAADI)
        muyassar = self.fetch(surah, ayah, TAFSIR_MUYASSAR)

        parts = []
        if saadi:
            parts.append(f"[تفسير السعدي]\n{saadi}")
        if muyassar:
            parts.append(f"\n[التفسير الميسر]\n{muyassar}")

        if not parts:
            return None
        return "\n".join(parts)


# ════════════════════════════════════════════════════════════════
# GeminiReviewer — Layer 2 (the ONLY active reviewer in v22.5)
# ════════════════════════════════════════════════════════════════
class GeminiReviewer:
    """Religious validator using Gemini 2.5 Flash with multi-key rotation.

    [Multi-key rotation — v22.5]
    Accepts one OR multiple Gemini API keys (typically GEMINI_API_KEY,
    GEMINI_API_KEY_2, GEMINI_API_KEY_3 from 3 separate Google projects).
    On 429 RESOURCE_EXHAUSTED (daily quota), rotates to the next key.
    Each key has its own 20/day free-tier limit — 3 keys = 60/day total.

    [Conservative bias]
    The prompt instructs Gemini to default to "needs review" when uncertain,
    rather than auto-approving. Better to over-flag than under-flag for
    children's religious content.

    [Retry policy]
    Per-key: 4 attempts with 15s/30s/60s backoffs (transient 503s).
    Across keys: rotate immediately on 429 daily quota (no backoff).

    [JSON parsing]
    Uses response_mime_type=application/json. Defensively normalizes smart
    quotes, strips markdown fences, handles truncated responses gracefully.
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        gemini_api_keys: Optional[List[str]] = None,
    ) -> None:
        """Build reviewer with one OR multiple keys.

        Args:
            gemini_api_key: Single key (legacy compat — prefer gemini_api_keys)
            gemini_api_keys: List of keys for rotation across daily-quota-isolated
                             projects. If both args given, key is appended first.
        """
        # Normalize to a list of unique non-empty keys
        keys: List[str] = []
        if gemini_api_key:
            keys.append(gemini_api_key)
        if gemini_api_keys:
            for k in gemini_api_keys:
                if k and k not in keys:
                    keys.append(k)

        self._keys: List[str] = keys
        self._current_key_idx: int = 0
        self._clients: Dict[str, Any] = {}
        self._rate_limiters: Dict[str, Any] = {}
        self._key_exhausted: Dict[str, bool] = {}
        self._available = bool(keys)

        if not keys:
            logger.warning(
                "⚠️ GeminiReviewer constructed with no keys — disabled"
            )
            return

        # Pre-build clients + rate limiters for each key
        try:
            from google import genai
            from core.gemini_rate_limiter import limiter_for_key
            for k in keys:
                self._clients[k] = genai.Client(api_key=k)
                self._rate_limiters[k] = limiter_for_key(
                    k, label_hint="tafsir-reviewer",
                )
                self._key_exhausted[k] = False
            logger.info(
                f"✅ Gemini religious reviewer ready "
                f"(model=gemini-2.5-flash, keys={len(keys)})"
            )
        except ImportError:
            logger.warning(
                "⚠️ google-genai package not installed — Gemini reviewer disabled"
            )
            self._available = False
            return
        except Exception as e:
            logger.warning(f"⚠️ Gemini reviewer init failed: {e}")
            self._available = False
            return

    @property
    def _current_key(self) -> Optional[str]:
        """Currently active key (None if all exhausted)."""
        if not self._keys or self._current_key_idx >= len(self._keys):
            return None
        return self._keys[self._current_key_idx]

    @property
    def _client(self) -> Any:
        """Active client for the current key (legacy property)."""
        k = self._current_key
        return self._clients.get(k) if k else None

    @property
    def _key(self) -> str:
        """Current active key (legacy property — back-compat)."""
        return self._current_key or ""

    @property
    def _rate_limiter(self) -> Any:
        """Rate limiter for current key (legacy property)."""
        k = self._current_key
        return self._rate_limiters.get(k) if k else None

    def _rotate_to_next_key(self) -> bool:
        """Mark current key exhausted, advance to next available.

        Returns True if a fresh key is now active, False if all exhausted.
        """
        cur = self._current_key
        if cur:
            self._key_exhausted[cur] = True
            logger.warning(
                f"⚠️ GeminiReviewer: key #{self._current_key_idx + 1} "
                f"daily-quota exhausted, rotating"
            )

        # Advance to next non-exhausted key
        for idx in range(self._current_key_idx + 1, len(self._keys)):
            if not self._key_exhausted.get(self._keys[idx]):
                self._current_key_idx = idx
                logger.info(
                    f"✅ GeminiReviewer: switched to key #{idx + 1}/{len(self._keys)}"
                )
                return True

        # All keys exhausted
        self._current_key_idx = len(self._keys)
        logger.error(
            f"❌ GeminiReviewer: ALL {len(self._keys)} keys exhausted today"
        )
        return False

    def review(
        self,
        ayah_text: str,
        surah_name: str,
        ayah_number: int,
        llm_explanation: str,
        llm_analogy: str,
        authentic_tafsir: str,
    ) -> TafsirValidationResult:
        """Review one ayah explanation against the authentic tafsir.

        Returns a TafsirValidationResult. On transient failure (503/429),
        retries up to 4 times with exponential backoffs of 15/30/60s.
        On permanent failure (auth, invalid request), fails immediately.
        """
        if not self._available:
            return TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=["Gemini reviewer unavailable"],
                reviewer="none",
            )

        prompt = self._build_review_prompt(
            ayah_text=ayah_text,
            surah_name=surah_name,
            ayah_number=ayah_number,
            llm_explanation=llm_explanation,
            llm_analogy=llm_analogy,
            authentic_tafsir=authentic_tafsir,
        )

        max_attempts = 4
        last_error: Optional[Exception] = None
        backoffs = [15, 30, 60]  # seconds before retries 2, 3, 4

        for attempt in range(1, max_attempts + 1):
            try:
                return self._do_review_call(prompt, authentic_tafsir, ayah_number)
            except Exception as e:
                err_msg = str(e).lower()
                last_error = e

                # v22.5: distinguish daily quota (RESOURCE_EXHAUSTED) from
                # transient rate limits. Daily quota = key is dead for the day,
                # rotate immediately to next key. Transient = backoff and retry.
                is_daily_quota = (
                    "resource_exhausted" in err_msg
                    or "exceeded your current quota" in err_msg
                    or "generaterequestsperdayperprojectpermodel" in err_msg
                )
                is_transient = is_daily_quota or (
                    "503" in err_msg
                    or "unavailable" in err_msg
                    or "429" in err_msg
                    or "rate limit" in err_msg
                    or "timeout" in err_msg
                )

                # Daily quota → try next key without backoff
                if is_daily_quota and len(self._keys) > 1:
                    if self._rotate_to_next_key():
                        logger.info(
                            f"🔄 Daily quota hit — rotated to fresh key, "
                            f"retrying ayah {ayah_number} immediately"
                        )
                        # Don't count this as a retry attempt — fresh key gets full budget
                        continue
                    else:
                        # All keys exhausted today
                        logger.error(
                            f"❌ All {len(self._keys)} Gemini keys exhausted today"
                        )
                        return TafsirValidationResult(
                            passed=False, confidence=0.0,
                            concerns=[f"All Gemini keys daily-exhausted: {e}"],
                            reviewer="gemini-2.5-flash",
                        )

                if not is_transient or attempt == max_attempts:
                    logger.error(f"❌ Gemini review error: {e}")
                    return TafsirValidationResult(
                        passed=False, confidence=0.0,
                        concerns=[f"Reviewer error: {e}"],
                        reviewer="gemini-2.5-flash",
                    )
                wait = backoffs[attempt - 1]
                logger.warning(
                    f"⚠️ Gemini review transient failure "
                    f"(attempt {attempt}/{max_attempts}): retrying in {wait}s"
                )
                time.sleep(wait)

        # Defensive: should not reach here
        return TafsirValidationResult(
            passed=False, confidence=0.0,
            concerns=[f"Reviewer error after retries: {last_error}"],
            reviewer="gemini-2.5-flash",
        )

    def _do_review_call(
        self,
        prompt: str,
        authentic_tafsir: str,
        ayah_number: int,
    ) -> TafsirValidationResult:
        """Single Gemini review call. Raises on transient errors so the
        retry wrapper in review() can catch them.

        v22.5.2: bumped max_output_tokens to 2048 (was 800 — too tight for
        Arabic prompts which use ~3 chars per token vs 1 for English).
        Also added robust field-by-field JSON salvage for the case where
        Gemini returns malformed JSON with broken Arabic escapes.
        """
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()

        from google.genai import types
        response = self._client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=2048,  # Arabic ~3 chars/token, gives headroom
            ),
        )
        text = response.text.strip() if response.text else ""

        # Strip markdown fences if present
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Normalize smart quotes
        text = (text
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2018", "'").replace("\u2019", "'")
            .replace("\u00ab", '"').replace("\u00bb", '"')
        )

        # ─── Robust parsing chain ────────────────────────────────
        data = self._robust_parse_json(text, ayah_number)

        return TafsirValidationResult(
            passed=bool(data.get("passed", False)),
            confidence=float(data.get("confidence", 0.0)),
            concerns=list(data.get("concerns", [])),
            authentic_excerpt=authentic_tafsir[:300],
            reviewer="gemini-2.5-flash",
        )

    @staticmethod
    def _robust_parse_json(text: str, ayah_number: int) -> Dict[str, Any]:
        """Parse Gemini JSON output with multiple fallback strategies.

        Real-world failures observed in production logs:
          • "Unterminated string starting at: line 4 column 3"
          • "Expecting property name enclosed in double quotes: line 3 col 21"

        These happen when Gemini outputs malformed JSON with Arabic content
        (broken escapes, mid-string newlines, trailing commas, etc).

        Strategy:
          1. json.loads — works for clean output
          2. Find {...} substring and parse — handles preamble/markdown
          3. Field-level regex extraction — works on truncated output
          4. Last resort: return safe default that triggers a retry
        """
        import re as _re

        # Layer 1: clean parse
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Layer 2: extract JSON object substring
        match = _re.search(r'\{.*\}', text, flags=_re.DOTALL)
        if match:
            obj_text = match.group(0)
            try:
                return json.loads(obj_text)
            except (json.JSONDecodeError, ValueError):
                # Try fixing common issues: trailing commas, unquoted keys
                fixed = _re.sub(r',\s*([\]}])', r'\1', obj_text)  # trailing commas
                # Quote unquoted keys: `confidence:` → `"confidence":`
                fixed = _re.sub(
                    r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
                    r'\1"\2":',
                    fixed,
                )
                try:
                    return json.loads(fixed)
                except (json.JSONDecodeError, ValueError):
                    pass

        # Layer 3: field-by-field regex extraction (handles truncation)
        passed_match = _re.search(
            r'"passed"\s*:\s*(true|false)', text, _re.IGNORECASE,
        )
        conf_match = _re.search(
            r'"confidence"\s*:\s*([0-9.]+)', text,
        )
        if passed_match and conf_match:
            logger.warning(
                f"⚠️ Salvaged malformed JSON for ayah {ayah_number} via regex "
                f"(passed={passed_match.group(1)}, conf={conf_match.group(1)})"
            )
            # Try to extract concerns array (best effort)
            concerns_match = _re.search(
                r'"concerns"\s*:\s*\[([^\]]*)\]', text, _re.DOTALL,
            )
            concerns: List[str] = []
            if concerns_match:
                # Pull quoted strings from inside the array
                concerns = _re.findall(r'"([^"]*)"', concerns_match.group(1))
            return {
                "passed": passed_match.group(1).lower() == "true",
                "confidence": float(conf_match.group(1)),
                "concerns": concerns or ["[malformed JSON — fields salvaged via regex]"],
            }

        # Layer 4: complete failure — log raw text for debugging and fail closed
        logger.error(
            f"❌ Could not parse Gemini response for ayah {ayah_number}. "
            f"Raw (first 500 chars): {text[:500]!r}"
        )
        # Return safe default that indicates failure (downstream gates this)
        return {
            "passed": False,
            "confidence": 0.0,
            "concerns": [f"JSON parse completely failed (raw len={len(text)})"],
        }

    def _build_review_prompt(
        self,
        ayah_text: str,
        surah_name: str,
        ayah_number: int,
        llm_explanation: str,
        llm_analogy: str,
        authentic_tafsir: str,
    ) -> str:
        """Build the validation prompt — strict, conservative, schema-locked."""
        return f"""أنت عالم دين أزهري متخصص في التفسير. مهمتك مراجعة شرح موجَّه للأطفال (٦-١٢ سنة) للآية المذكورة.

[الآية]
سورة {surah_name}، الآية {ayah_number}:
"{ayah_text}"

[التفسير الأصيل المعتمد (الـ ground truth)]
{authentic_tafsir}

[الشرح المقترح للأطفال]
الشرح: {llm_explanation}
المثال/التشبيه: {llm_analogy}

[مهمتك]
قارن الشرح المقترح بالتفسير الأصيل. حدد:

1. **passed (boolean):**
   - true: الشرح صحيح ومتوافق مع التفسير الأصيل
   - false: فيه خطأ أو انحراف عن المعنى الصحيح

2. **confidence (0.0-1.0):** ثقتك في حكمك
   - 0.9+: الحكم قاطع وواضح
   - 0.7-0.9: ثقة عالية مع بعض الفروقات الطفيفة
   - 0.5-0.7: غير متأكد — يحتاج مراجعة بشرية
   - <0.5: أحكام مبهمة، اطلب مراجعة

3. **concerns (list of strings):** المشاكل الفعلية المحددة
   - أي خطأ عقدي
   - أي تشبيه يقلل من قيمة الآية
   - أي حذف لمعنى أساسي

[قواعد صارمة]
- ✋ لو في أي شك، اخفض الـ confidence
- ✋ لا تقبل شرحاً يضيف معاني مش في التفسير الأصيل
- ✋ التشبيهات ممكنة لو ما خالفتش المعنى — لكن لازم تكون واضحة
- ✋ مفيش مجال للتساهل في الشرح للأطفال — الخطأ يبقى خطأ

[الإجابة]
ارجع JSON فقط (بدون markdown، بدون نص قبله أو بعده):
{{
  "passed": true/false,
  "confidence": 0.0-1.0,
  "concerns": ["قلق 1", "قلق 2"]
}}"""

    # ════════════════════════════════════════════════════════════════
    # BATCH review (v22.5.1) — review all ayahs in one Gemini call
    # ════════════════════════════════════════════════════════════════
    def review_batch(
        self,
        ayahs_payload: List[Dict[str, Any]],
        surah_name: str,
    ) -> List[TafsirValidationResult]:
        """Review ALL ayahs of an episode in ONE Gemini call.

        [Why this exists]
        Free-tier Gemini = 20 requests/day per project. Reviewing 7 ayahs
        with 7 separate calls eats 35% of the daily quota for one episode.
        Batching all 7 into a single call costs 1 quota token regardless of
        ayah count — leaving 19 calls/day available for script + visuals.

        Args:
            ayahs_payload: list of dicts, each with keys:
                - ayah_number (int)
                - ayah_text (str)
                - llm_explanation (str)
                - llm_analogy (str)
                - authentic_tafsir (str)
            surah_name: e.g. "الفاتحة"

        Returns:
            One TafsirValidationResult per input ayah, in input order. If the
            batch fails partially, missing ayahs get a result with
            passed=False/confidence=0 so the caller's quality gate catches it.
        """
        if not self._available:
            return [
                TafsirValidationResult(
                    passed=False, confidence=0.0,
                    concerns=["Gemini reviewer unavailable"],
                    reviewer="none",
                )
                for _ in ayahs_payload
            ]

        if not ayahs_payload:
            return []

        # Build batch prompt
        prompt = self._build_batch_prompt(ayahs_payload, surah_name)

        # Retry + key rotation logic — same as single review() but batch-aware
        max_attempts = 4
        backoffs = [15, 30, 60]
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                return self._do_batch_review_call(prompt, ayahs_payload)
            except Exception as e:
                err_msg = str(e).lower()
                last_error = e

                is_daily_quota = (
                    "resource_exhausted" in err_msg
                    or "exceeded your current quota" in err_msg
                    or "generaterequestsperdayperprojectpermodel" in err_msg
                )
                is_transient = is_daily_quota or (
                    "503" in err_msg
                    or "unavailable" in err_msg
                    or "429" in err_msg
                    or "rate limit" in err_msg
                    or "timeout" in err_msg
                )

                # Daily quota → rotate keys without backoff
                if is_daily_quota and len(self._keys) > 1:
                    if self._rotate_to_next_key():
                        logger.info(
                            f"🔄 Batch review: daily quota hit, rotated to "
                            f"fresh key, retrying immediately"
                        )
                        continue
                    else:
                        logger.error(
                            f"❌ Batch review: all {len(self._keys)} keys exhausted"
                        )
                        return [
                            TafsirValidationResult(
                                passed=False, confidence=0.0,
                                concerns=[f"All Gemini keys exhausted: {e}"],
                                reviewer="gemini-2.5-flash",
                            )
                            for _ in ayahs_payload
                        ]

                if not is_transient or attempt == max_attempts:
                    logger.error(f"❌ Batch review error: {e}")
                    return [
                        TafsirValidationResult(
                            passed=False, confidence=0.0,
                            concerns=[f"Batch reviewer error: {e}"],
                            reviewer="gemini-2.5-flash",
                        )
                        for _ in ayahs_payload
                    ]
                wait = backoffs[attempt - 1]
                logger.warning(
                    f"⚠️ Batch review transient failure "
                    f"(attempt {attempt}/{max_attempts}): retrying in {wait}s"
                )
                time.sleep(wait)

        # Defensive
        return [
            TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=[f"Batch reviewer failed: {last_error}"],
                reviewer="gemini-2.5-flash",
            )
            for _ in ayahs_payload
        ]

    def _build_batch_prompt(
        self,
        ayahs_payload: List[Dict[str, Any]],
        surah_name: str,
    ) -> str:
        """Build a single prompt for reviewing all ayahs at once."""
        ayah_blocks = []
        for i, p in enumerate(ayahs_payload, start=1):
            block = f"""━━━━━━━━━━━ الآية #{i} ━━━━━━━━━━━
سورة {surah_name}، الآية {p['ayah_number']}:
"{p['ayah_text']}"

[التفسير الأصيل المعتمد]
{p['authentic_tafsir']}

[الشرح المقترح للأطفال]
الشرح: {p['llm_explanation']}
المثال/التشبيه: {p['llm_analogy']}
"""
            ayah_blocks.append(block)

        ayahs_text = "\n".join(ayah_blocks)
        n = len(ayahs_payload)

        return f"""أنت عالم دين أزهري متخصص في التفسير. مهمتك مراجعة شرح موجَّه للأطفال (٦-١٢ سنة) لـ {n} آيات من سورة {surah_name}.

{ayahs_text}

━━━━━━━━━━━ نهاية الآيات ━━━━━━━━━━━

[مهمتك]
راجع كل آية على حدة ضد التفسير الأصيل. لكل آية حدد:

1. **passed (boolean):**
   - true: الشرح صحيح ومتوافق مع التفسير الأصيل
   - false: فيه خطأ أو انحراف عن المعنى

2. **confidence (0.0-1.0):** ثقتك في حكمك على هذه الآية تحديداً

3. **concerns (list of strings):** المشاكل الفعلية المحددة لو وُجدت

[قواعد صارمة]
- ✋ راجع كل آية بشكل مستقل (لا تتأثر بحكمك على الآيات الأخرى)
- ✋ لو في أي شك، اخفض الـ confidence
- ✋ لا تقبل شرحاً يضيف معاني مش في التفسير الأصيل
- ✋ التشبيهات ممكنة لو ما خالفتش المعنى
- ✋ مفيش مجال للتساهل — الخطأ يبقى خطأ

[الإجابة]
ارجع JSON فقط (بدون markdown، بدون نص قبله أو بعده):
{{
  "reviews": [
    {{"ayah_number": <number>, "passed": true/false, "confidence": 0.0-1.0, "concerns": ["..."]}},
    ... (واحد لكل آية بنفس الترتيب)
  ]
}}

⚠️ مهم: ارجع بالظبط {n} reviews بنفس ترتيب الآيات في المدخلات."""

    def _do_batch_review_call(
        self,
        prompt: str,
        ayahs_payload: List[Dict[str, Any]],
    ) -> List[TafsirValidationResult]:
        """Make ONE Gemini call and parse the array response.

        Raises on transient errors (caught by review_batch retry loop).
        Returns one result per input ayah on success.
        """
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()

        # Defensive: client may be None if all keys exhausted
        if self._client is None:
            raise RuntimeError("No active Gemini client (all keys exhausted)")

        from google.genai import types as genai_types
        # Output: ~150 tokens × N ayahs. For 7 ayahs ≈ 1050 tokens. Cap at 4096
        # to give some headroom but stay well under the 8192 model limit.
        config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            max_output_tokens=4096,
        )

        response = self._client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config,
        )

        text = response.text or ""
        # Strip markdown fences if Gemini added them
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = (text
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2018", "'").replace("\u2019", "'")
            .replace("\u00ab", '"').replace("\u00bb", '"')
        )

        # Parse the JSON array — try clean parse, then progressive salvage
        data: Dict[str, Any] = {}
        try:
            data = json.loads(text.strip())
        except (json.JSONDecodeError, ValueError):
            # Salvage 1: find { ... }
            import re as _re
            match = _re.search(r'\{.*\}', text, flags=_re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except (json.JSONDecodeError, ValueError):
                    # Salvage 2: trailing-comma fix
                    fixed = _re.sub(r',\s*([\]}])', r'\1', match.group(0))
                    try:
                        data = json.loads(fixed)
                    except (json.JSONDecodeError, ValueError):
                        pass

        reviews = data.get("reviews", []) if data else []
        if not isinstance(reviews, list):
            reviews = []

        # If batch parse failed completely, fall back to per-ayah field extraction
        if not reviews:
            logger.warning(
                f"⚠️ Batch JSON unparseable, salvaging individual ayah fields. "
                f"Raw (first 300 chars): {text[:300]!r}"
            )
            import re as _re
            # Find every { passed: ..., confidence: ..., ayah_number: ... } block
            # Pattern allows them in any order (Gemini doesn't always output consistently)
            block_pattern = _re.compile(
                r'\{[^{}]*?"ayah_number"\s*:\s*(\d+)[^{}]*?\}',
                flags=_re.DOTALL,
            )
            for block_match in block_pattern.finditer(text):
                block = block_match.group(0)
                ayah_match = block_match.group(1)
                passed_m = _re.search(r'"passed"\s*:\s*(true|false)', block, _re.I)
                conf_m = _re.search(r'"confidence"\s*:\s*([0-9.]+)', block)
                if passed_m and conf_m:
                    reviews.append({
                        "ayah_number": int(ayah_match),
                        "passed": passed_m.group(1).lower() == "true",
                        "confidence": float(conf_m.group(1)),
                        "concerns": ["[salvaged from malformed batch JSON]"],
                    })

        # Build result list — one per input ayah, in input order
        results: List[TafsirValidationResult] = []
        for input_ayah in ayahs_payload:
            ayah_num = input_ayah['ayah_number']
            authentic = input_ayah.get('authentic_tafsir', '')
            # Find matching review by ayah_number
            matching = next(
                (r for r in reviews
                 if r.get('ayah_number') == ayah_num),
                None,
            )
            if matching is None:
                logger.warning(
                    f"⚠️ Batch review missing ayah {ayah_num} — flagging as failed"
                )
                results.append(TafsirValidationResult(
                    passed=False, confidence=0.0,
                    concerns=[f"Missing in batch review response"],
                    authentic_excerpt=authentic[:300],
                    reviewer="gemini-2.5-flash-batch",
                ))
            else:
                results.append(TafsirValidationResult(
                    passed=bool(matching.get("passed", False)),
                    confidence=float(matching.get("confidence", 0.0)),
                    concerns=list(matching.get("concerns", [])),
                    authentic_excerpt=authentic[:300],
                    reviewer="gemini-2.5-flash-batch",
                ))

        logger.info(
            f"✅ Batch review: {sum(r.passed for r in results)}/{len(results)} ayahs passed "
            f"(1 Gemini call instead of {len(results)})"
        )
        return results


# ════════════════════════════════════════════════════════════════
# TafsirValidator — main coordinator
# ════════════════════════════════════════════════════════════════
class TafsirValidator:
    """Main validator. Coordinates fetcher + Gemini reviewer.

    Usage:
        validator = TafsirValidator(gemini_review_key=key1)
        all_passed, concerns = validator.validate_episode(
            episode_data, surah=1, surah_name="الفاتحة",
        )
        if not all_passed:
            raise QualityGateError("Religious validation failed", critiques=concerns)

    [v22.5 Architecture]
    Single-layer Gemini-only validation. No Claude, no heuristic fallback.
    On Gemini failure (network, quota), validation FAILS LOUDLY so the
    next-day retry catches it with refreshed quota.
    """

    def __init__(
        self,
        gemini_review_key: Optional[str] = None,
        gemini_review_keys: Optional[List[str]] = None,
        confidence_threshold: float = 0.65,
        cache_path: Optional[Path] = None,
    ) -> None:
        """Build a v22.5 validator.

        Args:
            gemini_review_key: REQUIRED if gemini_review_keys not given. The
                              Gemini API key for review calls. Legacy single-key
                              path (back-compat).
            gemini_review_keys: NEW v22.5 — list of multiple keys for daily-quota
                              rotation. When the active key hits 429
                              RESOURCE_EXHAUSTED, the reviewer rotates to the
                              next available key. Each key counts as a separate
                              free-tier 20/day quota (use 3 separate Google
                              projects). If both `key` and `keys` given, they
                              are merged with `key` first.
            confidence_threshold: Minimum confidence to mark validation as
                                 passed (default: 0.65).
            cache_path: Optional path to a persistent JSON tafsir cache.
                        When provided, AuthenticTafsirFetcher is wrapped in a
                        CachedTafsirFetcher so quran.com is only hit once per
                        (surah, ayah, tafsir_id) across all runs.
        """
        # Build the tafsir fetcher chain: optionally wrap with disk cache
        upstream_fetcher: Any = AuthenticTafsirFetcher()
        if cache_path is not None:
            try:
                from core.tafsir_cache import TafsirCache, CachedTafsirFetcher
                self._fetcher = CachedTafsirFetcher(
                    fetcher=upstream_fetcher,
                    cache=TafsirCache(cache_path),
                )
                logger.info(
                    f"📚 TafsirValidator: persistent cache wired at {cache_path}"
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ Tafsir disk cache init failed ({e}) — using "
                    f"in-memory cache only"
                )
                self._fetcher = upstream_fetcher
        else:
            self._fetcher = upstream_fetcher

        self._gemini_reviewer: Optional[GeminiReviewer] = None
        self._confidence_threshold = confidence_threshold

        # Has at least one key?
        has_key = bool(gemini_review_key) or bool(gemini_review_keys)
        if has_key:
            self._gemini_reviewer = GeminiReviewer(
                gemini_api_key=gemini_review_key,
                gemini_api_keys=gemini_review_keys,
            )
            n_keys = len(self._gemini_reviewer._keys)
            logger.info(
                f"✅ TafsirValidator wired (Gemini-only, {n_keys} key(s) — v22.5)"
            )
        else:
            logger.error(
                "❌ TafsirValidator: NO Gemini key — validation will FAIL "
                "every episode. Set GEMINI_API_KEY (or whichever key the "
                "config exposes via tafsir_review_key)."
            )

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────
    def validate_explanation(
        self,
        ayah_text: str,
        surah: int,
        surah_name: str,
        ayah_number: int,
        llm_explanation: str,
        llm_analogy: str = "",
    ) -> TafsirValidationResult:
        """Validate one ayah explanation against authentic tafsir.

        Returns TafsirValidationResult — orchestrator should treat passed=False
        as a hard failure for the episode.
        """
        # Layer 1: Fetch authentic tafsir (HTTP — no Gemini quota cost)
        authentic = self._fetcher.fetch_combined(surah, ayah_number)
        if not authentic:
            logger.warning(
                f"⚠️ No authentic tafsir for {surah_name} {ayah_number} — "
                f"cannot validate (returning soft-pass with low confidence)"
            )
            return TafsirValidationResult(
                passed=True,  # Don't block if source unavailable (rare)
                confidence=0.3,
                concerns=["Authentic tafsir unavailable — manual review recommended"],
                reviewer="none",
            )

        # Layer 2: Gemini review
        if not self._gemini_reviewer:
            logger.error(
                f"❌ TafsirValidator: Gemini unavailable for {surah_name} "
                f"ayah {ayah_number} — returning failure"
            )
            return TafsirValidationResult(
                passed=False,
                confidence=0.0,
                concerns=[
                    "Gemini reviewer not configured. "
                    "Validation requires a Gemini API key."
                ],
                authentic_excerpt=authentic[:300],
                reviewer="none",
            )

        result = self._gemini_reviewer.review(
            ayah_text=ayah_text,
            surah_name=surah_name,
            ayah_number=ayah_number,
            llm_explanation=llm_explanation,
            llm_analogy=llm_analogy or "",
            authentic_tafsir=authentic,
        )

        # Distinguish infra errors from genuine religious rejection.
        # On infra errors, surface them clearly so the day-N+1 retry can pick up.
        concern_text = " ".join(result.concerns).lower()
        is_infra_error = (
            "reviewer error" in concern_text
            or "json parse" in concern_text
            or "unavailable" in concern_text
            or "timeout" in concern_text
            or "503" in concern_text
            or "429" in concern_text
            or "rate limit" in concern_text
            or "quota" in concern_text
        )
        if is_infra_error:
            logger.error(
                f"❌ Gemini infra error on {surah_name} ayah {ayah_number}: "
                f"{result.concerns[0] if result.concerns else 'unknown'}"
            )
            return TafsirValidationResult(
                passed=False,
                confidence=0.0,
                concerns=result.concerns + [
                    "Phase quota likely exhausted — retry next day"
                ],
                authentic_excerpt=result.authentic_excerpt,
                reviewer=result.reviewer,
            )

        if result.confidence < self._confidence_threshold:
            return TafsirValidationResult(
                passed=False,
                confidence=result.confidence,
                concerns=result.concerns + [
                    f"Confidence {result.confidence:.2f} below threshold "
                    f"{self._confidence_threshold}"
                ],
                authentic_excerpt=result.authentic_excerpt,
                reviewer=result.reviewer,
            )

        return result

    def validate_episode(
        self,
        episode_data: Dict,
        surah: int,
        surah_name: str,
    ) -> Tuple[bool, List[str]]:
        """Validate ALL ayahs in an episode using a SINGLE Gemini call.

        v22.5.1: switched from per-ayah loop (7 calls) to batched review
        (1 call). Saves 6/7 of daily quota per episode. Falls back to the
        per-ayah path automatically if reviewer is unavailable or batching
        is somehow disabled.

        Returns (all_passed, combined_concerns).
        """
        scenes = episode_data.get("ayah_scenes", [])
        if not scenes:
            logger.warning("⚠️ validate_episode called with no ayah_scenes")
            return True, []

        # Build batch payload — fetch authentic tafsir for each ayah first.
        # Tafsir fetch is cheap (HTTP cached), Gemini is the expensive resource.
        batch_payload: List[Dict[str, Any]] = []
        skipped: List[Tuple[int, str]] = []  # (ayah_num, reason)

        for scene in scenes:
            ayah = scene.get("ayah", {})
            ayah_num = ayah.get("number", 0)
            ayah_text = ayah.get("text", "")
            llm_explanation = scene.get("explain_text", "")
            llm_analogy = scene.get("story_text", "")

            if not (ayah_text and llm_explanation):
                skipped.append((ayah_num, "missing ayah text or explanation"))
                continue

            try:
                authentic = self._fetcher.fetch_combined(
                    surah=surah, ayah=ayah_num,
                )
            except Exception as e:
                skipped.append((ayah_num, f"tafsir fetch failed: {e}"))
                continue

            if not authentic:
                skipped.append((ayah_num, "no authentic tafsir available"))
                continue

            batch_payload.append({
                "ayah_number": ayah_num,
                "ayah_text": ayah_text,
                "llm_explanation": llm_explanation,
                "llm_analogy": llm_analogy,
                "authentic_tafsir": authentic,
            })

        # If reviewer can't do batch (no keys), fail loudly
        if self._gemini_reviewer is None or not self._gemini_reviewer._available:
            logger.error("❌ TafsirValidator: reviewer unavailable")
            return False, ["Reviewer unavailable for batch review"]

        # Single Gemini call for ALL ayahs
        results = self._gemini_reviewer.review_batch(
            ayahs_payload=batch_payload,
            surah_name=surah_name,
        )

        # Pair results back with ayah numbers and check threshold
        all_passed = True
        all_concerns: List[str] = []

        for payload, result in zip(batch_payload, results):
            ayah_num = payload["ayah_number"]
            ayah_label = f"{surah_name} {ayah_num}"

            # Apply confidence threshold (same logic as single review path)
            effectively_passed = (
                result.passed
                and result.confidence >= self._confidence_threshold
            )

            if not effectively_passed:
                all_passed = False
                if result.concerns:
                    all_concerns.extend([f"[{ayah_label}] {c}" for c in result.concerns])
                else:
                    all_concerns.append(
                        f"[{ayah_label}] confidence {result.confidence:.2f} "
                        f"below threshold {self._confidence_threshold}"
                    )
                logger.warning(
                    f"⚠️ Religious validation FAILED for {ayah_label} "
                    f"(reviewer={result.reviewer}, confidence={result.confidence:.2f})"
                )
                # v22.5.4: log the actual concerns so we can debug failures
                # without having to inspect artifacts. Gemini's concerns explain
                # WHY the script was rejected — without these logs, we just see
                # "FAILED" with no clue about the root cause.
                if result.concerns:
                    for i, concern in enumerate(result.concerns, 1):
                        logger.warning(f"   └─ concern #{i}: {concern}")
                else:
                    logger.warning(
                        f"   └─ Gemini said passed=True but confidence "
                        f"{result.confidence:.2f} < threshold "
                        f"{self._confidence_threshold:.2f}"
                    )
            else:
                logger.info(
                    f"✅ Religious OK: {ayah_label} "
                    f"(reviewer={result.reviewer}, confidence={result.confidence:.2f})"
                )

        # Skipped ayahs are also failures
        for ayah_num, reason in skipped:
            all_passed = False
            all_concerns.append(f"[{surah_name} {ayah_num}] skipped: {reason}")
            logger.warning(f"⚠️ Skipped ayah {ayah_num}: {reason}")

        # Flush disk cache (best-effort)
        try:
            cache_obj = getattr(self._fetcher, "_cache", None)
            if cache_obj is not None and hasattr(cache_obj, "flush"):
                cache_obj.flush()
                logger.debug("📚 Tafsir cache flushed to disk")
        except Exception as e:
            logger.warning(f"⚠️ Tafsir cache flush failed (non-fatal): {e}")

        return all_passed, all_concerns

    # ─────────────────────────────────────────────────────────────
    # Backward compat for orchestrator
    # ─────────────────────────────────────────────────────────────
    def validate_episode_batched_v20(
        self,
        episode_data: Dict,
        surah: int,
        surah_name: str,
    ) -> Tuple[bool, List[str]]:
        """v22.5: kept for orchestrator compatibility — delegates to per-ayah.

        The original v20 design did one Claude call for the whole episode
        (cost optimization). With Claude removed and Gemini's 5 RPM ceiling,
        batching no longer helps — we MUST throttle per-ayah anyway.
        """
        return self.validate_episode(episode_data, surah, surah_name)
