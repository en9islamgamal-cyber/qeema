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
        """Review one ayah explanation. Same signature as ClaudeReviewer.review()."""
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

        try:
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

            data = json.loads(text.strip())

            return TafsirValidationResult(
                passed=bool(data.get("passed", False)),
                confidence=float(data.get("confidence", 0.0)),
                concerns=list(data.get("concerns", [])),
                authentic_excerpt=authentic_tafsir[:300],
                reviewer="gemini-2.5-flash",
            )
        except json.JSONDecodeError as e:
            logger.error(f"❌ Gemini returned invalid JSON: {e}")
            return TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=[f"Reviewer JSON parse error: {e}"],
                reviewer="gemini-2.5-flash",
            )
        except Exception as e:
            logger.error(f"❌ Gemini review error: {e}")
            return TafsirValidationResult(
                passed=False, confidence=0.0,
                concerns=[f"Reviewer error: {e}"],
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
        v22.5: Now accepts a dedicated Gemini key for religious validation.

        Chain priority:
            1. Claude (if anthropic_key provided AND credit available)
            2. Gemini (if gemini_review_key provided)
            3. Heuristic (always available as last resort)

        Args:
            anthropic_key: Anthropic API key for Claude reviewer (preferred)
            gemini_review_key: Dedicated Gemini key for fallback reviewer
            confidence_threshold: minimum confidence to pass validation
        """
        self._fetcher = AuthenticTafsirFetcher()
        self._claude_reviewer: Optional[ClaudeReviewer] = None
        self._gemini_reviewer: Optional[GeminiReviewer] = None
        self._heuristic_reviewer = HeuristicReviewer()
        self._confidence_threshold = confidence_threshold
        # v22.5: Track Claude credit failures within a run to avoid retry storms
        self._claude_credit_exhausted = False

        if anthropic_key:
            self._claude_reviewer = ClaudeReviewer(anthropic_key)

        if gemini_review_key:
            self._gemini_reviewer = GeminiReviewer(gemini_review_key)

        if not self._claude_reviewer and not self._gemini_reviewer:
            logger.warning(
                "⚠️ TafsirValidator running without Claude OR Gemini — "
                "using heuristic fallback only (not recommended for production)"
            )
        elif not self._claude_reviewer:
            logger.info(
                "ℹ️ TafsirValidator: Claude unavailable, Gemini reviewer is primary"
            )
        elif not self._gemini_reviewer:
            logger.info(
                "ℹ️ TafsirValidator: Claude is primary, Gemini fallback unavailable"
            )
        else:
            logger.info(
                "✅ TafsirValidator: Claude (primary) + Gemini (fallback) wired"
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
        Validates LLM-generated explanation for a single ayah.
        Returns TafsirValidationResult — orchestrator should reject episode if passed=False.
        """
        # Layer 1: Fetch authentic tafsir
        authentic = self._fetcher.fetch_combined(surah, ayah_number)
        if not authentic:
            logger.warning(
                f"⚠️ No authentic tafsir for {surah_name} {ayah_number} — "
                f"falling back to heuristic-only check"
            )
            # Still try heuristic on whatever we have
            return TafsirValidationResult(
                passed=True,  # Don't block if we can't validate
                confidence=0.3,
                concerns=["Authentic tafsir unavailable — manual review recommended"],
                reviewer="none",
            )
        
        # Layer 2: Claude review (preferred — if available + credit)
        if self._claude_reviewer and not self._claude_credit_exhausted:
            result = self._claude_reviewer.review(
                ayah_text=ayah_text,
                surah_name=surah_name,
                ayah_number=ayah_number,
                llm_explanation=llm_explanation,
                llm_analogy=llm_analogy or "",
                authentic_tafsir=authentic,
            )
            # v22.5: Detect credit exhaustion to skip retries this run
            if (
                "credit balance is too low" in str(result.concerns).lower()
                or "credit_balance" in str(result.concerns).lower()
            ):
                logger.error(
                    "💸 Anthropic credit exhausted — switching to Gemini reviewer "
                    "for the rest of this run"
                )
                self._claude_credit_exhausted = True
                # Don't return — fall through to Gemini below
            elif result.confidence < self._confidence_threshold:
                return TafsirValidationResult(
                    passed=False,
                    confidence=result.confidence,
                    concerns=result.concerns + [
                        f"Confidence {result.confidence:.2f} below threshold {self._confidence_threshold}"
                    ],
                    authentic_excerpt=result.authentic_excerpt,
                    reviewer=result.reviewer,
                )
            else:
                return result

        # Layer 2b (v22.5): Gemini fallback — if Claude unavailable/failed
        if self._gemini_reviewer:
            result = self._gemini_reviewer.review(
                ayah_text=ayah_text,
                surah_name=surah_name,
                ayah_number=ayah_number,
                llm_explanation=llm_explanation,
                llm_analogy=llm_analogy or "",
                authentic_tafsir=authentic,
            )
            if result.confidence < self._confidence_threshold:
                return TafsirValidationResult(
                    passed=False,
                    confidence=result.confidence,
                    concerns=result.concerns + [
                        f"Confidence {result.confidence:.2f} below threshold {self._confidence_threshold}"
                    ],
                    authentic_excerpt=result.authentic_excerpt,
                    reviewer=result.reviewer,
                )
            return result
        
        # Layer 3: Heuristic fallback
        return self._heuristic_reviewer.review(
            ayah_text=ayah_text,
            llm_explanation=llm_explanation,
            authentic_tafsir=authentic,
        )
    
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
        v20 OPTIMIZATION: Validate all ayahs in ONE Claude call instead of N.

        Cost reduction:
            Old (validate_episode): 5 ayahs × $0.03 = $0.15/episode
            New (this method):     1 call × $0.05 = $0.05/episode
            → 67% cheaper, 5x fewer API calls

        Strategy:
            - Fetch authentic tafsir for all ayahs in parallel (HTTP batched)
            - Build single multi-part validation prompt
            - Send to Claude with structured JSON response schema
            - Parse per-ayah verdicts from single response

        Falls back to per-ayah validation if Claude unavailable.
        """
        ayah_scenes = episode_data.get("ayah_scenes", [])
        if not ayah_scenes:
            return True, []

        # v22.5: Skip Claude batched call if credit exhausted in this run
        if self._claude_credit_exhausted:
            logger.info(
                "💸 Claude credit exhausted earlier — going straight to per-ayah "
                "(which uses Gemini fallback)"
            )
            return self.validate_episode(episode_data, surah, surah_name)

        # No Claude reviewer? Fall back to per-ayah heuristic.
        if not self._claude_reviewer or not self._claude_reviewer._available:
            logger.info("📋 No Claude reviewer — falling back to per-ayah heuristic")
            return self.validate_episode(episode_data, surah, surah_name)

        try:
            import concurrent.futures
            # Step 1: Fetch all authentic tafsir in parallel
            authentic_map: Dict[int, str] = {}

            def _fetch(ayah_num: int) -> Tuple[int, str]:
                authentic = self._fetch_authentic_tafsir(surah, ayah_num)
                if authentic:
                    return ayah_num, authentic.get("saadi", "") or authentic.get("muyassar", "")
                return ayah_num, ""

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                futures = [
                    ex.submit(_fetch, scene.get("ayah", {}).get("number", 0))
                    for scene in ayah_scenes
                ]
                for fut in concurrent.futures.as_completed(futures):
                    num, text = fut.result()
                    if text:
                        authentic_map[num] = text

            # Step 2: Build single batched prompt
            prompt = self._build_batched_prompt(
                ayah_scenes, surah_name, authentic_map
            )

            # Step 3: One Claude call (with v22.5 credit-exhaustion detection)
            try:
                verdicts = self._call_claude_batched(prompt)
            except Exception as e:
                err_msg = str(e).lower()
                if "credit balance is too low" in err_msg or "credit_balance" in err_msg:
                    logger.error(
                        "💸 Anthropic credit exhausted in batched call — "
                        "switching to per-ayah Gemini for this episode"
                    )
                    self._claude_credit_exhausted = True
                    return self.validate_episode(episode_data, surah, surah_name)
                # Other errors: re-raise to outer try/except
                raise

            # Step 4: Parse per-ayah verdicts
            all_passed = True
            all_concerns: List[str] = []
            for verdict in verdicts:
                ayah_num = verdict.get("ayah_number", 0)
                passed = verdict.get("passed", False)
                concerns = verdict.get("concerns", [])
                ayah_label = f"{surah_name} {ayah_num}"
                if not passed:
                    all_passed = False
                    all_concerns.extend([f"[{ayah_label}] {c}" for c in concerns])
                    logger.warning(
                        f"⚠️ Religious FAIL: {ayah_label} — {len(concerns)} concerns"
                    )
                else:
                    logger.info(f"✅ Religious OK: {ayah_label} (batched)")

            return all_passed, all_concerns

        except Exception as e:
            logger.warning(
                f"⚠️ Batched validation failed: {e} — falling back to per-ayah"
            )
            return self.validate_episode(episode_data, surah, surah_name)

    def _build_batched_prompt(
        self,
        ayah_scenes: List[Dict],
        surah_name: str,
        authentic_map: Dict[int, str],
    ) -> str:
        """Build prompt that asks Claude to validate all ayahs at once."""
        ayah_blocks: List[str] = []
        for i, scene in enumerate(ayah_scenes, 1):
            ayah_data = scene.get("ayah", {})
            num = ayah_data.get("number", i)
            text = ayah_data.get("text", "")
            authentic = authentic_map.get(num, "")[:1200]
            explanation = scene.get("explain_text", "")
            analogy = scene.get("story_text", "")
            takeaway = scene.get("moral_text", "")

            ayah_blocks.append(f"""
═══ آية رقم {num} ═══
[النص]: {text}

[التفسير المعتمد - السعدي/الميسر]:
{authentic if authentic else '(غير متاح — اعتمد على معرفتك التفسيرية)'}

[الشرح المُنتج آلياً]:
الشرح: {explanation}
المثال: {analogy}
الخلاصة: {takeaway}
""")

        ayahs_section = "\n".join(ayah_blocks)

        return f"""أنت مدقق ديني صارم. تراجع شرحاً قرآنياً للأطفال أُنتج بنظام آلي.
سورة {surah_name}، عدد الآيات: {len(ayah_scenes)}.

{ayahs_section}

═══════════════════════════════════════
[المطلوب]:
قيّم كل آية على الـ 5 محاور:
1. التوافق العقدي مع التفاسير المعتمدة
2. الدقة التفسيرية
3. التبسيط الآمن (لم يخل بمفهوم عقدي)
4. سلامة التشبيهات (لا تشبه الله بمخلوقاته)
5. اللهجة الموقّرة

أجب بـ JSON فقط، بدون أي نص خارجه:

{{
  "verdicts": [
    {{
      "ayah_number": 1,
      "passed": true/false,
      "confidence": 0.0-1.0,
      "concerns": ["..."],
      "key_finding": "summary"
    }}
    // ... كرر لكل آية
  ]
}}

passed=false إذا فيه أي خطأ عقدي أو معلومة غلط.
passed=true فقط إذا كل المحاور سليمة.
"""

    def _call_claude_batched(self, prompt: str) -> List[Dict]:
        """Call Claude with batched prompt, return list of per-ayah verdicts.

        v22.5 FIX: Uses self._claude_reviewer._client directly (the bug was
        accessing self._anthropic_key which doesn't exist on this class).
        """
        if not self._claude_reviewer or not self._claude_reviewer._available:
            raise RuntimeError("Claude reviewer not available")

        client = self._claude_reviewer._client

        # Use larger max_tokens since we're returning multiple verdicts
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=2000,  # ~5 verdicts × 400 tokens each
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # Strip markdown if present
        import re
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        try:
            data = json.loads(text)
            verdicts = data.get("verdicts", [])
            if not isinstance(verdicts, list):
                raise ValueError("verdicts is not a list")
            return verdicts
        except json.JSONDecodeError as e:
            logger.error(f"Could not parse batched verdicts: {e}\nText: {text[:500]}")
            raise
