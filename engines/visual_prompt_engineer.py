"""
engines/visual_prompt_engineer.py — VALUE / QEEMA v18.0
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
    """Build Leonardo prompts from structured LLM output (classmethod-only)."""

    # ─── LOCKED style — never changes across episodes ────────────
    LOCKED_STYLE: str = (
        "hand-painted children's book illustration, "
        "Studio Ghibli inspired, soft brushstrokes, "
        "harmonious warm color palette, dreamy atmosphere, "
        "shallow depth of field, atmospheric perspective, "
        "layered foreground midground background, "
        "rim lighting, subtle particles in air, "
        "16:9 widescreen cinematic composition, "
        "no text, no logos, no signatures, "
        "no detailed human faces, no recognizable characters, "
        "professional editorial illustration quality, "
        "award-winning concept art"
    )

    LOCKED_NEGATIVE: str = (
        "photo, photograph, photorealistic, hyperrealistic, "
        "3D render, CGI, video game, anime, manga, "
        "cartoon network style, low quality, blurry, pixelated, "
        "watermark, text, signature, deformed, distorted, "
        "scary, horror, dark themes, weapons, violence, blood, "
        "named character, real person, celebrity, politician, "
        "uncanny valley, creepy faces, plastic skin"
    )

    # ─── Per-emotion lighting (carries the mood) ─────────────────
    LIGHTING_BY_EMOTION: Dict[str, str] = {
        "warm": "golden hour sunlight, soft warm tones, gentle shadows",
        "reverent": "soft moonlight with starlight, ethereal blue-violet glow, sacred atmosphere",
        "playful": "bright morning light, vibrant cheerful colors, dynamic energy",
        "peaceful": "twilight pink-purple sky, soft diffused light, calm serenity",
        "excited": "dramatic sunbeams, cinematic backlight, dynamic high contrast",
    }

    # ─── Per-emotion compositional hint ─────────────────────────
    COMPOSITION_BY_EMOTION: Dict[str, str] = {
        "warm": "wide cinematic shot, rule of thirds",
        "reverent": "symmetric composition, contemplative spacing",
        "playful": "dynamic angle, leading lines, sense of motion",
        "peaceful": "horizontal composition, expansive sky, minimal foreground",
        "excited": "low angle wide shot, sweeping vista, sense of scale",
    }

    @classmethod
    def build_prompt(
        cls,
        *,
        subject: str,
        action: str = "",
        environment: str = "",
        emotion: str = "warm",
    ) -> Tuple[str, str]:
        """Build (positive_prompt, negative_prompt) from structured inputs."""
        lighting = cls.LIGHTING_BY_EMOTION.get(
            emotion, cls.LIGHTING_BY_EMOTION["warm"]
        )
        composition = cls.COMPOSITION_BY_EMOTION.get(
            emotion, cls.COMPOSITION_BY_EMOTION["warm"]
        )

        # Build structured prompt
        parts = [subject.strip()]
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

        # Always append the LOCKED_STYLE for visual identity consistency
        positive = f"{deep_content}, {cls.LOCKED_STYLE}"

        # Truncate to Leonardo's 1500 char limit (more generous than legacy's 1000)
        if len(positive) > 1500:
            positive = positive[:1497] + "..."

        return positive, cls.LOCKED_NEGATIVE
