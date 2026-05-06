"""
engines/age_appropriate_validator.py — VALUE / QEEMA v23 (NEW)
=========================================================================
Age-appropriateness validator for kid-targeted Quranic content.

[Why this exists]
TafsirValidator (v18) checks RELIGIOUS accuracy — whether the explanation
matches authentic tafsir like Al-Saadi or Al-Muyassar. But it doesn't check
PEDAGOGICAL fitness for ages 6-12.

Examples of religiously-accurate-but-age-inappropriate content:
  - Detailed jurisprudence terminology
  - Eschatological imagery beyond age comprehension
  - Punishment-heavy framing (can frighten young children)
  - Abstract theological concepts without concrete grounding
  - Adult life situations (marriage, divorce, financial law)

[Two modes]
  1. **Heuristic** (default, free, fast)
     Pattern-based detection of red flags. No API call.
     Catches the obvious cases: complex jargon, fear-inducing words.

  2. **Deep Claude review** (optional, costs ~$0.01/episode)
     Sends script to Claude with kid-pedagogy criteria.
     Catches subtle issues heuristic misses.
     Off by default to avoid API costs unless flag set.

[What we check for]
  ✗ Frightening imagery without comfort
  ✗ Complex theological terms (تجلي، تنزيه، صفات الذات...)
  ✗ Adult-context applications
  ✗ Abstract without concrete example
  ✗ Long sentences (>14 words for ages 6-9)

[What we DON'T check]
  - Religious accuracy (TafsirValidator's job)
  - Grammar (not our job)
  - SEO (YouTube titles' concern)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Patterns — terms / topics that need careful framing for kids
# ════════════════════════════════════════════════════════════════

# Terms that scare without context
FRIGHTENING_TERMS = [
    "نار جهنم", "جحيم", "عذاب أليم", "عقاب شديد",
    "غضب الله", "بطش", "انتقام", "هلاك",
    "لعنة", "ملعون",
]

# Adult-context topics
ADULT_TOPICS = [
    "طلاق", "نكاح", "ميراث", "ربا", "زنا", "قتل النفس",
    "الحدود", "القصاص", "الجلد", "الرجم",
]

# Complex theological terms (advanced kalam terminology)
COMPLEX_THEOLOGY = [
    "تجلي", "تنزيه", "صفات الذات", "صفات الفعل",
    "وحدة الوجود", "حلول", "تشبيه", "تعطيل",
    "الجبر", "الاختيار", "القدر المطلق",
    "أسماء وصفات", "خلق الأفعال", "كسب",
]

# Heavy MSA jurisprudence vocabulary
HEAVY_FIQH = [
    "أحكام", "تكليف", "اجتهاد", "إجماع", "قياس",
    "ناسخ ومنسوخ", "محكم ومتشابه",
]


# Words that indicate concrete grounding (good signs!)
CONCRETE_INDICATORS = [
    "تخيل", "زي", "مثل", "زي ما", "كأنه", "كأن",
    "في يوم", "لو", "ايه رأيك", "بتلاحظ",
]


@dataclass
class AgeReport:
    """Results of age-appropriateness check."""
    passed: bool
    confidence: float          # 0–1, higher = more confident in verdict
    issues: List[Dict[str, Any]] = field(default_factory=list)
    concrete_score: float = 0.0  # 0-1, how grounded the content is
    method: str = "heuristic"

    def summary(self) -> str:
        emoji = "✅" if self.passed else "⚠️"
        lines = [
            f"{emoji} Age-appropriate check: "
            f"{'PASS' if self.passed else 'NEEDS REVIEW'} "
            f"(method={self.method}, confidence={self.confidence:.0%})"
        ]
        if self.issues:
            lines.append(f"  Issues found: {len(self.issues)}")
            for issue in self.issues[:5]:
                lines.append(
                    f"    • [{issue['type']}] {issue['detail']}"
                )
        lines.append(
            f"  Concrete grounding score: {self.concrete_score:.0%}"
        )
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Heuristic validator (default, free, fast)
# ════════════════════════════════════════════════════════════════
class AgeAppropriateValidator:
    """Pedagogical fitness checker for ages 6-12.

    Default: heuristic (free).
    Optional: Claude deep review (costs ~$0.01).
    """

    MAX_SENTENCE_WORDS = 14

    def __init__(
        self,
        *,
        anthropic_client: Any = None,
        enable_claude_review: bool = False,
        target_age_min: int = 6,
        target_age_max: int = 12,
    ) -> None:
        self._claude = anthropic_client
        self._enable_claude = enable_claude_review
        self._age_min = target_age_min
        self._age_max = target_age_max

    def validate(self, script_data: Dict[str, Any]) -> AgeReport:
        """Run heuristic check. Optionally adds Claude review.

        Returns:
            AgeReport with issues list + verdict.
        """
        # Always run heuristic first (cheap)
        report = self._heuristic_check(script_data)

        # Optionally enhance with Claude
        if self._enable_claude and self._claude is not None:
            try:
                claude_report = self._claude_check(script_data)
                report = self._merge_reports(report, claude_report)
            except Exception as e:
                logger.warning(
                    f"⚠️ Claude age-check failed ({e}), using heuristic only"
                )

        return report

    # ─── Heuristic check ────────────────────────────────────────
    def _heuristic_check(self, data: Dict[str, Any]) -> AgeReport:
        """Pattern-based detection of age-inappropriate content."""
        issues: List[Dict[str, Any]] = []

        # Collect all narrative text
        all_text = self._collect_narrative_text(data)

        # 1. Frightening terms without comfort
        for term in FRIGHTENING_TERMS:
            if term in all_text:
                # Look for comfort markers within 80 chars
                indices = [m.start() for m in re.finditer(re.escape(term), all_text)]
                for idx in indices:
                    window = all_text[max(0, idx-40):idx+len(term)+40]
                    has_comfort = any(
                        marker in window
                        for marker in ["رحمة", "محبة", "تيجي ربنا", "غفور", "حنين"]
                    )
                    if not has_comfort:
                        issues.append({
                            "type": "frightening_no_comfort",
                            "detail": f"'{term}' without comforting frame",
                            "severity": "high",
                        })
                        break  # one issue per term type

        # 2. Adult topics
        for topic in ADULT_TOPICS:
            if topic in all_text:
                issues.append({
                    "type": "adult_topic",
                    "detail": f"adult-context: '{topic}'",
                    "severity": "high",
                })

        # 3. Complex theology
        complex_count = sum(1 for t in COMPLEX_THEOLOGY if t in all_text)
        if complex_count >= 1:
            found = [t for t in COMPLEX_THEOLOGY if t in all_text][:3]
            issues.append({
                "type": "complex_theology",
                "detail": f"complex term(s): {', '.join(found)}",
                "severity": "medium",
            })

        # 4. Heavy fiqh terminology
        fiqh_count = sum(1 for t in HEAVY_FIQH if t in all_text)
        if fiqh_count >= 2:
            issues.append({
                "type": "heavy_fiqh",
                "detail": f"{fiqh_count} fiqh terms — too academic",
                "severity": "medium",
            })

        # 5. Long sentences (re-check from polish dimension)
        long_count = self._count_long_sentences(all_text)
        if long_count > 3:
            issues.append({
                "type": "long_sentences",
                "detail": f"{long_count} sentences >{self.MAX_SENTENCE_WORDS} words",
                "severity": "medium",
            })

        # 6. Concrete grounding score
        concrete_score = self._compute_concrete_score(all_text)
        if concrete_score < 0.2:
            issues.append({
                "type": "abstract_without_grounding",
                "detail": f"low concrete grounding ({concrete_score:.0%})",
                "severity": "low",
            })

        # Determine pass/fail
        high_issues = sum(1 for i in issues if i["severity"] == "high")
        passed = high_issues == 0  # any high-severity issue = needs review

        return AgeReport(
            passed=passed,
            confidence=0.7,  # heuristic is moderately confident
            issues=issues,
            concrete_score=concrete_score,
            method="heuristic",
        )

    # ─── Claude deep review (optional) ──────────────────────────
    def _claude_check(self, data: Dict[str, Any]) -> AgeReport:
        """Send script to Claude with pedagogy-focused prompt.

        Costs ~$0.005-0.01 per episode (Haiku model).
        """
        text_summary = self._build_text_summary(data)

        prompt = f"""أنت خبير تربية أطفال متخصص في تعليم الأطفال من سن {self._age_min}-{self._age_max} سنة.

