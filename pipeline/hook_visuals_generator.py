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
    VISUALS_SYSTEM_PROMPT, build_visuals_user_prompt,
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

    # Defensive: ensure all full_prompts have the style template
    parsed = _enrich_visual_prompts(parsed)

    log.info(
        "✓ Hook + visuals generated: hook=%d chars, %d ayah visuals, %d thumbnails",
        len(parsed.hook_text),
        len(parsed.ayah_visuals),
        len(parsed.thumbnail_visuals),
    )
    return parsed, key_label


def _enrich_visual_prompts(
    hav: EpisodeHookAndVisuals,
) -> EpisodeHookAndVisuals:
    """
    Defensive post-processing: ensure every full_prompt includes the
    style template. If Gemini forgot, we append it.
    """
    # We can't mutate Pydantic models in-place safely, so we
    # re-build the model with enriched fields
    def enrich(vp):
        new_full = ensure_style_in_full_prompt(vp.full_prompt)
        if new_full == vp.full_prompt:
            return vp
        return vp.model_copy(update={"full_prompt": new_full})

    return hav.model_copy(update={
        "hook_visual": enrich(hav.hook_visual),
        "intro_visual": enrich(hav.intro_visual),
        "ayah_visuals": [enrich(v) for v in hav.ayah_visuals],
        "outro_visual": enrich(hav.outro_visual),
        "thumbnail_visuals": [enrich(v) for v in hav.thumbnail_visuals],
    })
