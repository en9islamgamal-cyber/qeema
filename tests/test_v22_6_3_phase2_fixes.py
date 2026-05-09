"""Tests for v22.6.3 Phase 2 fixes.

Each test pins the contract of one fix from the v22.6.3 hotfix:

  Fix 1 — BatchTTSOut schema doesn't trip Gemini's state-space limit
  Fix 2 — Legacy TTSDirector fallback is no longer reported as success
  Fix 3 — _deep_visuals + _tts_directions persist into phase state
  Fix 4 — Script is rebuilt from disk after deep visuals augment it
  Fix 5 — Phase 2 reload restores episode JSON from snapshot, no regen
"""
from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from engines.batch_engines import (
    AyahVisualOut,
    BatchTTSOut,
    BatchVisualOut,
    SegmentTTSOut,
)


# ════════════════════════════════════════════════════════════════
# Fix 1 — Schema state-space (Gemini 400 INVALID_ARGUMENT)
# ════════════════════════════════════════════════════════════════
class TestSchemaStateSpace:
    """Gemini's response_schema validator rejects schemas where the
    cumulative state space (array bound × per-element bounds) is too
    large. The 400 we hit on episode 1 was caused by SegmentTTSOut
    fields × array max_length=80. v22.6.3 strips those bounds from
    the JSON-Schema generation while keeping Pydantic-side validation."""

    def test_segment_tts_out_no_max_length_in_schema(self):
        """The JSON schema sent to Gemini must not advertise per-string
        max_length on segment fields — that's what triggered the 400."""
        schema = SegmentTTSOut.model_json_schema()
        # Walk all string properties and confirm no 'maxLength' keyword
        properties = schema.get("properties", {})
        for field_name, field_schema in properties.items():
            assert "maxLength" not in field_schema, (
                f"Field {field_name} still has maxLength in JSON schema "
                f"(state-space risk for Gemini): {field_schema}"
            )

    def test_batch_tts_out_no_array_max_length_in_schema(self):
        """The directions array must not have a maxItems bound either —
        the state-space explosion was bound × per-element bounds."""
        schema = BatchTTSOut.model_json_schema()
        properties = schema.get("properties", {})
        directions_schema = properties.get("directions", {})
        # Either the bound is gone, or it's 0 (which Pydantic doesn't emit
        # by default when the field has no max_length). The point is
        # Gemini doesn't see it.
        assert "maxItems" not in directions_schema, (
            f"directions still has maxItems: {directions_schema}"
        )

    def test_batch_tts_still_validates_required_fields_at_python_level(self):
        """We dropped schema-side bounds but Pydantic-side validation of
        REQUIRED fields must still work. The schema must reject
        SegmentTTSOut without segment_id/directed_text/pace."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SegmentTTSOut()  # all required fields missing
        with pytest.raises(ValidationError):
            SegmentTTSOut(segment_id="x")  # missing directed_text + pace

    def test_batch_tts_still_requires_at_least_one_direction(self):
        """The min_length=1 on directions array is preserved — empty
        BatchTTSOut would mean no segments to synthesize, which is wrong."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BatchTTSOut(directions=[])


