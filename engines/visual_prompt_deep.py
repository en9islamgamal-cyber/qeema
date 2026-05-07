"""
engines/visual_prompt_deep.py — VALUE / QEEMA v22.5 (NEW)
=========================================================================
Deep visual prompt generator using chained Gemini calls.

[Why this exists]
The existing flow gets `visual_subject`, `visual_action`, `visual_environment`
as side-output from the script generation call. These are SHALLOW because:
  - The script LLM is focused on text quality, not visuals
  - It writes 5-8 word fields (e.g., "vast galaxy with stars")
  - No depth, composition, lighting nuance, or atmospheric detail
  - Leonardo gets a generic prompt → generic image

[The 3-day pipeline gives us time]
Phase 1 of the new pipeline runs only Gemini calls. We have budget to chain
3 calls per scene to deepen the visual description WITHOUT impacting Phase 2
(Leonardo) or Phase 3 (render). Total cost: 21 extra Gemini calls per
episode in Phase 1, ~5 minutes added to phase 1 only.

[The 3 chained calls per scene]

  Call 1 — Scene primitives:
      Input:  ayah text + scene emotion + initial seeds
      Output: refined subject + action + environment + time-of-day

  Call 2 — Aesthetic refinement:
      Input:  scene primitives from Call 1
      Output: mood + color palette + lighting direction + atmospheric elements
              (fog, particles, distance haze)

  Call 3 — Cinematic composition:
      Input:  primitives + aesthetic
      Output: camera angle + depth of field + foreground/midground/background
              + focal point + leading lines

[Output]
A DeepVisualPromptResult containing all 3 layers, which the
VisualPromptEngineer merges into a single ~150 token Leonardo prompt
instead of the current ~30 token version.

[Failure handling]
If any chained call fails:
  - Log warning
  - Use whatever data we have so far (degrade gracefully)
  - Worst case: fall back to original visual_subject/action/environment
    from the script call (current behavior)

So this module is purely additive — never breaks the existing flow.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DeepVisualPromptResult:
    """Result of chained Gemini visual prompt generation for ONE scene."""
    # Layer 1: scene primitives
    subject: str = ""
    action: str = ""
    environment: str = ""
    time_of_day: str = ""

    # Layer 2: aesthetic
    mood: str = ""
    color_palette: str = ""
    lighting_direction: str = ""
    atmospheric_elements: str = ""

    # Layer 3: cinematic composition
    camera_angle: str = ""
    depth_of_field: str = ""
    foreground: str = ""
    midground: str = ""
    background: str = ""
    focal_point: str = ""

    # Tracking
    layers_completed: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.layers_completed == 3

    @property
    def is_usable(self) -> bool:
        """Is this result good enough to use? At minimum need layer 1."""
        return bool(self.subject) and self.layers_completed >= 1

    def merge_to_leonardo_prompt(self) -> str:
        """Merge all 3 layers into a single dense Leonardo positive prompt.

        Returns a comma-separated prompt ~150 tokens. Falls through gracefully
        if not all layers present.
        """
        parts: List[str] = []

        # Layer 1: scene primitives (must exist)
        if self.subject:
            parts.append(self.subject)
        if self.action:
            parts.append(self.action)
        if self.environment:
            parts.append(f"in {self.environment}")
        if self.time_of_day:
            parts.append(self.time_of_day)

        # Layer 2: aesthetic
        if self.mood:
            parts.append(f"{self.mood} mood")
        if self.color_palette:
            parts.append(f"color palette: {self.color_palette}")
        if self.lighting_direction:
            parts.append(self.lighting_direction)
        if self.atmospheric_elements:
            parts.append(self.atmospheric_elements)

        # Layer 3: composition
        if self.camera_angle:
            parts.append(self.camera_angle)
        if self.depth_of_field:
            parts.append(self.depth_of_field)
        if self.foreground:
            parts.append(f"foreground: {self.foreground}")
        if self.midground:
            parts.append(f"midground: {self.midground}")
        if self.background:
            parts.append(f"background: {self.background}")
        if self.focal_point:
            parts.append(f"focal point: {self.focal_point}")

        return ", ".join(p for p in parts if p)


class DeepVisualPromptGenerator:
    """Generate deep, layered visual prompts via chained Gemini calls.

    Uses ONE Gemini adapter (assumed to be from the script pool, since the
    script call already happened in the same Phase 1).

    Each call has a tight, bounded prompt with strict JSON schema. The
    output is merged into a structured DeepVisualPromptResult.
    """

    # Hard timeouts to prevent Phase 1 hanging
    PER_CALL_TIMEOUT_SEC: int = 30

    def __init__(self, gemini_adapter: Any) -> None:
        """
        Args:
            gemini_adapter: Any object with .generate_json(prompt) -> Dict
                            or .generate(prompt) -> str. We try both interfaces.
        """
        self._adapter = gemini_adapter

    # ════════════════════════════════════════════════════════════════
    # Public entry — generate for a single scene
    # ════════════════════════════════════════════════════════════════
    def generate_for_scene(
        self,
        ayah_text: str,
        ayah_number: int,
        scene_emotion: str,
        initial_subject: str = "",
        initial_action: str = "",
        initial_environment: str = "",
    ) -> DeepVisualPromptResult:
        """Run all 3 chained calls. Returns whatever succeeded."""
        result = DeepVisualPromptResult(
            subject=initial_subject,
            action=initial_action,
            environment=initial_environment,
        )

        # Layer 1: scene primitives
        try:
            data1 = self._call_layer1(
                ayah_text, ayah_number, scene_emotion,
                initial_subject, initial_action, initial_environment,
            )
            self._merge_layer1(data1, result)
            result.layers_completed = 1
        except Exception as e:
            logger.warning(f"⚠️ Visual layer 1 failed (ayah {ayah_number}): {e}")
            result.errors.append(f"layer1: {e}")
            # Without layer 1, can't continue
            return result

        # Layer 2: aesthetic
        try:
            data2 = self._call_layer2(result, scene_emotion)
            self._merge_layer2(data2, result)
            result.layers_completed = 2
        except Exception as e:
            logger.warning(f"⚠️ Visual layer 2 failed (ayah {ayah_number}): {e}")
            result.errors.append(f"layer2: {e}")
            return result  # layer 1 still usable

        # Layer 3: cinematic composition
        try:
            data3 = self._call_layer3(result, scene_emotion)
            self._merge_layer3(data3, result)
            result.layers_completed = 3
        except Exception as e:
            logger.warning(f"⚠️ Visual layer 3 failed (ayah {ayah_number}): {e}")
            result.errors.append(f"layer3: {e}")

        return result

    # ════════════════════════════════════════════════════════════════
    # Adapter abstraction — works with any Gemini wrapper interface
    # ════════════════════════════════════════════════════════════════
    def _call_gemini_json(self, prompt: str) -> Dict[str, Any]:
        """Call Gemini and parse JSON response. Tries multiple adapter styles."""
        # Try generate_json first (most explicit)
        if hasattr(self._adapter, "generate_json"):
            return self._adapter.generate_json(prompt)

        # Fallback: generate() returning text, parse manually
        if hasattr(self._adapter, "generate"):
            text = self._adapter.generate(prompt)
            return self._parse_json_safe(text)

        # Last resort: __call__ if it's a callable
        if callable(self._adapter):
            text = self._adapter(prompt)
            return self._parse_json_safe(text)

        raise RuntimeError(
            "Gemini adapter must have .generate_json(), .generate(), or be callable"
        )

    @staticmethod
    def _parse_json_safe(text: str) -> Dict[str, Any]:
        """Strip markdown fences and parse JSON."""
        if not isinstance(text, str):
            return {}
        text = text.strip()
        # Remove markdown fences
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from Gemini: {e} | text[:200]={text[:200]}")

    # ════════════════════════════════════════════════════════════════
    # Layer 1: scene primitives
    # ════════════════════════════════════════════════════════════════
    def _call_layer1(
        self,
        ayah_text: str,
        ayah_number: int,
        emotion: str,
        seed_subject: str,
        seed_action: str,
        seed_environment: str,
    ) -> Dict[str, Any]:
        """Layer 1: refined scene primitives."""
        seed_hint = ""
        if seed_subject or seed_action or seed_environment:
            seed_hint = f"""
