"""
engines/age_appropriateness.py — VALUE / QEEMA v22.2 (NEW)
=========================================================================
Pedagogical age-appropriateness check for tafsir explanations.

[Why this exists]
TafsirValidator checks RELIGIOUS accuracy (does this match Tafsir Saadi?)
but NOT pedagogical fit (is this appropriate for a 6-year-old to hear?).

A tafsir explanation can be 100% authentic but:
  - Use abstract concepts beyond a child's grasp (e.g., نفاق, فطرة)
  - Reference graphic punishment imagery (hellfire descriptions)
  - Discuss adult topics (marriage, jihad, slavery)
  - Use complex Quranic vocabulary without explanation

This module flags those issues so the script can be revised.

[Approach]
Heuristic rule-based — no LLM call needed (saves cost). Pattern matching
on:
  - Heavy theological terms requiring prior knowledge
  - Graphic/scary imagery (punishment, death, blood)
  - Adult topics
  - Complex sentence structure (>20 words = above grade-3 reading level)
  - Negative emotional framing (fear, guilt, threats)

[Output]
AgeAppropriatenessReport — categorized issues with severity:
  - CRITICAL: must revise (e.g., graphic imagery)
  - WARNING:  should consider revising (complex term unexplained)
  - INFO:     stylistic note (could simplify)

[Integration]
Called after tafsir validation in orchestrator. Logs warnings but doesn't
block the pipeline (kids' content quality is iterative — flag for review).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class AgeIssue:
    severity: Severity
    category: str
    text_field: str        # e.g., "scene 2.explain_text"
    pattern: str           # what was found
    suggestion: str = ""

    def __str__(self) -> str:
        s = f"[{self.severity.value.upper()}] {self.category}: '{self.pattern}' in {self.text_field}"
        if self.suggestion:
            s += f"\n  💡 {self.suggestion}"
        return s


@dataclass
class AgeAppropriatenessReport:
    issues: List[AgeIssue] = field(default_factory=list)
    word_count: int = 0
    avg_sentence_length: float = 0.0
    target_age: str = "6-12"

    @property
    def has_critical(self) -> bool:
        return any(i.severity == Severity.CRITICAL for i in self.issues)

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    def by_severity(self, severity: Severity) -> List[AgeIssue]:
        return [i for i in self.issues if i.severity == severity]

    def summary(self) -> str:
        if not self.has_issues:
            return (
                f"✅ Age-appropriateness check passed "
                f"(target: {self.target_age}, "
                f"avg sentence: {self.avg_sentence_length:.1f} words)"
            )
        lines = [
            f"📋 Age-Appropriateness Report (target: {self.target_age}):"
        ]
        for sev in [Severity.CRITICAL, Severity.WARNING, Severity.INFO]:
            issues = self.by_severity(sev)
            if issues:
                emoji = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}[
                    sev.value
                ]
                lines.append(f"  {emoji} {sev.value.upper()}: {len(issues)}")
                for issue in issues[:3]:  # show top 3
                    lines.append(f"     • {issue.pattern} ({issue.text_field})")
        lines.append(
            f"  📊 Avg sentence: {self.avg_sentence_length:.1f} words"
        )
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Pattern dictionaries
# ════════════════════════════════════════════════════════════════

# CRITICAL: graphic/scary imagery for kids
GRAPHIC_PATTERNS = [
    (r'\bعذاب\s+شديد\b', "use 'النتيجة' or 'العاقبة' instead"),
    (r'\bحريق\s+جهنم\b', "skip — too graphic for children"),
    (r'\bالنار\s+الكبرى\b', "skip — too graphic"),
    (r'\bسلاسل\b', "skip — graphic punishment"),
    (r'\bأغلال\b', "skip — graphic punishment"),
    (r'\bدماء\b', "use 'إصابة' if needed"),
    (r'\bقتل\b(?!ا)', "use 'فقد حياته' or 'مات'"),
    (r'\bذبح\b', "rephrase entirely"),
]

# CRITICAL: adult topics inappropriate for kids 6-12
ADULT_TOPICS = [
    (r'\bالنكاح\b', "skip — adult topic"),
    (r'\bالطلاق\b', "skip — complex topic"),
    (r'\bالسبي\b', "skip — historical violence"),
    (r'\bالعبيد\b(?!ا)', "skip or use 'الناس' generically"),
    (r'\bالفروج\b', "skip — adult topic"),
    (r'\bالنفاس\b', "skip — biology beyond age"),
]

# WARNING: complex theological terms needing explanation
COMPLEX_THEOLOGY = {
    "نفاق": "use 'كذب على نفسه ' أو 'تظاهر بحاجة مش حقيقية'",
    "فطرة": "use 'الطبيعة الأصلية' أو 'اللي ربنا خلقنا عليه'",
    "تقوى": "use 'إن ربنا شايفنا في كل حاجة'",
    "إخلاص": "use 'تعمل الخير علشان ربنا فقط'",
    "خشوع": "use 'تركيز كامل في الصلاة'",
    "ورع": "use 'حذر من الحرام'",
    "زهد": "use 'مش متعلق بالدنيا أوي'",
    "يقين": "use 'إيمان قوي'",
    "تدبر": "use 'تفكر بهدوء'",
    "اضطرار": "use 'محتاج جداً ومحدش معاه حل'",
    "اصطفاء": "use 'ربنا اختاره علشان كذا'",
}

# WARNING: negative emotional framing
NEGATIVE_FRAMING = [
    (r'\bستذهب\s+إلى\s+النار\b', "reframe positively: 'الصواب يقربك من ربنا'"),
    (r'\bخائف\s+من\s+الله\b', "use 'محب لربنا' أو 'محترم لربنا'"),
    (r'\bلا\s+تكن\s+مثل\b', "use 'كن مثل' (positive framing)"),
    (r'\bالمصير\s+المحتوم\b', "skip — fatalistic phrasing"),
    (r'\bالعذاب\s+الأليم\b', "use 'النتيجة الصعبة'"),
]

# INFO: stylistic notes (could be simpler)
STYLISTIC = [
    (r'\bإنّ\s', "uses MSA particle 'إنّ' — could be simpler"),
    (r'\bذلك\b', "consider 'ده' (Egyptian)"),
    (r'\bأولئك\b', "consider 'هما دول' (Egyptian)"),
    (r'\bلكنّه\b', "consider 'بس' (Egyptian)"),
]


# ════════════════════════════════════════════════════════════════
# AgeAppropriatenessChecker
# ════════════════════════════════════════════════════════════════
class AgeAppropriatenessChecker:
    """Heuristic checker for kids' content (ages 6-12)."""

    MAX_SENTENCE_FOR_AGE = 18  # words per sentence

    @classmethod
    def check_script(
        cls, script_data: Dict[str, Any],
    ) -> AgeAppropriatenessReport:
        """Check a full script dict. Returns report."""
        report = AgeAppropriatenessReport()

        # Collect all texts to check, with their source labels
        text_sources = cls._collect_texts(script_data)

        for text, source in text_sources:
            cls._check_text(text, source, report)

        # Compute statistics
        all_text = " ".join(t for t, _ in text_sources)
        report.word_count = len(all_text.split())
        report.avg_sentence_length = cls._avg_sentence_length(all_text)

        # If sentences too long on average, add a warning
        if report.avg_sentence_length > cls.MAX_SENTENCE_FOR_AGE:
            report.issues.append(AgeIssue(
                severity=Severity.WARNING,
                category="readability",
                text_field="overall",
                pattern=f"avg {report.avg_sentence_length:.1f} words/sentence",
                suggestion=f"Target ≤ {cls.MAX_SENTENCE_FOR_AGE} for ages 6-12",
            ))

        return report

    @staticmethod
    def _collect_texts(data: Dict[str, Any]) -> List[tuple]:
        """Collect (text, source_label) pairs."""
        items = []
        for f in ["intro_text", "outro_text", "cta_text"]:
            if data.get(f):
                items.append((data[f], f))

        for i, scene in enumerate(data.get("ayah_scenes", [])):
            sid = f"scene{i+1}"
            for f in ["hook_text", "intro_text", "analogy_text",
                      "explain_text", "moral_text"]:
                if scene.get(f):
                    items.append((scene[f], f"{sid}.{f}"))
        return items

    @classmethod
    def _check_text(
        cls, text: str, source: str, report: AgeAppropriatenessReport,
    ) -> None:
        """Run all pattern checks on a single text."""
        # CRITICAL: graphic
        for pattern, suggestion in GRAPHIC_PATTERNS:
            if re.search(pattern, text):
                report.issues.append(AgeIssue(
                    severity=Severity.CRITICAL,
                    category="graphic_imagery",
                    text_field=source,
                    pattern=re.search(pattern, text).group(0),
                    suggestion=suggestion,
                ))

        # CRITICAL: adult topics
        for pattern, suggestion in ADULT_TOPICS:
            if re.search(pattern, text):
                report.issues.append(AgeIssue(
                    severity=Severity.CRITICAL,
                    category="adult_topic",
                    text_field=source,
                    pattern=re.search(pattern, text).group(0),
                    suggestion=suggestion,
                ))

        # WARNING: complex theology
        for term, suggestion in COMPLEX_THEOLOGY.items():
            if term in text:
                # Check if explained nearby (rough heuristic: within 30 chars)
                idx = text.find(term)
                context = text[max(0, idx-30):min(len(text), idx+50)]
                # If there's an explanation marker like "يعني" or "أي" nearby
                if not any(
                    marker in context
                    for marker in ["يعني", "أي ", "بمعنى", "ده معناه"]
                ):
                    report.issues.append(AgeIssue(
                        severity=Severity.WARNING,
                        category="unexplained_term",
                        text_field=source,
                        pattern=term,
                        suggestion=suggestion,
                    ))

        # WARNING: negative framing
        for pattern, suggestion in NEGATIVE_FRAMING:
            if re.search(pattern, text):
                report.issues.append(AgeIssue(
                    severity=Severity.WARNING,
                    category="negative_framing",
                    text_field=source,
                    pattern=re.search(pattern, text).group(0),
                    suggestion=suggestion,
                ))

        # INFO: stylistic
        for pattern, suggestion in STYLISTIC:
            if re.search(pattern, text):
                report.issues.append(AgeIssue(
                    severity=Severity.INFO,
                    category="style",
                    text_field=source,
                    pattern=re.search(pattern, text).group(0),
                    suggestion=suggestion,
                ))

    @staticmethod
    def _avg_sentence_length(text: str) -> float:
        """Average words per sentence."""
        sentences = [s.strip() for s in re.split(r'[.!?؟]+', text) if s.strip()]
        if not sentences:
            return 0.0
        return sum(len(s.split()) for s in sentences) / len(sentences)


def check_age_appropriateness(
    script_data: Dict[str, Any],
    *,
    log_report: bool = True,
) -> AgeAppropriatenessReport:
    """Public convenience wrapper."""
    report = AgeAppropriatenessChecker.check_script(script_data)
    if log_report:
        if report.has_critical:
            logger.error(report.summary())
        elif report.has_issues:
            logger.warning(report.summary())
        else:
            logger.info(report.summary())
    return report