افحص النص التالي وحدد إذا كان مناسب للأطفال في هذا السن:

═══════════════════════════════════════════════════
{text_summary}
═══════════════════════════════════════════════════

معايير الفحص:
1. هل اللغة مفهومة لطفل في هذا السن؟
2. هل الأمثلة ملموسة وقابلة للتخيل؟
3. هل فيه أي محتوى مخيف بدون توازن؟
4. هل الإطار العام إيجابي وتشجيعي؟
5. هل يتجنب المصطلحات المعقدة؟

أجب بـ JSON فقط:
{{
  "verdict": "pass" أو "needs_revision",
  "confidence": 0.0-1.0,
  "concerns": ["مشكلة 1", "مشكلة 2"],
  "praise": ["نقطة قوية 1"]
}}

ابدأ بـ {{ مباشرة."""

        # Call Claude
        message = self._claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()

        # Parse
        import json
        # Strip code fences
        response_text = re.sub(r'^\s*```(?:json)?\s*', '', response_text)
        response_text = re.sub(r'\s*```\s*$', '', response_text)
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            # Try to extract JSON object
            m = re.search(r'\{.*\}', response_text, re.DOTALL)
            if m:
                result = json.loads(m.group(0))
            else:
                raise

        passed = result.get("verdict") == "pass"
        confidence = float(result.get("confidence", 0.5))
        concerns = result.get("concerns", [])

        issues = [
            {"type": "claude_concern", "detail": c, "severity": "medium"}
            for c in concerns
        ]

        return AgeReport(
            passed=passed,
            confidence=confidence,
            issues=issues,
            concrete_score=0.0,  # Claude doesn't compute this
            method="claude",
        )

    @staticmethod
    def _merge_reports(heuristic: AgeReport, claude: AgeReport) -> AgeReport:
        """Combine heuristic + Claude reports — take stricter verdict."""
        return AgeReport(
            passed=heuristic.passed and claude.passed,
            confidence=max(heuristic.confidence, claude.confidence),
            issues=heuristic.issues + claude.issues,
            concrete_score=heuristic.concrete_score,
            method=f"{heuristic.method}+{claude.method}",
        )

    # ─── Helpers ────────────────────────────────────────────────
    @staticmethod
    def _collect_narrative_text(data: Dict[str, Any]) -> str:
        """Collect ONLY narrative text — NOT Quran ayahs."""
        parts = [
            data.get("intro_text", ""),
            data.get("outro_text", ""),
            data.get("cta_text", ""),
        ]
        for scene in data.get("ayah_scenes", []):
            parts.extend([
                scene.get("hook_text", ""),
                scene.get("intro_text", ""),
                scene.get("analogy_text", ""),
                scene.get("explain_text", ""),
                scene.get("moral_text", ""),
            ])
        return " ".join(p for p in parts if p)

    @classmethod
    def _count_long_sentences(cls, text: str) -> int:
        long_count = 0
        for sentence in re.split(r'[.!?؟]+', text):
            sentence = sentence.strip()
            if len(sentence.split()) > cls.MAX_SENTENCE_WORDS:
                long_count += 1
        return long_count

    @staticmethod
    def _compute_concrete_score(text: str) -> float:
        """Ratio of concrete grounding markers to text length.
        
        Uses word-boundary matching to avoid substring false positives
        (e.g., "مثل" inside "أمثلة" should NOT count).
        """
        if not text:
            return 0.0
        word_count = len(text.split())
        if word_count == 0:
            return 0.0
        # Word-boundary regex: marker must be surrounded by whitespace
        # or text edges or punctuation
        marker_count = 0
        for marker in CONCRETE_INDICATORS:
            pattern = r'(?:^|[\s.,!?؟،])' + re.escape(marker) + r'(?:[\s.,!?؟،]|$)'
            marker_count += len(re.findall(pattern, text))
        # 1 marker per 25 words = score 1.0 (good)
        return min(1.0, marker_count * 25 / max(word_count, 1))

    @staticmethod
    def _build_text_summary(data: Dict[str, Any]) -> str:
        """Build a condensed text summary for Claude (token-efficient)."""
        parts = [
            f"Title: {data.get('title', '')}",
            f"Intro: {data.get('intro_text', '')}",
        ]
        for i, scene in enumerate(data.get("ayah_scenes", []), 1):
            parts.extend([
                f"\n--- Scene {i} ---",
                f"Hook: {scene.get('hook_text', '')}",
                f"Analogy: {scene.get('analogy_text', '')}",
                f"Moral: {scene.get('moral_text', '')}",
            ])
        parts.append(f"\nOutro: {data.get('outro_text', '')}")
        return "\n".join(parts)


# ════════════════════════════════════════════════════════════════
# Public function
# ════════════════════════════════════════════════════════════════
def validate_age_appropriate(
    script_data: Dict[str, Any],
    *,
    anthropic_client: Any = None,
    enable_claude_review: bool = False,
    log_report: bool = True,
) -> AgeReport:
    """Convenience wrapper. Returns AgeReport."""
    validator = AgeAppropriateValidator(
        anthropic_client=anthropic_client,
        enable_claude_review=enable_claude_review,
    )
    report = validator.validate(script_data)
    if log_report:
        if not report.passed:
            logger.warning(report.summary())
        else:
            logger.info(report.summary())
    return report