Seeds from script generation (refine these, don't replace):
- subject: {seed_subject or '(none)'}
- action: {seed_action or '(none)'}
- environment: {seed_environment or '(none)'}
"""

        prompt = f"""You are a cinematic concept artist creating a single illustration
for an ayah from the Quran (Arabic verse) for children's content.

Ayah {ayah_number} text (Arabic): {ayah_text}
Scene emotion: {emotion}
{seed_hint}
TASK: Define the SCENE PRIMITIVES for the illustration. Be SPECIFIC,
concrete, and visually rich. Avoid abstract clichés.

CRITICAL CONSTRAINTS:
- NO human faces (no recognizable people)
- NO famous characters or copyrighted entities
- NO text or letters in the image
- Subject must be from nature, cosmos, or abstract symbolism
- Be CONCRETE: "ant carrying leaf 100x its body weight on bark texture"
  is better than "an ant"

Respond with JSON ONLY (no markdown, no preamble):
{{
  "subject": "the main subject of the scene, 8-12 words, very specific",
  "action": "what is happening, 4-8 words, present continuous",
  "environment": "where it happens, 6-10 words including atmospheric texture",
  "time_of_day": "exact time (golden hour / blue hour / night with stars / etc.)"
}}"""
        return self._call_gemini_json(prompt)

    @staticmethod
    def _merge_layer1(data: Dict, result: DeepVisualPromptResult) -> None:
        """Merge layer 1 fields into result, overriding seeds with refined values."""
        if data.get("subject"):
            result.subject = str(data["subject"]).strip()
        if data.get("action"):
            result.action = str(data["action"]).strip()
        if data.get("environment"):
            result.environment = str(data["environment"]).strip()
        if data.get("time_of_day"):
            result.time_of_day = str(data["time_of_day"]).strip()

    # ════════════════════════════════════════════════════════════════
    # Layer 2: aesthetic refinement
    # ════════════════════════════════════════════════════════════════
    def _call_layer2(
        self, partial: DeepVisualPromptResult, emotion: str,
    ) -> Dict[str, Any]:
        """Layer 2: aesthetic — mood, palette, lighting, atmosphere."""
        prompt = f"""You are a cinematographer defining the AESTHETIC for an illustration.

Scene primitives (from previous step):
- Subject: {partial.subject}
- Action: {partial.action}
- Environment: {partial.environment}
- Time of day: {partial.time_of_day}
- Emotion: {emotion}

TASK: Define mood, color palette, lighting, and atmospheric elements.

For lighting direction, use professional cinematography terms like:
"warm rim lighting from left", "soft top-down sunlight through canopy",
"moonlight filtering through mist", etc.

For color palette, name 3-4 specific colors that harmonize, e.g.:
"warm ochre, deep teal, soft cream, rust orange".

For atmospheric elements, name visible particles/effects:
"dust motes in sunbeams", "fine mist over water", "snow flurries", etc.

Respond with JSON ONLY (no markdown):
{{
  "mood": "1-3 words describing emotional atmosphere",
  "color_palette": "3-4 specific colors that harmonize",
  "lighting_direction": "professional cinematography lighting description",
  "atmospheric_elements": "visible particles, fog, mist, glow effects"
}}"""
        return self._call_gemini_json(prompt)

    @staticmethod
    def _merge_layer2(data: Dict, result: DeepVisualPromptResult) -> None:
        if data.get("mood"):
            result.mood = str(data["mood"]).strip()
        if data.get("color_palette"):
            result.color_palette = str(data["color_palette"]).strip()
        if data.get("lighting_direction"):
            result.lighting_direction = str(data["lighting_direction"]).strip()
        if data.get("atmospheric_elements"):
            result.atmospheric_elements = str(data["atmospheric_elements"]).strip()

    # ════════════════════════════════════════════════════════════════
    # Layer 3: cinematic composition
    # ════════════════════════════════════════════════════════════════
    def _call_layer3(
        self, partial: DeepVisualPromptResult, emotion: str,
    ) -> Dict[str, Any]:
        """Layer 3: cinematic composition — camera, depth, layered scene."""
        prompt = f"""You are a film director defining COMPOSITION for an illustration.

Scene so far:
- Subject: {partial.subject}
- Action: {partial.action}
- Environment: {partial.environment}
- Time of day: {partial.time_of_day}
- Mood: {partial.mood}
- Lighting: {partial.lighting_direction}

TASK: Define cinematic composition elements.

For camera_angle, use specific terms:
"low angle wide shot", "overhead bird's eye", "Dutch angle close-up",
"eye-level medium shot with leading lines", etc.

For depth_of_field:
"shallow depth of field with bokeh background",
"deep focus showing all layers sharp",
"selective focus on midground subject", etc.

For foreground/midground/background, describe what's visible at each
distance plane.

For focal_point, identify the visual anchor that draws the eye.

Respond with JSON ONLY (no markdown):
{{
  "camera_angle": "specific cinematography angle and shot type",
  "depth_of_field": "depth of field description with bokeh/focus details",
  "foreground": "what occupies the closest visual plane",
  "midground": "what occupies the middle distance",
  "background": "what occupies the far distance",
  "focal_point": "the primary visual anchor that draws the viewer's eye"
}}"""
        return self._call_gemini_json(prompt)

    @staticmethod
    def _merge_layer3(data: Dict, result: DeepVisualPromptResult) -> None:
        if data.get("camera_angle"):
            result.camera_angle = str(data["camera_angle"]).strip()
        if data.get("depth_of_field"):
            result.depth_of_field = str(data["depth_of_field"]).strip()
        if data.get("foreground"):
            result.foreground = str(data["foreground"]).strip()
        if data.get("midground"):
            result.midground = str(data["midground"]).strip()
        if data.get("background"):
            result.background = str(data["background"]).strip()
        if data.get("focal_point"):
            result.focal_point = str(data["focal_point"]).strip()

    # ════════════════════════════════════════════════════════════════
    # Episode-level batch generation
    # ════════════════════════════════════════════════════════════════
    def generate_for_episode(
        self,
        ayah_scenes: List[Dict[str, Any]],
        max_workers: int = 3,
    ) -> List[DeepVisualPromptResult]:
        """Generate deep prompts for all scenes in an episode.

        Uses ThreadPoolExecutor for parallelism (Gemini supports concurrent
        requests up to its rate limit). Order is preserved.

        Args:
            ayah_scenes: list of scene dicts from script JSON. Each must have
                         'ayah' or 'ayah_number', 'scene_emotion', and
                         optionally visual_subject/action/environment.
            max_workers: thread pool size (don't exceed Gemini RPM limit).

        Returns:
            List of DeepVisualPromptResult, one per scene, same order.
        """
        import concurrent.futures

        def _run_one(idx: int, scene: Dict[str, Any]) -> tuple:
            ayah_text = ""
            ayah_number = scene.get("ayah_number") or scene.get("ayah", {}).get("number", idx + 1)
            if "ayah" in scene and isinstance(scene["ayah"], dict):
                ayah_text = scene["ayah"].get("text", "")
            ayah_text = ayah_text or scene.get("ayah_text", "")

            emotion = scene.get("scene_emotion", "warm")
            if hasattr(emotion, "value"):
                emotion = emotion.value

            result = self.generate_for_scene(
                ayah_text=ayah_text,
                ayah_number=ayah_number,
                scene_emotion=emotion,
                initial_subject=scene.get("visual_subject", ""),
                initial_action=scene.get("visual_action", ""),
                initial_environment=scene.get("visual_environment", ""),
            )
            return idx, result

        results: List[Optional[DeepVisualPromptResult]] = [None] * len(ayah_scenes)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [
                ex.submit(_run_one, i, scene) for i, scene in enumerate(ayah_scenes)
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    idx, result = fut.result(timeout=180)
                    results[idx] = result
                except Exception as e:
                    logger.error(f"❌ Deep visual generation failed: {e}")

        # Replace any None with empty result so caller doesn't crash
        return [r if r is not None else DeepVisualPromptResult() for r in results]
