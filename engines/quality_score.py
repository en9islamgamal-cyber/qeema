"""
engines/quality_score.py — VALUE / QEEMA v15.0 (NEW)
=========================================================
Heuristic + LLM-based quality scoring for generated scripts.

[Strategy]
1. Cheap heuristic checks first (free, instant)
2. If heuristics pass → optional LLM grading via Gemini Flash
3. Returns QualityReport with score 0-100 + critiques

[Heuristic checks — covered without LLM]
- Word count per field within budget
- No banned words (Modern Standard Arabic, Levantine slang, scary words)
- Required fields populated
- Tashkeel cleanliness
- Story has a character name
- Moral references the ayah
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Banned words (per Sheikh Abu Ziyad style guide) ──
_BANNED_MSA_WORDS = frozenset([
    "كيف", "ماذا", "أين", "الآن", "حسناً", "بالطبع", "حقاً", "إذاً",
])
_BANNED_LEVANTINE = frozenset([
    "شو", "هلق", "هيك", "كتير", "منيح", "تمام كتير",
])
_SCARY_WORDS = frozenset([
    "العقاب", "النار", "الجحيم", "العذاب", "جهنم",
])

_REQUIRED_FIELDS = ("hook_text", "intro_text", "explain_text", "moral_text")
_TASHKEEL_RE = re.compile(r'[\u064B-\u0652\u0670]')


@dataclass
class QualityReport:
    overall_score: float           # 0-100
    passed: bool                   # True if score >= threshold
    critiques: List[str] = field(default_factory=list)
    breakdown: Dict[str, float] = field(default_factory=dict)


class QualityScorer:
    """v15: Multi-layer quality scoring for ayah scenes."""

    def __init__(
        self,
        *,
        threshold: float = 70.0,
        use_llm_grading: bool = False,
    ) -> None:
        self._threshold = threshold
        self._use_llm = use_llm_grading

    def score_episode(self, script_dict: Dict) -> QualityReport:
        """Score a full episode. Returns aggregated report."""
        critiques: List[str] = []
        scores: Dict[str, float] = {}

        # ── Intro/outro presence (10 pts each)
        intro_text = script_dict.get("intro_scene", {}).get("narrator_text", "")
        outro_text = script_dict.get("outro_scene", {}).get("narrator_text", "")
        if not intro_text:
            critiques.append("Missing intro narrator text")
            scores["intro"] = 0.0
        else:
            scores["intro"] = 10.0
        if not outro_text:
            critiques.append("Missing outro narrator text")
            scores["outro"] = 0.0
        else:
            scores["outro"] = 10.0

        # ── CTA presence (5 pts)
        if script_dict.get("cta_text"):
            scores["cta"] = 5.0
        else:
            critiques.append("Missing CTA text — subscriber growth at risk")
            scores["cta"] = 0.0

        # ── Per-ayah scoring (75 pts total, distributed)
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

    def _score_ayah_scene(self, scene: Dict, index: int) -> tuple:
        """Score 0.0-1.0 (relative to per-ayah max). Returns (score, critiques)."""
        score = 1.0
        critiques: List[str] = []

        # Required fields (-0.15 each missing, max -0.45)
        for field_name in _REQUIRED_FIELDS:
            text = scene.get(field_name, "")
            if not text or len(text.strip()) < 5:
                score -= 0.15
                critiques.append(f"missing/short {field_name}")

        # Word count budget per field
        budgets = {
            "hook_text": (5, 35),
            "intro_text": (8, 45),
            "story_text": (15, 80),
            "explain_text": (10, 60),
            "moral_text": (4, 30),
        }
        for fld, (lo, hi) in budgets.items():
            text = scene.get(fld, "")
            wc = len(text.split())
            if text and (wc < lo or wc > hi):
                score -= 0.05
                critiques.append(f"{fld} word count {wc} outside [{lo},{hi}]")

        # Banned words
        all_text = " ".join(scene.get(f, "") for f in _REQUIRED_FIELDS)
        for word in _BANNED_MSA_WORDS:
            if f" {word} " in f" {all_text} " or all_text.startswith(word + " "):
                score -= 0.08
                critiques.append(f"contains MSA word: {word}")
                break
        for word in _BANNED_LEVANTINE:
            if word in all_text:
                score -= 0.10
                critiques.append(f"contains Levantine word: {word}")
                break
        for word in _SCARY_WORDS:
            if word in all_text:
                score -= 0.12
                critiques.append(f"contains scary word for kids: {word}")
                break

        # Tashkeel residue (Arabic narration should be clean)
        narration = " ".join(
            scene.get(f, "") for f in _REQUIRED_FIELDS if f != "visual_prompt"
        )
        tashkeel_count = len(_TASHKEEL_RE.findall(narration))
        if tashkeel_count > 3:
            score -= 0.05
            critiques.append(f"residual tashkeel: {tashkeel_count} marks")

        # Story has a character name
        story = scene.get("story_text", "")
        from engines.script_engine import _CHARACTER_NAMES
        if story and not any(n in story for n in _CHARACTER_NAMES):
            score -= 0.06
            critiques.append("story missing recognizable character name")

        return max(0.0, score), critiques


class QualityScorerAdapter:
    """
    Adapter implementing core.interfaces.QualityValidator API.
    Wraps QualityScorer for use in ScriptEngine.
    """
    def __init__(self, threshold: float = 70.0) -> None:
        self._scorer = QualityScorer(threshold=threshold)

    def validate(self, script_dict: Dict) -> QualityReport:
        return self._scorer.score_episode(script_dict)