# ════════════════════════════════════════════════════════════════
# Fix 2 — Honest TTS fallback reporting
# ════════════════════════════════════════════════════════════════
class TestLegacyTtsHonestReporting:
    """Before v22.6.3, the orchestrator's _legacy_tts returned the
    fallback dict (with directed_text == original_text) and the caller
    logged 'N segments directed with SSML' — false. Now _legacy_tts
    returns None when fallback_used is True, so the caller logs honestly.
    """

    def test_legacy_tts_returns_none_when_fallback_used(self):
        from orchestrator import Orchestrator

        instance = MagicMock(spec=Orchestrator)
        adapter = MagicMock()
        instance._phase2_tts_gemini_adapter = MagicMock(return_value=adapter)

        # Mock TTSDirector to return a fallback EpisodeDirection
        fake_episode_direction = MagicMock()
        fake_episode_direction.fallback_used = True
        # segments is non-empty but they're all fallback (no real SSML)
        sd = MagicMock()
        sd.segment_id = "intro_text"
        sd.directed_text = "original text without SSML"
        sd.pace = "normal"
        sd.pronunciation_notes = []
        fake_episode_direction.segments = {"intro_text": sd}

        with patch("engines.tts_director.TTSDirector") as director_cls:
            director = MagicMock()
            director.direct_episode.return_value = fake_episode_direction
            director_cls.return_value = director

            result = Orchestrator._legacy_tts(
                instance, episode_data={"intro_text": "x"},
            )

        assert result is None, (
            "Fallback EpisodeDirection must be reported as failure, "
            "not as 'directions produced'"
        )

    def test_legacy_tts_returns_dict_when_real_ssml_produced(self):
        """Sanity: when fallback_used=False, legacy_tts returns the
        directions dict (it actually worked)."""
        from orchestrator import Orchestrator

        instance = MagicMock(spec=Orchestrator)
        adapter = MagicMock()
        instance._phase2_tts_gemini_adapter = MagicMock(return_value=adapter)

        fake_ed = MagicMock()
        fake_ed.fallback_used = False
        sd = MagicMock()
        sd.segment_id = "intro_text"
        sd.directed_text = 'تخيل <break time="300ms"/> معايا'
        sd.pace = "normal"
        sd.pronunciation_notes = []
        fake_ed.segments = {"intro_text": sd}

        with patch("engines.tts_director.TTSDirector") as director_cls:
            director = MagicMock()
            director.direct_episode.return_value = fake_ed
            director_cls.return_value = director

            result = Orchestrator._legacy_tts(
                instance, episode_data={"intro_text": "x"},
            )

        assert result is not None
        assert "intro_text" in result
        assert "<break" in result["intro_text"]["directed_text"]


