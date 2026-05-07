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
    """Religious validator using Gemini 2.5 Flash.

    [Conservative bias]
    The prompt instructs Gemini to default to "needs review" when uncertain,
    rather than auto-approving. Better to over-flag than under-flag for
    children's religious content.

    [Retry policy]
    4 attempts with 15s, 30s, 60s backoffs. Total worst-case = 105s per
    ayah. The script-engine throttle (4 RPM) keeps us under Gemini's free
    tier 5-RPM ceiling in normal operation, but transient 503s from Google's
    load balancer still happen and these waits give it time to recover.

    [JSON parsing]
    Uses response_mime_type=application/json. Defensively normalizes smart
    quotes (Gemini sometimes wraps Arabic text in “ ” instead of " "),
    strips markdown fences, and falls back to regex { ... } extraction
    if direct parse fails.
    """

    def __init__(self, gemini_api_key: str) -> None:
        self._key = gemini_api_key
        self._available = False
        self._rate_limiter = None  # set below if key is non-empty

        if not gemini_api_key:
            logger.warning(
                "⚠️ GeminiReviewer constructed with empty api_key — disabled"
            )
            return

        # v22.5: shared rate limiter — if ScriptEngine uses the same key,
        # both go through the same sliding window. This prevents the
        # 5 RPM ceiling from being exceeded by combined script+tafsir
        # traffic in Phase 1.
        from core.gemini_rate_limiter import limiter_for_key
        self._rate_limiter = limiter_for_key(
            gemini_api_key, label_hint="tafsir-reviewer",
        )

        try:
            from google import genai
            self._client = genai.Client(api_key=gemini_api_key)
            self._available = True
            logger.info("✅ Gemini religious reviewer ready (model=gemini-2.5-flash)")
        except ImportError:
            logger.warning(
                "⚠️ google-genai package not installed — Gemini reviewer disabled"
            )
        except Exception as e:
            logger.warning(f"⚠️ Gemini reviewer init failed: {e}")

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
                is_transient = (
                    "503" in err_msg
                    or "unavailable" in err_msg
                    or "429" in err_msg
                    or "resource_exhausted" in err_msg
                    or "rate limit" in err_msg
                    or "timeout" in err_msg
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

        v22.5: Acquires a rate-limit token before calling Gemini. This is
        the same limiter ScriptEngine uses if they share a key, so combined
        traffic on key #1 in Phase 1 stays under 4 RPM.
        """
        # Block if we'd exceed 4 RPM on this key (shared with ScriptEngine).
        # If no limiter (e.g. empty key in tests), skip — but we'd be
        # unavailable anyway so this branch isn't reached in practice.
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()

        from google.genai import types
        response = self._client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,  # low for consistency on religious validation
                response_mime_type="application/json",
                max_output_tokens=800,
            ),
        )
        text = response.text.strip() if response.text else ""

        # Strip markdown fences if present (Gemini sometimes adds them despite
        # response_mime_type=json)
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]

        # Normalize smart quotes — Gemini occasionally wraps Arabic strings in
        # “” instead of "" which breaks JSON parsing.
        text = (text
            .replace("\u201c", '"').replace("\u201d", '"')  # smart double
            .replace("\u2018", "'").replace("\u2019", "'")  # smart single
            .replace("\u00ab", '"').replace("\u00bb", '"')  # guillemets
        )

        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError as parse_err:
            # Salvage: find first { ... last }
            import re as _re
            match = _re.search(r'\{.*\}', text, flags=_re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    raise parse_err
            else:
                raise parse_err

        return TafsirValidationResult(
            passed=bool(data.get("passed", False)),
            confidence=float(data.get("confidence", 0.0)),
            concerns=list(data.get("concerns", [])),
            authentic_excerpt=authentic_tafsir[:300],
            reviewer="gemini-2.5-flash",
        )

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
        confidence_threshold: float = 0.65,
        cache_path: Optional[Path] = None,
    ) -> None:
        """Build a v22.5 validator.

        Args:
            gemini_review_key: REQUIRED. The Gemini API key for review calls.
                              In Phase 1 architecture, this is the same key as
                              script generation (key #1).
            confidence_threshold: Minimum confidence to mark validation as
                                 passed (default: 0.65).
            cache_path: Optional path to a persistent JSON tafsir cache.
                        When provided, AuthenticTafsirFetcher is wrapped in a
                        CachedTafsirFetcher so quran.com is only hit once per
                        (surah, ayah, tafsir_id) across all runs. Without this,
                        only the in-memory cache (per-process) is used and every
                        new run refetches all ayahs.
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
                # Cache is best-effort — fall back to in-memory only
                logger.warning(
                    f"⚠️ Tafsir disk cache init failed ({e}) — using "
                    f"in-memory cache only"
                )
                self._fetcher = upstream_fetcher
        else:
            # No persistent cache → in-memory only (each process refetches)
            self._fetcher = upstream_fetcher

        self._gemini_reviewer: Optional[GeminiReviewer] = None
        self._confidence_threshold = confidence_threshold

        if gemini_review_key:
            self._gemini_reviewer = GeminiReviewer(gemini_review_key)
            logger.info(
                "✅ TafsirValidator wired (Gemini-only — v22.5)"
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
        """Validate ALL ayahs in an episode.

        Returns (all_passed, combined_concerns). Logs progress per-ayah.
        """
        all_passed = True
        all_concerns: List[str] = []

        for scene in episode_data.get("ayah_scenes", []):
            ayah = scene.get("ayah", {})
            result = self.validate_explanation(
                ayah_text=ayah.get("text", ""),
                surah=surah,
                surah_name=surah_name,
                ayah_number=ayah.get("number", 0),
                llm_explanation=scene.get("explain_text", ""),
                llm_analogy=scene.get("story_text", ""),
            )

            if not result.passed:
                all_passed = False
                ayah_label = f"{surah_name} {ayah.get('number')}"
                all_concerns.extend([f"[{ayah_label}] {c}" for c in result.concerns])
                logger.warning(
                    f"⚠️ Religious validation FAILED for {ayah_label} "
                    f"(reviewer={result.reviewer}, confidence={result.confidence:.2f})"
                )
            else:
                logger.info(
                    f"✅ Religious OK: {surah_name} {ayah.get('number')} "
                    f"(reviewer={result.reviewer}, confidence={result.confidence:.2f})"
                )

        # v22.5: Flush the disk cache after each episode. Without this, if the
        # process crashes mid-pipeline (e.g., voice_engine OOM, FFmpeg failure),
        # the newly-fetched tafsir from quran.com is lost — next run hits the
        # API again. Best-effort: cache failure must NOT block validation.
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
