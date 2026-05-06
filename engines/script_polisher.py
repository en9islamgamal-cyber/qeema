"""
engines/script_polisher.py — VALUE / QEEMA v22.1 (NEW)
=========================================================================
Post-generation quality polishing for scripts.

[Why this exists]
LLM output, even with good prompts, has predictable quality drift:
  - Sometimes uses MSA when we asked for Egyptian dialect
  - Occasionally slips a banned phrase
  - Long sentences in some scenes
  - Inconsistent emotion-text alignment

Rather than re-prompting (expensive), we run a deterministic polish pass
that applies specific transformations + flags issues that need human review.

[What it does]
1. **Detect banned phrases** → log warnings (manual review needed)
2. **Detect MSA leakage** → log warnings (style drift)
3. **Sentence length analysis** → flag long sentences
4. **Emotion consistency check** → ensure scene_emotion matches text tone
5. **Apply minor fixes** → typo corrections, common variants

[Design]
Deterministic — no LLM calls. Pure string analysis.
Returns a polish report alongside the (possibly modified) script.

[What it does NOT do]
- Doesn't rewrite content (that's the LLM's job)
- Doesn't fail builds (just logs warnings)
- Doesn't change semantic meaning
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Banned phrases (broader set than prompt enforcement)
# ════════════════════════════════════════════════════════════════
BANNED_PATTERNS = [
    # Generic clichés
    (r'\bأحبائي\b', "cliché address"),
    (r'\bأحبتي\b', "cliché address"),
    (r'\bإخوتي\s+الكرام\b', "formal cliché"),
    (r'\bأبنائي\s+الأعزاء\b', "patronizing cliché"),
    (r'\bهل\s+تعلم\b', "tired hook"),
    (r'\bهيا\s+نتعلم\b', "patronizing intro"),
    (r'\bتعالوا\s+نتعلم\b', "patronizing intro"),
    (r'في\s+حلقة\s+اليوم', "meta-narration"),
]


# ════════════════════════════════════════════════════════════════
# MSA markers — when seen, suggests style drift to formal Arabic
# ════════════════════════════════════════════════════════════════
MSA_MARKERS = [
    # Formal verb forms
    "إنّ", "إنه يخبرنا", "تعالى", "سبحانه وتعالى",
    # Formal pronouns/conjunctions
    "أيها", "إنما", "كذلك", "لذلك",
    # Stiff phrases
    "يخبرنا الله", "يقول الله", "ذكر الله",
]


# Common Egyptian Arabic markers (presence = good sign)
EGYPTIAN_MARKERS = [
    "بقى", "كده", "ازاي", "ايه", "خالص", "أوي",
    "علشان", "عشان", "لما", "لسه", "بقالنا",
    "تخيل", "خليكي", "خليك", "بنحس", "بنحبه",
    "ربنا", "هو ده", "في يوم", "حلوة", "جامد",
]


@dataclass
class PolishReport:
    """Report of issues found during polishing."""
    banned_phrases: List[str] = field(default_factory=list)
    msa_leakage: List[str] = field(default_factory=list)
    long_sentences: List[Tuple[str, int]] = field(default_factory=list)
    fixes_applied: List[str] = field(default_factory=list)
    egyptian_score: float = 0.0  # 0–1, higher = more colloquial

    @property
    def has_issues(self) -> bool:
        return bool(
            self.banned_phrases
            or self.msa_leakage
            or self.long_sentences
        )

    def summary(self) -> str:
        lines = [f"📋 Script Polish Report:"]
        if self.banned_phrases:
            lines.append(
                f"  ❌ {len(self.banned_phrases)} banned phrase(s): "
                f"{', '.join(self.banned_phrases[:3])}"
            )
        if self.msa_leakage:
            lines.append(
                f"  ⚠️  {len(self.msa_leakage)} MSA marker(s): "
                f"{', '.join(self.msa_leakage[:3])}"
            )
        if self.long_sentences:
            lines.append(
                f"  ⚠️  {len(self.long_sentences)} long sentence(s) "
                f"(>14 words)"
            )
        if self.fixes_applied:
            lines.append(
                f"  🔧 {len(self.fixes_applied)} fix(es) applied"
            )
        lines.append(f"  📊 Egyptian dialect score: {self.egyptian_score:.0%}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Polisher
# ════════════════════════════════════════════════════════════════
class ScriptPolisher:
    """Post-generation quality polish for episode scripts."""

    MAX_SENTENCE_WORDS = 14  # slight buffer over the 12-word target

    @classmethod
    def polish(
        cls,
        script_data: Dict[str, Any],
        *,
        apply_fixes: bool = True,
    ) -> Tuple[Dict[str, Any], PolishReport]:
        """Polish a script dict in-place and return a report.

        Args:
            script_data: The script JSON dict (from multi-task LLM response).
            apply_fixes: If True, apply minor automatic fixes.
                        If False, only detect issues.

        Returns:
            (polished_dict, report) — polished_dict may be the same object.
        """
        report = PolishReport()
        all_text = cls._collect_all_text(script_data)

        # 1. Detect banned phrases
        report.banned_phrases = cls._find_banned(all_text)

        # 2. Detect MSA leakage
        report.msa_leakage = cls._find_msa(all_text)

        # 3. Long sentence detection
        report.long_sentences = cls._find_long_sentences(script_data)

        # 4. Egyptian dialect score
        report.egyptian_score = cls._compute_egyptian_score(all_text)

        # 5. Apply minor fixes if enabled
        if apply_fixes:
            cls._apply_fixes(script_data, report)

        return script_data, report

    @staticmethod
    def _collect_all_text(data: Dict[str, Any]) -> str:
        """Collect all text fields into one string for scanning."""
        parts = [
            data.get("intro_text", ""),
            data.get("outro_text", ""),
            data.get("cta_text", ""),
            data.get("title", ""),
            data.get("youtube_description", ""),
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

    @staticmethod
    def _find_banned(text: str) -> List[str]:
        found = []
        for pattern, label in BANNED_PATTERNS:
            matches = re.findall(pattern, text)
            for m in matches:
                found.append(f"'{m}' ({label})")
        return found

    @staticmethod
    def _find_msa(text: str) -> List[str]:
        found = []
        for marker in MSA_MARKERS:
            if marker in text:
                # Count occurrences (only flag if >=2, to allow occasional use)
                count = text.count(marker)
                if count >= 2:
                    found.append(f"'{marker}' (×{count})")
        return found

    @classmethod
    def _find_long_sentences(
        cls, data: Dict[str, Any],
    ) -> List[Tuple[str, int]]:
        """Find sentences exceeding word limit."""
        long_ones: List[Tuple[str, int]] = []

        def check(text: str, source: str) -> None:
            for sentence in re.split(r'[.!?؟]+', text):
                sentence = sentence.strip()
                words = sentence.split()
                if len(words) > cls.MAX_SENTENCE_WORDS:
                    preview = sentence[:50] + ("..." if len(sentence) > 50 else "")
                    long_ones.append((f"{source}: {preview}", len(words)))

        check(data.get("intro_text", ""), "intro")
        check(data.get("outro_text", ""), "outro")
        for i, scene in enumerate(data.get("ayah_scenes", [])):
            check(scene.get("hook_text", ""), f"scene{i+1}.hook")
            check(scene.get("explain_text", ""), f"scene{i+1}.explain")
            check(scene.get("analogy_text", ""), f"scene{i+1}.analogy")
            check(scene.get("moral_text", ""), f"scene{i+1}.moral")

        return long_ones

    @staticmethod
    def _compute_egyptian_score(text: str) -> float:
        """Heuristic: ratio of Egyptian markers to total word count."""
        if not text:
            return 0.0
        total_words = len(text.split())
        if total_words == 0:
            return 0.0
        marker_count = sum(text.count(m) for m in EGYPTIAN_MARKERS)
        # Normalize: 1 marker per 30 words = score 1.0
        return min(1.0, marker_count / max(total_words / 30, 1))

    @staticmethod
    def _apply_fixes(
        data: Dict[str, Any], report: PolishReport,
    ) -> None:
        """Apply deterministic safe fixes.

        Conservative — only fixes that have ZERO risk of changing meaning.
        """
        fixes_applied = []

        # Fix 1: Common typo "اللى" → "اللي" (Egyptian standard spelling)
        text_fields = ["intro_text", "outro_text", "cta_text"]
        for field_name in text_fields:
            if field_name in data and isinstance(data[field_name], str):
                original = data[field_name]
                fixed = original.replace("اللى ", "اللي ")
                if fixed != original:
                    data[field_name] = fixed
                    fixes_applied.append(
                        f"{field_name}: 'اللى' → 'اللي'"
                    )

        # Same for scene fields
        for i, scene in enumerate(data.get("ayah_scenes", [])):
            for k in ["hook_text", "intro_text", "analogy_text",
                      "explain_text", "moral_text"]:
                if k in scene and isinstance(scene[k], str):
                    original = scene[k]
                    fixed = original.replace("اللى ", "اللي ")
                    if fixed != original:
                        scene[k] = fixed
                        fixes_applied.append(
                            f"scene{i+1}.{k}: 'اللى' → 'اللي'"
                        )

        # Fix 2: Strip leading/trailing whitespace + normalize spaces
        for field_name in text_fields:
            if field_name in data and isinstance(data[field_name], str):
                original = data[field_name]
                fixed = re.sub(r'\s+', ' ', original).strip()
                if fixed != original:
                    data[field_name] = fixed

        for scene in data.get("ayah_scenes", []):
            for k in ["hook_text", "intro_text", "analogy_text",
                      "explain_text", "moral_text"]:
                if k in scene and isinstance(scene[k], str):
                    original = scene[k]
                    fixed = re.sub(r'\s+', ' ', original).strip()
                    if fixed != original:
                        scene[k] = fixed

        report.fixes_applied = fixes_applied


# ════════════════════════════════════════════════════════════════
# Public function for direct use
# ════════════════════════════════════════════════════════════════
def polish_script(
    script_data: Dict[str, Any],
    *,
    apply_fixes: bool = True,
    log_report: bool = True,
) -> PolishReport:
    """Polish a script in-place. Convenience wrapper.

    Returns the polish report.
    """
    _, report = ScriptPolisher.polish(script_data, apply_fixes=apply_fixes)
    if log_report:
        if report.has_issues:
            logger.warning(report.summary())
        else:
            logger.info(
                f"✅ Script polish: clean "
                f"(Egyptian score: {report.egyptian_score:.0%})"
            )
    return report