# ════════════════════════════════════════════════════════════════
# Fix 3 — _deep_visuals + _tts_directions persist into phase state
# ════════════════════════════════════════════════════════════════
class TestPhase2OutputsPersistedToState:
    """The temp/episodes/episode_001.json file is wiped between GitHub
    Actions runs. Phase 3 needs to read the visuals + TTS directions
    after Phase 2 finishes — therefore they MUST be in the cached
    phase state, not just the temp JSON."""

    def test_run_phase2_deep_visuals_calls_save_phase_state(
        self, tmp_path,
    ):
        """The deep-visual handler must invoke _save_phase_state with
        deep_visuals=... in addition to writing the temp JSON."""
        from orchestrator import Orchestrator

        # Build minimal episode JSON on disk
        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()
        ep_path = ep_dir / "episode_001.json"
        ep_path.write_text(
            json.dumps({
                "episode_number": 1,
                "ayah_scenes": [
                    {"ayah": {"number": 1, "text": "بسم الله"},
                     "explain_text": "e", "story_text": "s",
                     "scene_emotion": "warm"},
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir

        # _try_batch_visual_prompts returns a usable payload
        deep_payload = [
            {"subject": "seed", "is_usable": True, "layers_completed": 3,
             "action": "a", "environment": "e", "time_of_day": "dawn",
             "mood": "m", "color_palette": "p", "lighting_direction": "l",
             "atmospheric_elements": "ae", "camera_angle": "ca",
             "depth_of_field": "dof", "foreground": "fg",
             "midground": "mg", "background": "bg", "focal_point": "fp"},
        ]
        instance._try_batch_visual_prompts = MagicMock(return_value=deep_payload)

        Orchestrator._run_phase2_deep_visuals(
            instance, episode_number=1, script=MagicMock(),
        )

        # Verify _save_phase_state was called with deep_visuals
        instance._save_phase_state.assert_called_once()
        call_kwargs = instance._save_phase_state.call_args.kwargs
        assert "deep_visuals" in call_kwargs
        assert call_kwargs["deep_visuals"] == deep_payload

    def test_run_phase2_tts_director_calls_save_phase_state(
        self, tmp_path,
    ):
        from orchestrator import Orchestrator

        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()
        ep_path = ep_dir / "episode_001.json"
        ep_path.write_text(
            json.dumps({
                "intro_text": "أهلاً",
                "ayah_scenes": [
                    {"hook_text": "h", "story_text": "s", "moral_text": "m"},
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir

        directions_payload = {
            "intro_text": {
                "directed_text": 'أهلاً <break time="300ms"/>',
                "pace": "normal",
                "pronunciation_notes": [],
            },
        }
        instance._try_batch_tts = MagicMock(return_value=directions_payload)

        Orchestrator._run_phase2_tts_director(
            instance, episode_number=1, script=MagicMock(),
        )

        instance._save_phase_state.assert_called_once()
        call_kwargs = instance._save_phase_state.call_args.kwargs
        assert "tts_directions" in call_kwargs
        assert call_kwargs["tts_directions"] == directions_payload


# ════════════════════════════════════════════════════════════════
# Fix 5 — _reload_episode_script restores temp JSON from snapshot
# ════════════════════════════════════════════════════════════════
class TestReloadFromSnapshot:
    """If the temp episode JSON was wiped (typical between GitHub Actions
    runs), _reload_episode_script must restore it from the phase state
    snapshot BEFORE attempting to load — otherwise we burn a Gemini call
    regenerating identical content."""

    def test_snapshot_restored_when_temp_json_missing(self, tmp_path):
        from orchestrator import Orchestrator

        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()
        # Temp JSON does NOT exist initially
        ep_path = ep_dir / "episode_001.json"
        assert not ep_path.exists()

        # But phase state has a snapshot
        snapshot = {
            "episode_number": 1,
            "title": "تجربة",
            "ayah_scenes": [
                {"hook_text": "h", "ayah": {"number": 1, "text": "..."}},
            ],
        }

        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir
        instance._load_phase_state = MagicMock(
            return_value={"episode_json_snapshot": snapshot}
        )
        # Simulate load_from_disk reading the now-restored JSON
        loaded_script = MagicMock()
        instance.script_engine = MagicMock()
        instance.script_engine.load_from_disk = MagicMock(
            return_value=loaded_script,
        )

        result = Orchestrator._reload_episode_script(instance, 1)

        # The temp JSON should now exist (restored from snapshot)
        assert ep_path.exists()
        # And contains exactly the snapshot
        assert json.loads(ep_path.read_text(encoding="utf-8")) == snapshot
        # load_from_disk was called on the restored file (not regenerated)
        assert result is loaded_script

    def test_snapshot_skipped_when_temp_json_already_exists(self, tmp_path):
        """We don't overwrite a present temp JSON — within the same run,
        the temp file is the source of truth."""
        from orchestrator import Orchestrator

        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()
        ep_path = ep_dir / "episode_001.json"
        ep_path.write_text(
            '{"episode_number": 1, "title": "in-memory"}',
            encoding="utf-8",
        )

        snapshot = {"episode_number": 1, "title": "from-snapshot"}

        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir
        instance._load_phase_state = MagicMock(
            return_value={"episode_json_snapshot": snapshot},
        )
        instance.script_engine = MagicMock()
        instance.script_engine.load_from_disk = MagicMock(
            return_value=MagicMock(),
        )

        Orchestrator._reload_episode_script(instance, 1)

        # File NOT overwritten — still has "in-memory"
        existing = json.loads(ep_path.read_text(encoding="utf-8"))
        assert existing["title"] == "in-memory"

    def test_no_snapshot_no_temp_json_falls_through_to_load_from_disk(
        self, tmp_path,
    ):
        """If no snapshot AND no temp JSON, we still try load_from_disk
        (which will return None) and ultimately the regenerate fallback."""
        from orchestrator import Orchestrator

        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()

        # Use spec_set=False MagicMock so we can attach hook_optimizer freely
        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir
        instance._load_phase_state = MagicMock(return_value={})
        instance.script_engine = MagicMock()
        instance.script_engine.load_from_disk = MagicMock(return_value=None)
        # _reload_episode_script reads self.hook_optimizer when falling
        # through to UnifiedScriptEngine
        instance.hook_optimizer = MagicMock()
        instance._current_strategy = MagicMock()

        # The fallback path uses UnifiedScriptEngine — patch that to avoid
        # actually generating anything in this test.
        with patch(
            "engines.script_engine_unified.UnifiedScriptEngine"
        ) as unified_cls:
            unified = MagicMock()
            unified.generate.return_value = MagicMock(name="regenerated")
            unified_cls.return_value = unified
            result = Orchestrator._reload_episode_script(instance, 1)

        # We did fall through to regeneration
        assert result is not None
        unified.generate.assert_called_once()
