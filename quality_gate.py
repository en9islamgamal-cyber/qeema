"""
quality_gate.py — QEEMA v4.0
بوابة الجودة: تقييم السكريبت
"""

import json
import re
from typing import Union, Dict, List, Tuple, Any
from dataclasses import dataclass, field

import logging
logger = logging.getLogger(__name__)


@dataclass
class QualityReport:
    passed: bool
    overall_score: float
    field_scores: Dict[str, float]
    critiques: List[str]
    details: Dict[str, Any] = field(default_factory=dict)


class QualityGate:
    MAX_WORDS = 20
    FORBIDDEN_TASHKEEL = re.compile(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])')
    FORBIDDEN_VISUAL = re.compile(r'(Pixar|3D|realistic|photograph|complex)', re.I)

    def evaluate(self, script_data: Union[str, dict]) -> QualityReport:
        if isinstance(script_data, str):
            try:
                data = json.loads(script_data)
            except json.JSONDecodeError as e:
                return QualityReport(passed=False, overall_score=0, field_scores={}, critiques=[f"JSON error: {e}"], details={})
        else:
            data = script_data

        scores = {}
        critiques = []

        # فحص البنية
        required = ["title", "intro_scene", "ayah_scenes", "outro_scene"]
        missing = [f for f in required if f not in data]
        if missing:
            critiques.append(f"حقول مفقودة: {missing}")
            scores["structure"] = 0
        else:
            scores["structure"] = 100

        # فحص intro
        intro = data.get("intro_scene", {})
        intro_text = intro.get("narrator_text", "")
        if len(intro_text.split()) > self.MAX_WORDS:
            critiques.append("intro_text طويل جداً")
        if self.FORBIDDEN_TASHKEEL.search(intro_text):
            critiques.append("intro_text يحتوي تشكيلاً")
        scores["intro"] = max(0, 100 - len([c for c in critiques if "intro" in c]) * 20)

        # فحص ayah scenes
        ayah_scores = []
        for i, ayah in enumerate(data.get("ayah_scenes", [])):
            crit = []
            for field in ["intro_text", "explain_text"]:
                txt = ayah.get(field, "")
                if len(txt.split()) > self.MAX_WORDS:
                    crit.append(f"{field} طويل")
                if self.FORBIDDEN_TASHKEEL.search(txt):
                    crit.append(f"{field} يحتوي تشكيلاً")
            ayah_scores.append(max(0, 100 - len(crit) * 20))
            critiques.extend(crit)
        scores["ayah"] = sum(ayah_scores) / len(ayah_scores) if ayah_scores else 0

        # فحص outro
        outro = data.get("outro_scene", {})
        outro_text = outro.get("narrator_text", "")
        if len(outro_text.split()) > self.MAX_WORDS:
            critiques.append("outro_text طويل")
        if self.FORBIDDEN_TASHKEEL.search(outro_text):
            critiques.append("outro_text يحتوي تشكيلاً")
        scores["outro"] = max(0, 100 - len([c for c in critiques if "outro" in c]) * 20)

        # فحص visual prompts
        vis_texts = [intro.get("visual_prompt", ""), outro.get("visual_prompt", "")]
        for ayah in data.get("ayah_scenes", []):
            vis_texts.append(ayah.get("visual_prompt", ""))
        vis_crit = [self.FORBIDDEN_VISUAL.search(v) for v in vis_texts if v]
        scores["visual"] = max(0, 100 - len([c for c in vis_crit if c]) * 15)

        overall = sum(scores.values()) / len(scores) if scores else 0
        passed = overall >= 70
        return QualityReport(passed=passed, overall_score=round(overall,1), field_scores=scores, critiques=critiques[:20], details={})