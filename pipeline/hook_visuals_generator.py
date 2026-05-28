"""
pipeline/hook_visuals_generator.py
====================================================================
Stage 2 of the pipeline: generate the hook text + all visual prompts.

Inputs: EpisodeNarration from Stage 1
Output: EpisodeHookAndVisuals (Pydantic model)
Cost:   1 Gemini call
"""
from __future__ import annotations

import logging
from typing import Tuple

from core.models import EpisodeNarration, EpisodeHookAndVisuals
from pipeline.prompts import (
    VISUALS_SYSTEM_PROMPT,
    build_visuals_user_prompt,
    ensure_style_in_full_prompt,
)
from assets_engines.gemini_client import GeminiClient


log = logging.getLogger(__name__)


def generate_hook_and_visuals(
    surah_name: str,
    narration: EpisodeNarration,
    gemini: GeminiClient,
) -> Tuple[EpisodeHookAndVisuals, str]:
    """
    Generate hook + all visual prompts, grounded in the narration.

    Returns: (EpisodeHookAndVisuals, gemini_key_label_used)
    """
    log.info(
        "Generating hook + %d visual prompts for سورة %s",
        len(narration.ayahs) + 5,  # 1 hook + 1 intro + N ayah + 1 outro + 3 thumb
        surah_name,
    )

    ayah_explanations = [
        {
            "ayah_number": a.ayah_number,
            "ayah_text": a.ayah_text,
            "narration": a.narration,
        }
        for a in narration.ayahs
    ]

    user_prompt = build_visuals_user_prompt(
        surah_name=surah_name,
        title=narration.title,
        intro_text=narration.intro,
        ayah_explanations=ayah_explanations,
        outro_text=narration.outro,
    )

    parsed, key_label = gemini.generate_structured(
        system_prompt=VISUALS_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_schema=EpisodeHookAndVisuals,
        temperature=0.7,
    )

    parsed = _enrich_visual_prompts(parsed)

    log.info(
        "✓ Hook + visuals generated: hook=%d chars, %d ayah visuals, %d thumbnails",
        len(parsed.hook_text),
        len(parsed.ayah_visuals),
        len(parsed.thumbnail_visuals),
    )
    return parsed, key_label


def _dedupe_csv_phrases(text: str) -> str:
    """
    Small cleanup helper:
    If Gemini returns comma-separated repeated phrases,
    keep only the first occurrence of each phrase.
    """
    if not text:
        return text

    parts = [p.strip() for p in text.split(",") if p.strip()]
    seen = set()
    cleaned = []

    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(part)

    return ", ".join(cleaned)


def _motion_hint_for(vp) -> str:
    """
    Generate a human-readable motion hint that later stages
    (video assembler / renderer) can use to choose a camera move.

    Important:
    - This file does NOT execute the motion.
    - It only encodes the intended viewing pattern.
    """
    purpose = (getattr(vp, "purpose", "") or "").strip().lower()

    if purpose == "hook":
        return (
            "begin on the strongest symbolic element, "
            "make a gentle zoom in to create curiosity, "
            "then drift toward a second supporting element, "
            "ending on the clearer wider composition"
        )

    if purpose == "intro":
        return (
            "start with a calm medium-wide view, "
            "slowly move across one symbolic cluster, "
            "then ease into a wider reveal that prepares for recitation"
        )

    if purpose == "ayah":
        return (
            "start on one symbolic cluster, "
            "move in the same narrative order as the explained meaning, "
            "visit a second and then a third idea cluster if present, "
            "and finish with a soft zoom out that partially reveals the full board"
        )

    if purpose == "outro":
        return (
            "begin from a meaningful inner detail, "
            "slowly pull back, "
            "and finish with a full-board zoom out showing the complete image and all its ideas"
        )

    if purpose == "thumbnail":
        return (
            "mostly static framing, "
            "keep one dominant focal point centered or clearly emphasized, "
            "with only a very subtle push for energy if needed"
        )

    return (
        "start on a clear focal element, "
        "gently move toward the next meaningful area, "
        "and avoid fast or chaotic motion"
    )


def _enrich_visual_prompts(
    hav: EpisodeHookAndVisuals,
) -> EpisodeHookAndVisuals:
    """
    Defensive post-processing:
    - ensure every full_prompt includes the fixed style wrapper
    - optionally inject a motion_hint if the schema supports it
    """
    def enrich(vp):
        cleaned_prompt = _dedupe_csv_phrases(vp.full_prompt)
        new_full = ensure_style_in_full_prompt(cleaned_prompt)

        updates = {}

        if new_full != vp.full_prompt:
            updates["full_prompt"] = new_full

        if hasattr(vp, "motion_hint"):
            updates["motion_hint"] = _motion_hint_for(vp)

        if not updates:
            return vp

        return vp.model_copy(update=updates)

    return hav.model_copy(update={
        "hook_visual": enrich(hav.hook_visual),
        "intro_visual": enrich(hav.intro_visual),
        "ayah_visuals": [enrich(v) for v in hav.ayah_visuals],
        "outro_visual": enrich(hav.outro_visual),
        "thumbnail_visuals": [enrich(v) for v in hav.thumbnail_visuals],
    })