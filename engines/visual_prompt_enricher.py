"""
engines/visual_prompt_enricher.py — VALUE / QEEMA v22.5 (NEW)
=========================================================================
Chained Gemini calls to enrich visual prompts before sending to Leonardo.

[Why this exists]
The script LLM produces minimal visual fields:
  - visual_subject: 8 words max
  - visual_action: 6 words max
  - visual_environment: optional, terse

That's enough for "an ant" — but Leonardo produces flat, generic images
when the prompt has no atmosphere, depth, or composition guidance.

This module takes those minimal fields and runs 3 chained Gemini calls
to produce a rich, cinematic prompt of 150+ tokens. Each call has a
tight scope that's easy for Gemini to do well:

  Call A — Composition: subject + action + environment + time-of-day
  Call B — Aesthetic:   mood + palette + lighting direction
  Call C — Cinematic:   camera angle + depth-of-field + layered composition

Final output goes through VisualPromptEngineer.build_prompt() for the
locked style + negative prompt anchoring.

[When this runs]
Phase 1 of the 3-day pipeline. After script generation, before the
Leonardo asset generation in Phase 2. The enriched prompts are stored
in the script JSON's `visual_enriched_prompt` field.

[Cost analysis]
3 Gemini calls × 7 scenes = 21 calls per episode.
Gemini 2.5 Flash: ~$0.00015 per call → ~$0.003 per episode for enrichment.
Negligible compared to the quality improvement.

[Failure mode]
If any call fails, fall back to the minimal prompt from the script JSON.
The episode never blocks on enrichment failure — it's a quality enhancer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EnrichedVisualPrompt:
    """Result of the 3-call enrichment chain."""
    # Stage A: composition
    subject: str = ""
    action: str = ""
    environment: str = ""
    time_of_day: str = ""

    # Stage B: aesthetic
    mood: str = ""
    palette: str = ""
    lighting: str = ""

    # Stage C: cinematic
    camera_angle: str = ""
    depth_of_field: str = ""
    foreground: str = ""
    midground: str = ""
    background: str = ""

    # Tracking
    stages_completed: List[str] = field(default_factory=list)
    fallback_used: bool = False

    def to_leonardo_prompt(self) -> str:
        """Compose the final Leonardo prompt string."""
        parts: List[str] = []

        # Subject + action
        if self.subject:
            if self.action:
                parts.append(f"{self.subject} {self.action}")
            else:
                parts.append(self.subject)

        # Environment with time
        env_part = self.environment
        if self.time_of_day:
            env_part = f"{env_part}, {self.time_of_day}" if env_part else self.time_of_day
        if env_part:
            parts.append(env_part)

        # Aesthetic
        if self.lighting:
            parts.append(self.lighting)
        if self.palette:
            parts.append(self.palette)
        if self.mood:
            parts.append(self.mood)

        # Cinematic
        if self.camera_angle:
            parts.append(self.camera_angle)
        if self.depth_of_field:
            parts.append(self.depth_of_field)

        # Layered composition
        layers = []
        if self.foreground:
            layers.append(f"foreground: {self.foreground}")
        if self.midground:
            layers.append(f"midground: {self.midground}")
        if self.background:
            layers.append(f"background: {self.background}")
        if layers:
            parts.append("; ".join(layers))

        return ", ".join(p for p in parts if p)


# ════════════════════════════════════════════════════════════════
# Stage prompts — designed to be tight, fast, JSON-only
# ════════════════════════════════════════════════════════════════

STAGE_A_COMPOSITION_PROMPT = """Refine this visual scene description for a children's book illustration.

[Original (minimal)]
Subject: {subject}
Action: {action}
Environment: {environment}
Emotion: {emotion}

[Your task]
Expand into 4 specific fields for cinematic composition. Each field should
be concrete and specific (NOT vague). Avoid named characters, no human
faces, no famous IPs.

Return JSON only:
{{
  "subject": "What is the focal subject (12-20 words, very specific). Example: 'a small ant carrying an oak leaf 100 times its body weight, glistening with morning dew'",
  "action": "What is happening (8-12 words, dynamic verb). Example: 'climbing slowly up a textured tree bark surface'",
  "environment": "Where this happens (10-15 words, layered). Example: 'an ancient forest floor scattered with golden autumn leaves and moss-covered stones'",
  "time_of_day": "Time and atmospheric conditions (5-10 words). Example: 'golden hour just before sunset, soft mist rising'"
}}

NO markdown. NO preamble. JSON only."""


STAGE_B_AESTHETIC_PROMPT = """Define the aesthetic for this children's book scene.

