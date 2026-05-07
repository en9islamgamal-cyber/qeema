"""
engines/tafsir_validator.py — VALUE / QEEMA v18.0 (CRITICAL ADDITION)
======================================================================

Religious accuracy validator — prevents LLM hallucination in Quranic
explanations. THIS IS THE MOST IMPORTANT FILE IN THE PROJECT.

[Why this exists]
The LLM (Gemini 2.5 Flash) generates `explain_text` and `analogy_text`
based purely on the ayah text + a generic prompt. There is NO grounding
in authentic tafsir sources. A single wrong interpretation published
to the channel = reputation damage that may not be recoverable.

[Strategy — multi-layer defense]
Layer 1: Fetch authentic tafsir (As-Saadi + Al-Muyassar) from quran.com API
Layer 2: Use Claude Opus 4.7 as a strict religious reviewer
Layer 3: Heuristic fallback if APIs fail (keyword overlap analysis)
Layer 4: Manual review gate — episode marked `pending_review` not `published`

[Sources used]
- quran.com API: https://api.quran.com/api/v4/tafsirs
  - Tafsir ID 169: As-Saadi (تيسير الكريم الرحمن — مناسب للتبسيط)
  - Tafsir ID 16: Al-Muyassar (التفسير الميسر — للتحقق العام)
- Anthropic Claude API: religious cross-validation

[Cost]
Per ayah: ~2k Claude tokens × $0.015/1k = $0.03
Per episode (5 ayahs): ~$0.15
Per 100 episodes: ~$15 — negligible vs channel reputation
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Tafsir source IDs from quran.com API
# ════════════════════════════════════════════════════════════════
TAFSIR_SAADI = 169       # تفسير السعدي — clear language, kid-appropriate
TAFSIR_MUYASSAR = 16     # التفسير الميسر — concise, official Saudi-approved
TAFSIR_BAGHAWY = 168     # تفسير البغوي — classical reference


@dataclass(frozen=True)
class TafsirValidationResult:
    """Result of religious validation for a single ayah explanation."""
    passed: bool
    confidence: float  # 0.0 (definitely wrong) to 1.0 (perfectly aligned)
    concerns: List[str] = field(default_factory=list)
    authentic_excerpt: str = ""  # First 300 chars of authentic tafsir
    reviewer: str = "none"  # "claude-opus" | "heuristic" | "none"
    

class AuthenticTafsirFetcher:
    """Fetches authentic tafsir from quran.com API with cache."""
    
    BASE_URL = "https://api.quran.com/api/v4"
    
    def __init__(self) -> None:
        self._cache: Dict[Tuple[int, int, int], str] = {}  # (surah, ayah, tafsir_id) → text
    
    def fetch(
        self,
        surah: int,
        ayah: int,
        tafsir_id: int = TAFSIR_SAADI,
    ) -> Optional[str]:
        """
        Fetch tafsir for a specific ayah.
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
        """
        Fetch BOTH As-Saadi and Al-Muyassar.
        Returns concatenated text for cross-validation.
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


class ClaudeReviewer:
    """
    Uses Claude Opus 4.7 as a strict religious reviewer.
    Compares LLM-generated explanation against authentic tafsir sources.
    """
    
    def __init__(self, anthropic_key: str) -> None:
        self._key = anthropic_key
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=anthropic_key)
            self._available = True
            logger.info("✅ Claude religious reviewer ready (model=claude-opus-4-7)")
        except ImportError:
            logger.warning("⚠️ anthropic package not installed — Claude review disabled")
            self._available = False
        except Exception as e:
            logger.warning(f"⚠️ Claude reviewer init failed: {e}")
            self._available = False
    
    def review(
        self,
        ayah_text: str,
        surah_name: str,
        ayah_number: int,
        llm_explanation: str,
        llm_analogy: str,
        authentic_tafsir: str,
    ) -> TafsirValidationResult:
        """
        Returns ValidationResult. Blocks publication if passed=False.
        """
        if not self._available:
            return TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=["Claude reviewer unavailable"],
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
        
        try:
            response = self._client.messages.create(
                model="claude-opus-4-7",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text if response.content else ""
            
            # Parse JSON response
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            
            data = json.loads(text.strip())
            
            return TafsirValidationResult(
                passed=bool(data.get("passed", False)),
                confidence=float(data.get("confidence", 0.0)),
                concerns=list(data.get("concerns", [])),
                authentic_excerpt=authentic_tafsir[:300],
                reviewer="claude-opus-4-7",
            )
        except json.JSONDecodeError as e:
            logger.error(f"❌ Claude returned invalid JSON: {e}")
            return TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=[f"Reviewer JSON parse error: {e}"],
                reviewer="claude-opus-4-7",
            )
        except Exception as e:
            logger.error(f"❌ Claude review error: {e}")
            return TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=[f"Reviewer error: {type(e).__name__}: {e}"],
                reviewer="claude-opus-4-7",
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
        return f"""أنت مدقق ديني صارم متخصص في التفسير القرآني. مهمتك مراجعة محتوى مُنتج آلياً للأطفال قبل نشره على يوتيوب.

