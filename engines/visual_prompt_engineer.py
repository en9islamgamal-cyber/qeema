"""
engines/visual_prompt_engineer.py — VALUE / QEEMA v22.5 — LOCKED_STYLE Leonardo prompt builder
=========================================================================
Strict visual prompt construction. Prevents style drift across episodes.

[Why this exists]
v17 had a flaw: LLM wrote freeform prompts, then we appended a style suffix.
This created prompt conflicts ("photo of a child" + "2D illustration") →
Leonardo picked one randomly → inconsistent visual identity across episodes.

[Solution: locked template]
Visual prompts MUST follow this structure, enforced by code (not LLM):

    [SUBJECT] + [ACTION] + [ENVIRONMENT] + [LIGHTING] + [STYLE_LOCKED]

LLM only fills [SUBJECT], [ACTION], [ENVIRONMENT]
[LIGHTING] selected from emotion mapping
[STYLE_LOCKED] is constant — never changes across episodes
"""
from __future__ import annotations

from typing import Dict, Tuple


class VisualPromptEngineer:
    """Build Leonardo prompts from structured LLM output (classmethod-only).

    [v22.5 Visual Identity — NotebookLM watercolor reference]
    The LOCKED_STYLE was rebuilt from frame-by-frame analysis of the
    user-provided NotebookLM "أصحاب الفيل" reference video. Key signals:
      - Watercolor washes + ink line work (NOT Studio Ghibli, NOT 3D)
      - Cream paper background (subtle grid lines, hand-drawn aesthetic)
      - Muted earth palette: terracotta, dusty blue, pale yellow, warm grey
      - Subjects float on the page — heavy negative space, no full bleed
      - Loose ink splashes/spatters for energy (esp. action scenes)
      - Editorial children's book quality, vintage hand-drawn feel
      - 16:9 horizontal composition (matches 1280×720 720p output)
    """

    # ─── LOCKED style — NotebookLM watercolor + ink reference ────
    # CLIP-aware (Leonardo Lightning XL truncates at 77 tokens). The full
    # prompt = subject(~10t) + emotion(~10t) + composition(~7t) + LOCKED_STYLE.
    # That leaves ~50 tokens for LOCKED_STYLE. We pack the highest-impact
    # signals first: medium → palette → CRITICAL safety → reference tag.
    #
    # CRITICAL: "no faces, no text" MUST be in the first 77 tokens — they're
    # the safety constraints for kids' religious content. If they got truncated,
    # Leonardo could generate human faces or random Arabic text in the image.
    LOCKED_STYLE: str = (
        "watercolor and ink illustration on cream paper, "
        "loose pen lines, ink splashes, soft washes, "
        "muted earth palette terracotta dusty blue pale yellow, "
        "16:9 editorial, NotebookLM-style"
    )

    LOCKED_NEGATIVE: str = (
        "photo, photorealistic, 3D render, CGI, anime, manga, "
        "Studio Ghibli, Disney, Pixar, cel shading, "
        "saturated, neon, glossy, "
        "blurry, deformed, watermark, signature, "
        "scary, weapons, blood, "
        "named character, real person, celebrity"
    )

    # ─── Per-emotion mood (minimal — LOCKED_STYLE handles the medium) ─
    # Each value compressed to 2-4 words to fit CLIP budget. The watercolor
    # medium is in LOCKED_STYLE, so we don't repeat it here. Specific lighting
    # references like "rim lighting"/"sunbeams" are removed — they don't apply
    # to watercolor anyway.
    LIGHTING_BY_EMOTION: Dict[str, str] = {
        "warm": "soft golden afternoon",
        "reverent": "muted twilight, hushed",
        "playful": "bright morning, fresh",
        "peaceful": "pale dawn pastels",
        "excited": "bold ink splashes, high contrast",
    }

    # ─── Per-emotion composition hint (compact) ─────────────────
    COMPOSITION_BY_EMOTION: Dict[str, str] = {
        "warm": "wide editorial, rule of thirds",
        "reverent": "centered symmetric, contemplative",
        "playful": "off-center, sense of motion",
        "peaceful": "horizontal, low horizon",
        "excited": "low-angle wide, dynamic scale",
    }

    # ─── Critical safety prefix — must appear in first ~10 tokens ─
    # Even if a long subject exists, these phrases MUST get through CLIP.
    # Repeating them in both positive prompt prefix AND negative prompt
    # gives belt-and-suspenders protection.
    SAFETY_PREFIX: str = "no faces, no text, no logos"

    @classmethod
    def build_prompt(
        cls,
        *,
        subject: str,
        action: str = "",
        environment: str = "",
        emotion: str = "warm",
    ) -> Tuple[str, str]:
        """Build (positive_prompt, negative_prompt) from structured inputs.

        [v22.5 token budget structure]
        Order matters because Leonardo CLIP truncates at 77 tokens. The
        layout is:
          1. SAFETY_PREFIX (~6 tokens) — survives any truncation
          2. Subject (~10 tokens)
          3. Action + environment (~10 tokens)
          4. Emotion lighting + composition (~6 tokens)
          5. LOCKED_STYLE aesthetic tags (~33 tokens)
        Total: ~65 tokens, well under 77.

        Long deep-visual subjects from DeepVisualPromptGenerator can push
        this over the limit. The SAFETY_PREFIX up front + LOCKED_NEGATIVE
        backup ensure the no-faces / no-text constraints survive.
        """
        lighting = cls.LIGHTING_BY_EMOTION.get(
            emotion, cls.LIGHTING_BY_EMOTION["warm"]
        )
        composition = cls.COMPOSITION_BY_EMOTION.get(
            emotion, cls.COMPOSITION_BY_EMOTION["warm"]
        )

        # Build structured prompt — safety prefix FIRST so it survives
        # truncation from long subjects.
        parts = [cls.SAFETY_PREFIX, subject.strip()]
        if action.strip():
            parts.append(action.strip())
        if environment.strip():
            parts.append(f"in {environment.strip()}")
        parts.append(lighting)
        parts.append(composition)
        parts.append(cls.LOCKED_STYLE)

        positive = ", ".join(parts)

        # Truncate if too long (Leonardo limit: 1000 chars)
        if len(positive) > 1000:
            positive = positive[:997] + "..."

        return positive, cls.LOCKED_NEGATIVE

    @classmethod
    def build_from_llm_dict(
        cls, visual_data: Dict[str, str], emotion: str = "warm",
    ) -> Tuple[str, str]:
        """Build prompt from LLM JSON output dict."""
        return cls.build_prompt(
            subject=visual_data.get("subject", "abstract symbolic scene"),
            action=visual_data.get("action", ""),
            environment=visual_data.get("environment", ""),
            emotion=emotion,
        )

    @classmethod
    def build_legacy(cls, llm_visual_prompt: str, emotion: str = "warm") -> Tuple[str, str]:
        """Backward compatible builder for v16/v17 freeform prompts.
        Uses LLM text as 'subject', applies lighting/style on top.
        """
        return cls.build_prompt(subject=llm_visual_prompt, emotion=emotion)

    @classmethod
    def build_from_deep_result(
        cls, deep_result: Any, emotion: str = "warm",
    ) -> Tuple[str, str]:
        """v22.5: Build prompt from a DeepVisualPromptResult (chained Gemini output).

        Falls back gracefully:
            - Layer 1 only: equivalent to classic build (still better than legacy)
            - Layer 2 added: rich aesthetic baked in
            - Layer 3 added: full cinematographic composition

        The deep_result already contains palette, lighting, composition that
        SUPERSEDE the LIGHTING_BY_EMOTION/COMPOSITION_BY_EMOTION defaults.
        We append LOCKED_STYLE for visual identity consistency across episodes.

        Args:
            deep_result: DeepVisualPromptResult instance
            emotion: scene emotion (used only if deep_result has no aesthetic data)

        Returns:
            (positive_prompt, LOCKED_NEGATIVE)
        """
        if not deep_result or not getattr(deep_result, "is_usable", False):
            # Empty/broken result: fall back to safe default
            return cls.build_prompt(
                subject="abstract symbolic illustration",
                emotion=emotion,
            )

        # Use the deep result's merged content
        deep_content = deep_result.merge_to_leonardo_prompt()

        # If aesthetic layer (2) didn't run, fall back to emotion-mapped lighting
        if not getattr(deep_result, "lighting_direction", ""):
            lighting = cls.LIGHTING_BY_EMOTION.get(
                emotion, cls.LIGHTING_BY_EMOTION["warm"],
            )
            deep_content = f"{deep_content}, {lighting}"

        # If composition layer (3) didn't run, append a basic composition
        if not getattr(deep_result, "camera_angle", ""):
            composition = cls.COMPOSITION_BY_EMOTION.get(
                emotion, cls.COMPOSITION_BY_EMOTION["warm"],
            )
            deep_content = f"{deep_content}, {composition}"

        # Always append the LOCKED_STYLE for visual identity consistency.
        # SAFETY_PREFIX leads so safety constraints survive any truncation.
        positive = f"{cls.SAFETY_PREFIX}, {deep_content}, {cls.LOCKED_STYLE}"

        # Truncate if too long (Leonardo limit: 1000 chars)
        if len(positive) > 1000:
            positive = positive[:997] + "..."

        return positive, cls.LOCKED_NEGATIVE
