"""
engines/quality_validator.py — VALUE / QEEMA v11.0 (Production)
=====================================================================
Quality gate for episode scripts.

[Why]
LLMs sometimes return scripts that are:
- Too long (children lose attention)
- Contain forbidden tashkeel artifacts
- Use formal Arabic instead of Egyptian dialect
- Reference forbidden visual styles (Pixar, 3D realistic)

[How]
- Pure rule-based: no LLM-as-a-judge (deterministic, free, fast)
- Returns QualityReport with field-level scores
- Orchestrator decides whether to retry or abort
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from core.interfaces import QualityReport, QualityValidator


# ════════════════════════════════════════════════════════════════
# Rules (constants)
# ════════════════════════════════════════════════════════════════
MAX_NARRATOR_WORDS: int = 35
MAX_INTRO_OUTRO_WORDS: int = 30

# Word-final tashkeel that should have been stripped before TTS
_TASHKEEL_END_RE = re.compile(r"[\u064B-\u0652]+(?=\s|$|[،.؟!])")

# Forbidden visual descriptors (we want flat illustration style)
_FORBIDDEN_VISUAL_RE = re.compile(
    r"\b(pixar|3d|realistic|photograph|photo[- ]?real|complex|hyperrealistic)\b",
    re.IGNORECASE,
)

# Lebanese / formal Arabic markers (we want Egyptian)
_FORBIDDEN_DIALECT_WORDS = {
    "هلق", "شو", "هيك", "كتير", "بدك", "كيف", "ماذا",
    "أين", "حسناً", "بالطبع",
}

# Words about punishment (channel guideline: encourage, don't scare)
_FORBIDDEN_PUNISHMENT_WORDS = {"نار", "عذاب", "جحيم"}


# ════════════════════════════════════════════════════════════════
# ScriptQualityValidator
# ════════════════════════════════════════════════════════════════
class ScriptQualityValidator(QualityValidator):
    """Rule-based validator for EpisodeScript dicts."""

    PASS_THRESHOLD: float = 70.0

    def validate(self, artifact: Any) -> QualityReport:
        """
        Validate an EpisodeScript (dict or pydantic model).
        """
        # Accept either dict or pydantic model
        if hasattr(artifact, "model_dump"):
            data = artifact.model_dump()
        elif isinstance(artifact, dict):
            data = artifact
        else:
            return QualityReport(
                passed=False,
                overall_score=0.0,
                critiques=["Unknown artifact type"],
            )

        critiques: List[str] = []
        scores: Dict[str, float] = {}

        # Structure
        scores["structure"] = self._score_structure(data, critiques)

        # Intro
        scores["intro"] = self._score_narrator(
            data.get("intro_scene", {}),
            "intro",
            MAX_INTRO_OUTRO_WORDS,
            critiques,
        )

        # Ayah scenes (averaged)
        scores["ayah"] = self._score_ayah_scenes(
            data.get("ayah_scenes", []), critiques
        )

        # Outro
        scores["outro"] = self._score_narrator(
            data.get("outro_scene", {}),
            "outro",
            MAX_INTRO_OUTRO_WORDS,
            critiques,
        )

        # Visual prompts
        scores["visual"] = self._score_visuals(data, critiques)

        overall = sum(scores.values()) / max(len(scores), 1)
        return QualityReport(
            passed=overall >= self.PASS_THRESHOLD,
            overall_score=round(overall, 1),
            field_scores={k: round(v, 1) for k, v in scores.items()},
            critiques=critiques[:30],
        )

    # ───────────────────────────────────────────────────────────
    # Sub-scorers
    # ───────────────────────────────────────────────────────────
    def _score_structure(
        self, data: Dict[str, Any], critiques: List[str],
    ) -> float:
        required = ("title", "intro_scene", "ayah_scenes", "outro_scene")
        missing = [k for k in required if k not in data]
        if missing:
            critiques.append(f"بنية ناقصة: {missing}")
            return 0.0
        if not data.get("ayah_scenes"):
            critiques.append("لا توجد آيات في السكريبت")
            return 0.0
        return 100.0

    def _score_narrator(
        self,
        scene: Dict[str, Any],
        label: str,
        max_words: int,
        critiques: List[str],
    ) -> float:
        text = scene.get("narrator_text", "")
        return self._score_text(text, label, max_words, critiques)

    def _score_ayah_scenes(
        self,
        scenes: List[Dict[str, Any]],
        critiques: List[str],
    ) -> float:
        if not scenes:
            critiques.append("ayah_scenes فارغة")
            return 0.0
        sub_scores: List[float] = []
        for i, sc in enumerate(scenes):
            for field, max_w in (
                ("intro_text", 25),
                ("explain_text", MAX_NARRATOR_WORDS),
            ):
                txt = sc.get(field, "")
                sub_scores.append(
                    self._score_text(txt, f"ayah[{i}].{field}", max_w, critiques)
                )
        return sum(sub_scores) / max(len(sub_scores), 1)

    def _score_visuals(
        self, data: Dict[str, Any], critiques: List[str],
    ) -> float:
        all_prompts: List[str] = []
        all_prompts.append(data.get("intro_scene", {}).get("visual_prompt", ""))
        all_prompts.append(data.get("outro_scene", {}).get("visual_prompt", ""))
        for sc in data.get("ayah_scenes", []):
            all_prompts.append(sc.get("visual_prompt", ""))

        bad_count = 0
        for p in all_prompts:
            if p and _FORBIDDEN_VISUAL_RE.search(p):
                bad_count += 1
                critiques.append(f"visual_prompt يحتوي على نمط ممنوع: {p[:50]}")
        return max(0.0, 100.0 - bad_count * 15.0)

    # ───────────────────────────────────────────────────────────
    # Text-level scoring
    # ───────────────────────────────────────────────────────────
    def _score_text(
        self,
        text: str,
        label: str,
        max_words: int,
        critiques: List[str],
    ) -> float:
        if not text:
            critiques.append(f"{label}: نص فارغ")
            return 0.0

        score = 100.0

        words = text.split()
        if len(words) > max_words:
            critiques.append(
                f"{label}: طويل ({len(words)} كلمة، الحد {max_words})"
            )
            score -= 25.0

        if _TASHKEEL_END_RE.search(text):
            critiques.append(f"{label}: تشكيل في نهاية الكلمات (artifact في TTS)")
            score -= 15.0

        word_set = set(words)
        bad_dialect = word_set & _FORBIDDEN_DIALECT_WORDS
        if bad_dialect:
            critiques.append(
                f"{label}: لهجة غير مصرية: {sorted(bad_dialect)}"
            )
            score -= 20.0

        bad_punishment = word_set & _FORBIDDEN_PUNISHMENT_WORDS
        if bad_punishment:
            critiques.append(
                f"{label}: كلمات تخويف: {sorted(bad_punishment)}"
            )
            score -= 15.0

        return max(0.0, score)