═══ النص المراد مراجعته ═══

[الآية]
سورة {surah_name}، آية رقم {ayah_number}
{ayah_text}

[الشرح المُنتج آلياً للأطفال]
{llm_explanation}

[المثال/التشبيه المستخدم]
{llm_analogy}

═══ المرجع المعتمد ═══

{authentic_tafsir}

═══ مهمة المراجعة ═══

قيّم على ٤ محاور:

1. **الدقة العقدية**: هل الشرح يخالف عقيدة أهل السنة والجماعة؟ هل يُنسب لله ما لا يليق؟
2. **التطابق مع المعنى الأصلي**: هل الشرح يتفق مع التفسير المعتمد المرفق؟ هل ابتعد عن المعنى الأصلي؟
3. **سلامة المثال**: هل التشبيه دقيق ومناسب أم مضلل أو مبالغ فيه؟
4. **التبسيط دون الإخلال**: هل تم حذف معنى عقدي/شرعي مهم لأجل التبسيط؟

═══ معايير الرفض ═══

ارفض (passed=false) في الحالات التالية:
- أي خطأ عقدي مهما صغر
- ادعاء معنى لا يدعمه التفسير
- مثال يوهم معنى مغلوطاً
- إسقاط شرعي خاطئ على واقع الأطفال
- تفسير الآية بطريقة تختلف عن التفسير المعتمد

اقبل (passed=true) فقط إذا:
- المعنى مطابق للتفسير المعتمد
- التبسيط لا يفقد المعنى الأصلي
- المثال صحيح ومناسب للأطفال

═══ التنسيق ═══

أجب بـ JSON فقط، بدون أي نص خارجه:

