"""
pipeline/tafsir_generator.py
====================================================================
Stage 1 of the pipeline: generate the Egyptian-Arabic narration.

Inputs: surah info + verified ayah text
Output: EpisodeNarration (Pydantic model)
Cost:   1 Gemini call
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from core.models import EpisodeNarration, VerifiedAyah
from pipeline.prompts import SHEIKH_SYSTEM_PROMPT, build_sheikh_user_prompt
from assets_engines.gemini_client import GeminiClient


log = logging.getLogger(__name__)


def generate_narration(
    surah_name: str,
    surah_number: int,
    ayahs: List[VerifiedAyah],
    gemini: GeminiClient,
) -> Tuple[EpisodeNarration, str]:
    """
    Generate the full narration for an episode.

    Returns: (EpisodeNarration, gemini_key_label_used)
    """
    if not ayahs:
        raise ValueError("Need at least one ayah")

    log.info(
        "Generating narration for سورة %s (آيات %d-%d)",
        surah_name,
        ayahs[0].number,
        ayahs[-1].number,
    )

    ayahs_payload = [
        {"number": a.number, "text": a.text}
        for a in ayahs
    ]

    user_prompt = build_sheikh_user_prompt(
        surah_name=surah_name,
        surah_number=surah_number,
        ayahs=ayahs_payload,
    )

    parsed, key_label = gemini.generate_structured(
        system_prompt=SHEIKH_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_schema=EpisodeNarration,
        temperature=0.75,  # slight creativity for storytelling
    )

    log.info(
        "✓ Narration generated: title='%s', %d ayahs",
        parsed.title, len(parsed.ayahs),
    )
    return parsed, key_label
