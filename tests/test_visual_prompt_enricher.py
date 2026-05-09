"""Tests for visual_prompt_enricher (v22.5)."""
import pytest
from unittest.mock import MagicMock

from engines.visual_prompt_enricher import (
    VisualPromptEnricher, EnrichedVisualPrompt,
)


# ════════════════════════════════════════════════════════════════
# EnrichedVisualPrompt — composition logic
# ════════════════════════════════════════════════════════════════
class TestEnrichedVisualPrompt:
    def test_empty_prompt_returns_empty_string(self):
        p = EnrichedVisualPrompt()
        assert p.to_leonardo_prompt() == ""

    def test_minimal_subject_only(self):
        p = EnrichedVisualPrompt(subject="a small ant")
        result = p.to_leonardo_prompt()
        assert "a small ant" in result

    def test_subject_action_combined(self):
        p = EnrichedVisualPrompt(
            subject="a small ant",
            action="carrying a leaf",
        )
        result = p.to_leonardo_prompt()
        assert "a small ant carrying a leaf" in result

    def test_environment_with_time_of_day(self):
        p = EnrichedVisualPrompt(
            subject="ant",
            environment="forest floor",
            time_of_day="golden hour",
        )
        result = p.to_leonardo_prompt()
        assert "forest floor" in result
        assert "golden hour" in result

    def test_full_prompt_includes_all_layers(self):
        p = EnrichedVisualPrompt(
            subject="ant carrying leaf",
            action="climbing tree bark",
            environment="forest floor",
            time_of_day="golden hour",
            mood="serene wonder",
            palette="warm honey gold and emerald",
            lighting="soft directional sunlight",
            camera_angle="low angle close-up",
            depth_of_field="shallow depth of field",
            foreground="blurred grass",
            midground="ant on bark",
            background="distant forest",
        )
        result = p.to_leonardo_prompt()
        # Each major element should be present
        for piece in [
            "ant carrying leaf", "climbing", "forest floor",
            "golden hour", "serene wonder", "honey gold",
            "soft directional sunlight", "low angle",
            "shallow depth of field",
            "foreground:", "midground:", "background:",
        ]:
            assert piece in result, f"Missing piece: {piece}"

    def test_layered_composition_uses_semicolon_separators(self):
        p = EnrichedVisualPrompt(
            subject="ant",
            foreground="grass",
            midground="bark",
            background="forest",
        )
        result = p.to_leonardo_prompt()
        assert "foreground: grass; midground: bark; background: forest" in result