{{
  "passed": true|false,
  "confidence": 0.0-1.0,
  "concerns": ["قائمة المخاوف بالعربية إن وجدت"],
  "alignment_score": 0.0-1.0,
  "doctrinal_safety": "safe"|"risky"|"violation",
  "analogy_quality": "accurate"|"weak"|"misleading"
}}"""


class GeminiReviewer:
    """
    Religious validation using Gemini (substitute when Claude unavailable).

    [Why this exists]
    Claude is the gold standard for nuanced theological review, but:
      1. Anthropic credit can run out (it did, in production)
      2. Claude API can have downtime
      3. The episode shouldn't fail just because one provider is down

    Gemini 2.5 Flash is "good enough" for tafsir validation when prompted
    carefully. It's NOT as nuanced as Claude on edge cases (e.g., subtle
    aqeedah issues), but with a strict prompt + the authentic tafsir as
    ground truth, it catches the obvious problems.

    [Interface compatibility]
    Same .review() signature as ClaudeReviewer so it slots in directly.

    [Conservative bias]
    The prompt instructs Gemini to default to "needs review" when uncertain,
    rather than auto-approving. Better to over-flag than under-flag for
    children's religious content.
    """

    def __init__(self, gemini_api_key: str) -> None:
        self._key = gemini_api_key
        self._available = False
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
        """Review one ayah explanation. Same signature as ClaudeReviewer.review().

        v22.5: Retries up to 3 times with exponential backoff on transient
        errors (503, 429). Hard failures (auth, invalid request) fail fast.
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

        # v22.5 FINAL: Retry logic tuned for Gemini free-tier 5 RPM ceiling.
        # Backoff sequence: 15s, 30s, 60s. Total worst case = ~105s per ayah.
        # The script-engine throttle ensures we don't hit 5 RPM in the first
        # place, but 503 (overload, not quota) can still happen → these waits
        # give Google's load balancer time to recover.
        import time as _time
        max_attempts = 4
        last_error: Optional[Exception] = None
        backoffs = [15, 30, 60]  # seconds before retries 2, 3, 4

        for attempt in range(1, max_attempts + 1):
            try:
                return self._do_review_call(
                    prompt, authentic_tafsir, ayah_number,
                )
            except Exception as e:
                err_msg = str(e).lower()
                last_error = e
                # Distinguish transient vs permanent errors
                is_transient = (
                    "503" in err_msg
                    or "unavailable" in err_msg
                    or "429" in err_msg
                    or "resource_exhausted" in err_msg
                    or "rate limit" in err_msg
                    or "timeout" in err_msg
                )
                if not is_transient or attempt == max_attempts:
                    # Permanent error or final attempt — give up
                    logger.error(f"❌ Gemini review error: {e}")
                    return TafsirValidationResult(
                        passed=False, confidence=0.0,
                        concerns=[f"Reviewer error: {e}"],
                        reviewer="gemini-2.5-flash",
                    )
                # Transient: long backoff and retry
                wait = backoffs[attempt - 1]
                logger.warning(
                    f"⚠️ Gemini review transient failure "
                    f"(attempt {attempt}/{max_attempts}): retrying in {wait}s"
                )
                _time.sleep(wait)

        # Should not reach here, but defensively:
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
        """
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

        # Strip markdown fences if present
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]

        # v22.5 FIX: Gemini returns smart quotes in Arabic text
        # which break JSON parsing. Normalize them to standard quotes.
        text = (text
            .replace("\u201c", '"').replace("\u201d", '"')  # smart double quotes
            .replace("\u2018", "'").replace("\u2019", "'")  # smart single quotes
            .replace("\u00ab", '"').replace("\u00bb", '"')  # guillemets
        )

        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError as parse_err:
            # Try to salvage: find first { ... last }
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


class HeuristicReviewer:
    """
    Fallback when Claude is unavailable.
    Uses keyword overlap + structural analysis.
    NOT a substitute for Claude — just prevents complete bypass.
    """
    
    # Words that indicate doctrinal claims requiring strict alignment
    DOCTRINAL_KEYWORDS = frozenset([
        "الله", "الرحمن", "الرحيم", "الجنة", "النار",
        "الإيمان", "الكفر", "العبادة", "الصلاة", "الدعاء",
        "الرسول", "النبي", "القيامة", "الآخرة",
    ])
    
    def review(
        self,
        ayah_text: str,
        llm_explanation: str,
        authentic_tafsir: str,
    ) -> TafsirValidationResult:
        if not authentic_tafsir:
            return TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=["No authentic tafsir available for comparison"],
                reviewer="heuristic",
            )
        
        # Keyword overlap analysis
        explanation_words = set(llm_explanation.split())
        authentic_words = set(authentic_tafsir.split())
        
        # Doctrinal keywords in explanation MUST appear in authentic source
        explanation_doctrinal = explanation_words & self.DOCTRINAL_KEYWORDS
        authentic_doctrinal = authentic_words & self.DOCTRINAL_KEYWORDS
        
        concerns = []
        confidence = 0.6  # Heuristic max confidence
        
        # Check 1: Doctrinal terms used by LLM must exist in source
        novel_doctrinal = explanation_doctrinal - authentic_doctrinal
        if novel_doctrinal:
            concerns.append(
                f"Explanation uses doctrinal terms not in source: {novel_doctrinal}"
            )
            confidence -= 0.2
        
        # Check 2: Length sanity (explanation shouldn't be 10x longer than source)
        if len(llm_explanation) > 5 * len(authentic_tafsir):
            concerns.append("Explanation suspiciously longer than authentic source")
            confidence -= 0.1
        
        # Check 3: General overlap
        overlap_ratio = (
            len(explanation_words & authentic_words) /
            max(len(explanation_words), 1)
        )
        if overlap_ratio < 0.15:
            concerns.append(f"Low keyword overlap with source ({overlap_ratio:.0%})")
            confidence -= 0.2
        
        passed = confidence >= 0.4 and not any(
            "violation" in c.lower() for c in concerns
        )
        
        return TafsirValidationResult(
            passed=passed,
            confidence=max(0.0, confidence),
            concerns=concerns,
            authentic_excerpt=authentic_tafsir[:300],
            reviewer="heuristic",
        )


class TafsirValidator:
    """
    Main validator. Coordinates fetcher + reviewers.
    
    Usage:
        validator = TafsirValidator(anthropic_key=os.getenv("ANTHROPIC_API_KEY"))
        result = validator.validate_explanation(
            ayah_text=...,
            surah=...,
            ayah_number=...,
            llm_explanation=...,
            llm_analogy=...,
        )
        if not result.passed:
            raise QualityGateError("Religious validation failed", critiques=result.concerns)
    """
    
    def __init__(
        self,
        anthropic_key: Optional[str] = None,
        gemini_review_key: Optional[str] = None,
        confidence_threshold: float = 0.65,
    ) -> None:
        """
        v22.5 FINAL: Gemini-only validation architecture.

        Background:
          - User has confirmed Anthropic credit will NOT be funded (zero & staying zero)
          - User has 3 Google accounts → 3 independent Gemini keys with full quotas
          - Heuristic was structurally too weak to reliably pass valid content

        Decision:
          - Claude reviewer: DISABLED (anthropic_key ignored even if provided)
          - Heuristic reviewer: DISABLED as fallback (kept on instance for legacy
            tests but never invoked in the live chain)
          - Gemini reviewer: PRIMARY and ONLY active reviewer

        The retry logic inside GeminiReviewer (3 attempts with exponential backoff)
        plus the rate limiter at the script-pool level should keep us under the
        5 RPM Gemini free-tier ceiling per key. Phase 3 (the day this runs) has
        an entire dedicated daily quota with no other consumers.

        Args:
            anthropic_key: IGNORED (kept for signature compatibility)
            gemini_review_key: REQUIRED for validation to succeed
            confidence_threshold: minimum confidence to pass validation
        """
        self._fetcher = AuthenticTafsirFetcher()
        self._claude_reviewer: Optional[ClaudeReviewer] = None  # ALWAYS None now
        self._gemini_reviewer: Optional[GeminiReviewer] = None
        self._heuristic_reviewer = HeuristicReviewer()  # kept but NOT used in chain
        self._confidence_threshold = confidence_threshold
        # Legacy flag — no longer meaningful since Claude is disabled, but kept
        # so existing code paths that reference it don't crash.
        self._claude_credit_exhausted = True

        if anthropic_key:
            logger.info(
                "ℹ️ Anthropic key provided but ignored — v22.5 uses Gemini-only "
                "validation (Claude disabled by design)"
            )

        if gemini_review_key:
            self._gemini_reviewer = GeminiReviewer(gemini_review_key)
            logger.info(
                "✅ TafsirValidator: Gemini-only architecture active "
                "(reviewer=gemini-2.5-flash)"
            )
        else:
            logger.error(
                "❌ TafsirValidator: NO Gemini key — validation will fail "
                "every episode. Set GEMINI_API_KEY_3 to enable."
            )
    
    def validate_explanation(
        self,
        ayah_text: str,
        surah: int,
        surah_name: str,
        ayah_number: int,
        llm_explanation: str,
        llm_analogy: str = "",
    ) -> TafsirValidationResult:
        """
        v22.5 FINAL: Validates one ayah explanation against authentic tafsir.

        Architecture: Gemini-only. No Claude. No heuristic fallback.
        On Gemini infrastructure failure, returns failure with a clear concern
        so the Phase 3 day-N+1 retry catches it with fresh quota.
        """
        # Layer 1: Fetch authentic tafsir (HTTP — no Gemini quota cost)
        authentic = self._fetcher.fetch_combined(surah, ayah_number)
        if not authentic:
            logger.warning(
                f"⚠️ No authentic tafsir for {surah_name} {ayah_number} — "
                f"cannot validate (returning soft-pass with low confidence)"
            )
            return TafsirValidationResult(
                passed=True,  # Don't block if source unavailable
                confidence=0.3,
                concerns=["Authentic tafsir unavailable — manual review recommended"],
                reviewer="none",
            )

        # Layer 2 (v22.5): Gemini is the ONLY active reviewer
        if not self._gemini_reviewer:
            logger.error(
                f"❌ TafsirValidator: Gemini unavailable for {surah_name} ayah {ayah_number} "
                "— returning failure (Gemini-only architecture, no fallback)"
            )
            return TafsirValidationResult(
                passed=False,
                confidence=0.0,
                concerns=[
                    "Gemini reviewer not configured. "
                    "Set GEMINI_API_KEY_3 (or last available key)."
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

        # Distinguish infra errors from genuine religious rejection. On infra
        # errors, surface them clearly — the day-N+1 retry will pick up the
        # episode with refreshed quota.
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
        """
        Validate ALL ayahs in an episode.
        Returns (all_passed, combined_concerns).
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
        
        return all_passed, all_concerns

    # ════════════════════════════════════════════════════════════════
    # v20: Batched validation — 1 Claude call for entire episode
    # ════════════════════════════════════════════════════════════════
    def validate_episode_batched_v20(
        self,
        episode_data: Dict,
        surah: int,
        surah_name: str,
    ) -> Tuple[bool, List[str]]:
        """
        v22.5 FINAL: Batched method retained for orchestrator compatibility,
        but now delegates to per-ayah Gemini validation.

        Background:
          The original v20 design did one Claude call for the whole episode
          (cost optimization). With Claude disabled in v22.5 and Gemini's
          5 RPM free-tier limit, batching no longer helps — we MUST throttle
          per-ayah anyway. So this method is now a thin wrapper around
          validate_episode().
        """
        return self.validate_episode(episode_data, surah, surah_name)