[Composition]
Subject: {subject}
Action: {action}
Environment: {environment}
Time: {time_of_day}
Emotion target: {emotion}

[Your task]
Define mood, color palette, and lighting that match the {emotion} feeling.
Be specific — name actual colors and lighting techniques.

Return JSON only:
{{
  "mood": "Emotional atmosphere phrase (8-15 words). Example: 'serene wonder with gentle awe, contemplative stillness'",
  "palette": "Specific color palette (10-15 words). Example: 'warm honey gold, soft cream, deep emerald, touches of amber and ochre'",
  "lighting": "Lighting direction and quality (10-15 words). Example: 'soft directional sunlight from upper left, warm golden rim lighting on subject edges'"
}}

NO markdown. JSON only."""


STAGE_C_CINEMATIC_PROMPT = """Define the cinematic depth for this children's book scene.

[Aesthetic established]
Subject: {subject}
Mood: {mood}
Lighting: {lighting}

[Your task]
Add cinematic composition: camera angle, depth of field, and three-layer
composition (foreground, midground, background).

Return JSON only:
{{
  "camera_angle": "Camera position and framing (8-12 words). Example: 'low angle close-up, eye-level with the subject, shallow framing'",
  "depth_of_field": "DOF treatment (5-8 words). Example: 'shallow depth of field, soft creamy bokeh background'",
  "foreground": "What's in the foreground (5-10 words). Example: 'out-of-focus blades of grass and tiny wildflowers'",
  "midground": "Where the subject is (5-10 words). Example: 'sharply focused subject on textured tree bark'",
  "background": "Distant background (5-10 words). Example: 'soft blurred forest with hints of warm sunlight filtering through'"
}}