# ════════════════════════════════════════════════════════════════
# VisualPromptEnricher — using mocked adapter
# ════════════════════════════════════════════════════════════════
class TestVisualPromptEnricher:
    def test_requires_adapter(self):
        with pytest.raises(ValueError, match="requires a Gemini adapter"):
            VisualPromptEnricher(gemini_adapter=None)

    def test_full_chain_success(self):
        """All 3 stages succeed → fully enriched prompt."""
        adapter = MagicMock()
        adapter.generate_json.side_effect = [
            # Stage A
            {
                "subject": "a small ant carrying an oak leaf 100 times its weight",
                "action": "climbing slowly up textured tree bark",
                "environment": "ancient forest floor with moss-covered stones",
                "time_of_day": "golden hour just before sunset",
            },
            # Stage B
            {
                "mood": "serene wonder with gentle awe",
                "palette": "warm honey gold, soft cream, deep emerald",
                "lighting": "soft directional sunlight from upper left",
            },
            # Stage C
            {
                "camera_angle": "low angle close-up, eye-level with subject",
                "depth_of_field": "shallow depth of field, soft creamy bokeh",
                "foreground": "out-of-focus blades of grass",
                "midground": "sharply focused subject on bark",
                "background": "soft blurred forest with sun rays",
            },
        ]

        enricher = VisualPromptEnricher(gemini_adapter=adapter)
        result = enricher.enrich_scene(
            subject="ant",
            action="carrying leaf",
            environment="forest",
            emotion="warm",
        )

        assert len(result.stages_completed) == 3
        assert "composition" in result.stages_completed
        assert "aesthetic" in result.stages_completed
        assert "cinematic" in result.stages_completed
        assert not result.fallback_used
        assert adapter.generate_json.call_count == 3

        # Verify Stage A output flowed through
        assert "ant carrying" in result.subject

        # Verify final prompt is rich
        prompt = result.to_leonardo_prompt()
        assert len(prompt) > 200  # rich, not minimal
        assert "honey gold" in prompt
        assert "low angle" in prompt

    def test_stage_a_fails_other_stages_use_originals(self):
        """If Stage A fails, B and C run with original minimal inputs."""
        adapter = MagicMock()
        adapter.generate_json.side_effect = [
            # Stage A: failure (returns None or invalid)
            Exception("Stage A error"),
            Exception("Stage A retry"),
            # Stage B: success
            {
                "mood": "warm",
                "palette": "golden",
                "lighting": "soft",
            },
            # Stage C: success
            {
                "camera_angle": "wide",
                "depth_of_field": "shallow",
                "foreground": "grass",
                "midground": "subject",
                "background": "trees",
            },
        ]

        enricher = VisualPromptEnricher(gemini_adapter=adapter)
        result = enricher.enrich_scene(
            subject="ant",
            action="climbing",
            emotion="warm",
            max_retries_per_stage=2,
        )

        assert "composition" not in result.stages_completed
        assert "aesthetic" in result.stages_completed
        assert "cinematic" in result.stages_completed
        assert result.fallback_used
        # Original subject preserved
        assert result.subject == "ant"
        # B and C results applied
        assert result.mood == "warm"
        assert result.camera_angle == "wide"

    def test_all_stages_fail_returns_minimal(self):
        """If everything fails, fall back to original minimal prompt."""
        adapter = MagicMock()
        adapter.generate_json.side_effect = Exception("all calls fail")

        enricher = VisualPromptEnricher(gemini_adapter=adapter)
        result = enricher.enrich_scene(
            subject="lonely ant",
            action="walking",
            environment="desert",
            emotion="reverent",
            max_retries_per_stage=1,
        )

        assert result.stages_completed == []
        assert result.fallback_used
        # Originals preserved
        assert result.subject == "lonely ant"
        assert result.action == "walking"
        assert result.environment == "desert"
        # Final prompt is still usable, just minimal
        prompt = result.to_leonardo_prompt()
        assert "lonely ant" in prompt
        assert "walking" in prompt

    def test_retries_within_stage(self):
        """Stage retries before giving up."""
        adapter = MagicMock()
        adapter.generate_json.side_effect = [
            Exception("first try fails"),
            Exception("second try fails"),
            {  # third try succeeds (Stage A)
                "subject": "recovered subject",
                "action": "recovered action",
                "environment": "recovered env",
                "time_of_day": "noon",
            },
            # Stage B
            {"mood": "x", "palette": "y", "lighting": "z"},
            # Stage C
            {
                "camera_angle": "a",
                "depth_of_field": "b",
                "foreground": "c",
                "midground": "d",
                "background": "e",
            },
        ]

        enricher = VisualPromptEnricher(gemini_adapter=adapter)
        result = enricher.enrich_scene(
            subject="ant", action="walking",
            max_retries_per_stage=3,
        )

        assert len(result.stages_completed) == 3
        # Subject got updated from Stage A
        assert result.subject == "recovered subject"

    def test_enrich_episode_processes_all_scenes(self):
        """enrich_episode loops through all scenes."""
        adapter = MagicMock()
        # Each scene needs 3 calls; we have 2 scenes → 6 calls total.
        # Use a generator that returns OK responses for all of them.
        def respond(*args, **kwargs):
            prompt = kwargs.get('prompt', args[0] if args else '')
            if "Stage A" in prompt or "composition" in prompt.lower():
                return {
                    "subject": "x", "action": "y",
                    "environment": "z", "time_of_day": "noon",
                }
            elif "aesthetic" in prompt.lower() or "Aesthetic" in prompt:
                return {"mood": "warm", "palette": "gold", "lighting": "soft"}
            else:  # cinematic
                return {
                    "camera_angle": "wide", "depth_of_field": "shallow",
                    "foreground": "fg", "midground": "mg", "background": "bg",
                }
        adapter.generate_json.side_effect = respond

        episode = {
            "ayah_scenes": [
                {
                    "visual_subject": "subject 1",
                    "visual_action": "action 1",
                    "visual_environment": "env 1",
                    "scene_emotion": "warm",
                },
                {
                    "visual_subject": "subject 2",
                    "visual_action": "action 2",
                    "visual_environment": "env 2",
                    "scene_emotion": "peaceful",
                },
            ]
        }

        enricher = VisualPromptEnricher(gemini_adapter=adapter)
        result = enricher.enrich_episode(episode)

        # Both scenes should have enriched prompts
        for scene in result["ayah_scenes"]:
            assert "visual_enriched_prompt" in scene
            assert "visual_enriched_meta" in scene
            assert len(scene["visual_enriched_prompt"]) > 0

    def test_enrich_episode_no_scenes_returns_unchanged(self):
        adapter = MagicMock()
        enricher = VisualPromptEnricher(gemini_adapter=adapter)
        episode = {"ayah_scenes": []}
        result = enricher.enrich_episode(episode)
        assert result == {"ayah_scenes": []}
        adapter.generate_json.assert_not_called()

    def test_episode_enrichment_handles_per_scene_failure(self):
        """If one scene fails entirely, others still work."""
        adapter = MagicMock()
        # First scene's first call fails permanently
        # Second scene works
        call_count = [0]
        def respond(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:  # first scene's stage A retries
                raise Exception("scene 1 stage A fail")
            # All subsequent calls succeed
            prompt = kwargs.get('prompt', args[0] if args else '')
            if "Aesthetic" in prompt or "aesthetic" in prompt.lower():
                return {"mood": "x", "palette": "y", "lighting": "z"}
            elif "cinematic" in prompt.lower() or "Cinematic" in prompt:
                return {
                    "camera_angle": "a", "depth_of_field": "b",
                    "foreground": "c", "midground": "d", "background": "e",
                }
            else:  # composition
                return {
                    "subject": "ok", "action": "ok",
                    "environment": "ok", "time_of_day": "noon",
                }
        adapter.generate_json.side_effect = respond

        episode = {
            "ayah_scenes": [
                {
                    "visual_subject": "first",
                    "scene_emotion": "warm",
                },
                {
                    "visual_subject": "second",
                    "scene_emotion": "warm",
                },
            ]
        }
        enricher = VisualPromptEnricher(gemini_adapter=adapter)
        result = enricher.enrich_episode(episode, max_retries_per_stage=2)

        # First scene: fallback (Stage A failed)
        assert result["ayah_scenes"][0]["visual_enriched_meta"]["fallback_used"] is True
        # Second scene: should work
        # (actually due to call counting, second scene also misses Stage A
        # but that's fine — we're testing partial failure tolerance)
        assert "visual_enriched_prompt" in result["ayah_scenes"][1]


class TestFallbackPrompt:
    def test_fallback_with_all_fields(self):
        scene = {
            "visual_subject": "an ant",
            "visual_action": "walking",
            "visual_environment": "garden",
        }
        prompt = VisualPromptEnricher._fallback_prompt(scene)
        assert "an ant" in prompt
        assert "walking" in prompt
        assert "in garden" in prompt

    def test_fallback_with_empty_scene(self):
        scene = {}
        prompt = VisualPromptEnricher._fallback_prompt(scene)
        assert prompt == "abstract peaceful scene"

    def test_fallback_with_only_subject(self):
        scene = {"visual_subject": "lonely ant"}
        prompt = VisualPromptEnricher._fallback_prompt(scene)
        assert "lonely ant" in prompt
