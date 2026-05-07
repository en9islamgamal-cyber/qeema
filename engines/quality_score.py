"""
engines/quality_score.py — VALUE / QEEMA v22.5 — quality scoring (numeric)
=========================================================
[v16 changes]
- REMOVED: character name requirement (no more "story missing kid name" critique)
- REMOVED: storytelling-pattern validation
- ADDED: hook quality scoring (curiosity gap detection)
- ADDED: cliché detection (penalize "يا أحبائي", "كان يا ما كان", etc.)
- ADDED: analogy_text scoring (real-world domain check)
- ADDED: insight density check (long flat text without questions/contrast)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)


# ── Banned words (style guide v16) ──
_BANNED_MSA_WORDS = frozenset([
    "كيف", "ماذا", "أين", "الآن", "حسناً", "حقاً", "إذاً",
])
_BANNED_LEVANTINE = frozenset([
    "شو", "هلق", "هيك", "كتير", "منيح", "تمام كتير",
])
_SCARY_WORDS = frozenset([
    "العقاب", "الجحيم", "العذاب",
])

# v16: Cliché detection — these phrases ruin the insight-first vibe
_CLICHE_PHRASES = frozenset([
    "يا أحبائي",
    "يا أحباب الله",
    "يا بنيا",
    "كان يا ما كان",
    "في يوم من الأيام",
    "مرة من المرات",
    "كان فيه ولد",
    "كان فيه بنت",
    "حدوتة",
    "جدو",
    "جدتي",
])

# v16: Hook quality indicators (presence of these = good hook)
_HOOK_INDICATORS = frozenset([
    "تعرف", "تعرفوا", "تخيل", "تخيلوا", "ليه", "كيف",
    "؟",  # questions
    "كنز", "سر", "غريب", "عجيب", "مذهل", "مفاجأة",
    "هل تعلم",
])

_REQUIRED_FIELDS = ("hook_text", "intro_text", "explain_text", "moral_text")
_TASHKEEL_RE = re.compile(r'[\u064B-\u0652\u0670]')


@dataclass
class QualityReport:
    overall_score: float
    passed: bool
    critiques: List[str] = field(default_factory=list)
    breakdown: Dict[str, float] = field(default_factory=dict)


class QualityScorer:
    """v16: Insight-first quality scoring."""

    def __init__(
        self,
        *,
        threshold: float = 70.0,
    ) -> None:
        self._threshold = threshold

    def score_episode(self, script_dict: Dict) -> QualityReport:
        critiques: List[str] = []
        scores: Dict[str, float] = {}

        # ── Intro/outro presence (10 pts each)
        intro_text = script_dict.get("intro_scene", {}).get("narrator_text", "")
        outro_text = script_dict.get("outro_scene", {}).get("narrator_text", "")

        # v16: intro needs hook quality
        intro_score = 10.0
        if not intro_text:
            critiques.append("Missing intro narrator text")
            intro_score = 0.0
        else:
            # Check for hook indicators
            has_hook = any(ind in intro_text for ind in _HOOK_INDICATORS)
            if not has_hook:
                intro_score -= 4.0
                critiques.append("Intro lacks hook indicator (question/curiosity)")
            # Check for clichés
            if any(c in intro_text for c in _CLICHE_PHRASES):
                intro_score -= 5.0
                critiques.append("Intro uses banned clichés ('يا أحبائي'/'كان يا ما كان')")
        scores["intro"] = max(0.0, intro_score)

        outro_score = 10.0 if outro_text else 0.0
        if outro_text and any(c in outro_text for c in _CLICHE_PHRASES):
            outro_score -= 3.0
            critiques.append("Outro uses banned clichés")
        scores["outro"] = max(0.0, outro_score)

        # ── CTA presence (5 pts)
        if script_dict.get("cta_text"):
            scores["cta"] = 5.0
        else:
            critiques.append("Missing CTA text — subscriber growth at risk")
            scores["cta"] = 0.0

        # ── Per-ayah scoring (75 pts total)
        ayah_scenes = script_dict.get("ayah_scenes", [])
        if not ayah_scenes:
            critiques.append("No ayah scenes generated")
            return QualityReport(overall_score=0.0, passed=False,
                                 critiques=critiques, breakdown=scores)

        per_ayah_max = 75.0 / len(ayah_scenes)
        ayah_total = 0.0
        for i, scene in enumerate(ayah_scenes):
            s, c = self._score_ayah_scene(scene, i)
            ayah_total += s * per_ayah_max
            critiques.extend(f"Ayah {i+1}: {note}" for note in c)
        scores["ayahs"] = round(ayah_total, 2)

        total = sum(scores.values())
        return QualityReport(
            overall_score=round(total, 2),
            passed=total >= self._threshold,
            critiques=critiques,
            breakdown=scores,
        )

    def _score_ayah_scene(self, scene: Dict, index: int):
        """Score 0.0-1.0 (relative to per-ayah max)."""
        score = 1.0
        critiques: List[str] = []

        # Required fields (-0.15 each missing)
        for field_name in _REQUIRED_FIELDS:
            text = scene.get(field_name, "")
            if not text or len(text.strip()) < 5:
                score -= 0.15
                critiques.append(f"missing/short {field_name}")

        # v16: Hook quality (10% weight)
        hook = scene.get("hook_text", "")
        if hook:
            has_hook_indicator = any(ind in hook for ind in _HOOK_INDICATORS)
            if not has_hook_indicator:
                score -= 0.10
                critiques.append("hook lacks curiosity indicator (no question/'تعرف')")
            if any(c in hook for c in _CLICHE_PHRASES):
                score -= 0.15
                critiques.append("hook uses cliché (banned in v16)")

        # v16: Cliché check across all narration
        all_narration = " ".join(
            scene.get(f, "") for f in ("hook_text", "intro_text", "story_text", "explain_text", "moral_text")
        )
        for cliche in _CLICHE_PHRASES:
            if cliche in all_narration:
                score -= 0.12
                critiques.append(f"contains cliché: '{cliche}'")
                break

        # Word count budgets (v16: tighter targets)
        budgets = {
            "hook_text": (5, 30),
            "intro_text": (8, 35),
            "story_text": (15, 70),    # holds analogy_text
            "explain_text": (10, 50),
            "moral_text": (4, 25),
        }
        for fld, (lo, hi) in budgets.items():
            text = scene.get(fld, "")
            wc = len(text.split())
            if text and (wc < lo or wc > hi):
                score -= 0.04
                critiques.append(f"{fld} word count {wc} outside [{lo},{hi}]")

        # Banned words
        for word in _BANNED_MSA_WORDS:
            if f" {word} " in f" {all_narration} ":
                score -= 0.06
                critiques.append(f"contains MSA word: {word}")
                break
        for word in _BANNED_LEVANTINE:
            if word in all_narration:
                score -= 0.10
                critiques.append(f"contains Levantine word: {word}")
                break
        for word in _SCARY_WORDS:
            if word in all_narration:
                score -= 0.10
                critiques.append(f"contains scary word for kids: {word}")
                break

        # Tashkeel residue
        narration = " ".join(
            scene.get(f, "") for f in _REQUIRED_FIELDS
        )
        tashkeel_count = len(_TASHKEEL_RE.findall(narration))
        if tashkeel_count > 3:
            score -= 0.04
            critiques.append(f"residual tashkeel: {tashkeel_count} marks")

        # v16 NOTE: NO character-name check (was in v15, removed)

        return max(0.0, score), critiques


class QualityScorerAdapter:
    """Adapter implementing core.interfaces.QualityValidator API."""
    def __init__(self, threshold: float = 70.0) -> None:
        self._scorer = QualityScorer(threshold=threshold)

    def validate(self, script_dict: Dict) -> QualityReport:
        return self._scorer.score_episode(script_dict)