NO markdown. JSON only."""


# ════════════════════════════════════════════════════════════════
# VisualPromptEnricher
# ════════════════════════════════════════════════════════════════
class VisualPromptEnricher:
    """Run 3 chained Gemini calls to enrich a minimal visual prompt.

    Args:
        gemini_adapter: An instance with .generate_json(prompt, ...) method.
                       Should be a GeminiJsonAdapter from llm_adapters.
                       In the 3-day pipeline, this should be one of the
                       script_pool keys (NOT the tafsir-dedicated key).
    """

    def __init__(self, gemini_adapter: Any) -> None:
        if gemini_adapter is None:
            raise ValueError("VisualPromptEnricher requires a Gemini adapter")
        self._adapter = gemini_adapter

    def enrich_scene(
        self,
        *,
        subject: str,
        action: str = "",
        environment: str = "",
        emotion: str = "warm",
        max_retries_per_stage: int = 2,
    ) -> EnrichedVisualPrompt:
        """Run the 3-stage chain. Returns EnrichedVisualPrompt.

        Each stage is independent — if one fails, we use what we have so far.
        Worst case: returns a prompt with just the original minimal fields
        (still usable, just less rich).
        """
        result = EnrichedVisualPrompt(
            subject=subject, action=action, environment=environment,
        )

        # Stage A: Composition
        stage_a = self._call_stage(
            stage_name="composition",
            prompt=STAGE_A_COMPOSITION_PROMPT.format(
                subject=subject or "abstract symbolic scene",
                action=action or "in a peaceful moment",
                environment=environment or "natural setting",
                emotion=emotion,
            ),
            max_retries=max_retries_per_stage,
        )
        if stage_a:
            result.subject = stage_a.get("subject", subject)
            result.action = stage_a.get("action", action)
            result.environment = stage_a.get("environment", environment)
            result.time_of_day = stage_a.get("time_of_day", "")
            result.stages_completed.append("composition")

        # Stage B: Aesthetic (uses Stage A output)
        stage_b = self._call_stage(
            stage_name="aesthetic",
            prompt=STAGE_B_AESTHETIC_PROMPT.format(
                subject=result.subject,
                action=result.action,
                environment=result.environment,
                time_of_day=result.time_of_day or "natural daylight",
                emotion=emotion,
            ),
            max_retries=max_retries_per_stage,
        )
        if stage_b:
            result.mood = stage_b.get("mood", "")
            result.palette = stage_b.get("palette", "")
            result.lighting = stage_b.get("lighting", "")
            result.stages_completed.append("aesthetic")

        # Stage C: Cinematic (uses Stages A + B output)
        stage_c = self._call_stage(
            stage_name="cinematic",
            prompt=STAGE_C_CINEMATIC_PROMPT.format(
                subject=result.subject,
                mood=result.mood or "peaceful contemplation",
                lighting=result.lighting or "soft natural light",
            ),
            max_retries=max_retries_per_stage,
        )
        if stage_c:
            result.camera_angle = stage_c.get("camera_angle", "")
            result.depth_of_field = stage_c.get("depth_of_field", "")
            result.foreground = stage_c.get("foreground", "")
            result.midground = stage_c.get("midground", "")
            result.background = stage_c.get("background", "")
            result.stages_completed.append("cinematic")

        # Mark fallback if anything missed
        if len(result.stages_completed) < 3:
            result.fallback_used = True
            logger.warning(
                f"⚠️ Visual enrichment partial: {len(result.stages_completed)}/3 "
                f"stages completed ({result.stages_completed})"
            )
        else:
            logger.info(f"🎨 Visual enrichment: all 3 stages completed")

        return result

    def enrich_episode(
        self,
        episode_data: Dict[str, Any],
        *,
        max_retries_per_stage: int = 2,
    ) -> Dict[str, Any]:
        """Enrich all scenes in an episode dict.

        Modifies episode_data in-place by adding `visual_enriched_prompt`
        field to each scene. Returns the same dict for chaining.

        Args:
            episode_data: Dict from script generation with `ayah_scenes`.
            max_retries_per_stage: how many retries per stage before falling
                                  back to original.

        Returns:
            episode_data with enriched prompts added per scene.
        """
        scenes = episode_data.get("ayah_scenes", [])
        if not scenes:
            logger.warning("No ayah_scenes to enrich")
            return episode_data

        enriched_count = 0
        partial_count = 0
        for i, scene in enumerate(scenes):
            try:
                enriched = self.enrich_scene(
                    subject=scene.get("visual_subject", ""),
                    action=scene.get("visual_action", ""),
                    environment=scene.get("visual_environment", ""),
                    emotion=scene.get("scene_emotion", "warm"),
                    max_retries_per_stage=max_retries_per_stage,
                )
                scene["visual_enriched_prompt"] = enriched.to_leonardo_prompt()
                scene["visual_enriched_meta"] = {
                    "stages_completed": enriched.stages_completed,
                    "fallback_used": enriched.fallback_used,
                }
                if not enriched.fallback_used:
                    enriched_count += 1
                else:
                    partial_count += 1
            except Exception as e:
                logger.error(
                    f"❌ Enrichment failed for scene {i+1}: {e} — "
                    f"using minimal prompt"
                )
                scene["visual_enriched_prompt"] = self._fallback_prompt(scene)
                scene["visual_enriched_meta"] = {
                    "stages_completed": [],
                    "fallback_used": True,
                    "error": str(e),
                }
                partial_count += 1

        logger.info(
            f"🎨 Episode enrichment: {enriched_count}/{len(scenes)} fully enriched, "
            f"{partial_count} partial/fallback"
        )
        return episode_data

    # ─── Internal helpers ──────────────────────────────────────
    def _call_stage(
        self,
        *,
        stage_name: str,
        prompt: str,
        max_retries: int = 2,
    ) -> Optional[Dict[str, str]]:
        """Single Gemini call with retry. Returns parsed dict or None on failure."""
        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                response = self._adapter.generate_json(
                    prompt=prompt,
                    temperature=0.6,  # some variation but consistent
                    max_tokens=600,
                )
                # generate_json returns dict directly
                if isinstance(response, dict):
                    return response
                # Some adapters return raw text — try to parse
                if isinstance(response, str):
                    return json.loads(response)
                logger.warning(
                    f"Unexpected response type from {stage_name}: "
                    f"{type(response).__name__}"
                )
                return None
            except json.JSONDecodeError as e:
                last_err = e
                logger.warning(
                    f"⚠️ Stage {stage_name} attempt {attempt}/{max_retries}: "
                    f"JSON parse error — {e}"
                )
            except Exception as e:
                last_err = e
                logger.warning(
                    f"⚠️ Stage {stage_name} attempt {attempt}/{max_retries}: {e}"
                )
        logger.error(f"❌ Stage {stage_name} failed after {max_retries} attempts")
        return None

    @staticmethod
    def _fallback_prompt(scene: Dict[str, Any]) -> str:
        """Build a minimal prompt from scene fields when enrichment fails."""
        parts = []
        if scene.get("visual_subject"):
            parts.append(scene["visual_subject"])
        if scene.get("visual_action"):
            parts.append(scene["visual_action"])
        if scene.get("visual_environment"):
            parts.append(f"in {scene['visual_environment']}")
        return ", ".join(parts) if parts else "abstract peaceful scene"
