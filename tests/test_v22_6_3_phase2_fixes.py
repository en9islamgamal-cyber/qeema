"""Tests for v22.6.3 (corrected) Phase 2 fixes.

The first cut of v22.6.3 wrote to the wrong phase-state system
(temp/episodes/_phase_state.json which is ephemeral). This test
file pins the corrected contract:

  Fix 1 — BatchTTSOut JSON Schema has no maxLength/maxItems
  Fix 2 — Legacy TTS fallback returns None when fallback_used=True
  Fix 3 — phase_router._run_phase_2 propagates _deep_visuals +
          _tts_directions from temp episode JSON into asset_paths
          (which IS persisted to state/phases/)
  Fix 4 — _run_pipeline rebuilds the script after deep visuals augment
          the episode JSON (so visual_prompt is populated for AI images)
  Fix 5 — _reload_episode_script restores temp JSON from PERSISTENT
          state (state/phases/ via PhaseStateManager.script_data),
          NOT from the orchestrator's ephemeral _phase_state.json

The Phase 3 hydration path is also covered: when running Phase 3
standalone on a fresh runner, the temp JSON gets reconstructed from
state.script_data + state.asset_paths so the rendered video has
populated visual_prompts and SSML directions.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engines.batch_engines import (
    BatchTTSOut,
    SegmentTTSOut,
)


# ════════════════════════════════════════════════════════════════
# Fix 1 — Schema state-space (Gemini 400 INVALID_ARGUMENT)
# ════════════════════════════════════════════════════════════════
class TestSchemaStateSpace:
    """Gemini's response_schema validator rejects schemas where the
    cumulative state space (array bound × per-element bounds) is too
    large."""

    def test_segment_tts_out_no_max_length_in_schema(self):
        schema = SegmentTTSOut.model_json_schema()
        properties = schema.get("properties", {})
        for field_name, field_schema in properties.items():
            assert "maxLength" not in field_schema, (
                f"Field {field_name} still has maxLength: {field_schema}"
            )

    def test_batch_tts_out_no_array_max_length_in_schema(self):
        schema = BatchTTSOut.model_json_schema()
        properties = schema.get("properties", {})
        directions_schema = properties.get("directions", {})
        assert "maxItems" not in directions_schema

    def test_batch_tts_still_validates_required_fields_at_python_level(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SegmentTTSOut()
        with pytest.raises(ValidationError):
            SegmentTTSOut(segment_id="x")

    def test_batch_tts_still_requires_at_least_one_direction(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BatchTTSOut(directions=[])


# ════════════════════════════════════════════════════════════════
# Fix 2 — Honest TTS fallback reporting
# ════════════════════════════════════════════════════════════════
class TestLegacyTtsHonestReporting:

    def test_legacy_tts_returns_none_when_fallback_used(self):
        from orchestrator import Orchestrator

        instance = MagicMock(spec=Orchestrator)
        adapter = MagicMock()
        instance._phase2_tts_gemini_adapter = MagicMock(return_value=adapter)

        fake_ed = MagicMock()
        fake_ed.fallback_used = True
        sd = MagicMock()
        sd.segment_id = "intro_text"
        sd.directed_text = "original text without SSML"
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

        assert result is None

    def test_legacy_tts_returns_dict_when_real_ssml_produced(self):
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
# Fix 3 — phase_router._run_phase_2 propagates Phase 2 outputs
# ════════════════════════════════════════════════════════════════
class TestPhaseRouterPropagatesPhase2Outputs:
    """The orchestrator writes _deep_visuals + _tts_directions into the
    temp episode JSON. The phase_router._run_phase_2 must read them and
    pass them through asset_paths so PhaseStateManager persists them
    in state/phases/."""

    def test_run_phase_2_extracts_deep_visuals_from_temp_json(
        self, tmp_path,
    ):
        from core.phase_router import PhaseRouter

        orch = MagicMock()
        ep_root = tmp_path / "episodes"
        ep_root.mkdir()
        orch.paths = MagicMock()
        orch.paths.temp_episodes = ep_root

        ep_json = ep_root / "episode_001.json"
        ep_json.write_text(
            json.dumps({
                "_deep_visuals": [
                    {"subject": "seed", "is_usable": True},
                    {"subject": "tree", "is_usable": True},
                ],
                "_tts_directions": {
                    "ayah_1.hook": {
                        "directed_text": 'تخيل <break time="300ms"/>',
                        "pace": "fast",
                        "pronunciation_notes": [],
                    },
                },
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        orch._load_phase_state.return_value = {
            "audio_map": {"intro": "/tmp/intro.mp3"},
            "mastered_map": {"intro": "/tmp/intro.m4a"},
        }
        report = MagicMock()
        report.success = True
        orch.run.return_value = report

        router = PhaseRouter.__new__(PhaseRouter)
        router._orchestrator = orch
        router._state_manager = MagicMock()

        state = MagicMock()
        state.script_data = {"episode_number": 1}

        outputs = router._run_phase_2(episode_number=1, state=state)

        asset_paths = outputs["asset_paths"]
        assert asset_paths["audio_map"]["intro"] == "/tmp/intro.mp3"
        assert asset_paths["mastered_map"]["intro"] == "/tmp/intro.m4a"
        assert "_deep_visuals" in asset_paths
        assert len(asset_paths["_deep_visuals"]) == 2
        assert asset_paths["_deep_visuals"][0]["subject"] == "seed"
        assert "_tts_directions" in asset_paths
        assert "ayah_1.hook" in asset_paths["_tts_directions"]

    def test_run_phase_2_handles_missing_deep_visuals_gracefully(
        self, tmp_path,
    ):
        from core.phase_router import PhaseRouter

        orch = MagicMock()
        ep_root = tmp_path / "episodes"
        ep_root.mkdir()
        orch.paths = MagicMock()
        orch.paths.temp_episodes = ep_root

        ep_json = ep_root / "episode_001.json"
        ep_json.write_text(
            json.dumps({"episode_number": 1}, ensure_ascii=False),
            encoding="utf-8",
        )
        orch._load_phase_state.return_value = {}
        report = MagicMock()
        report.success = True
        orch.run.return_value = report

        router = PhaseRouter.__new__(PhaseRouter)
        router._orchestrator = orch
        router._state_manager = MagicMock()
        state = MagicMock()
        state.script_data = {"episode_number": 1}

        outputs = router._run_phase_2(episode_number=1, state=state)

        asset_paths = outputs["asset_paths"]
        assert asset_paths["_deep_visuals"] == []
        assert asset_paths["_tts_directions"] == {}

    def test_run_phase_2_handles_missing_episode_json_gracefully(
        self, tmp_path,
    ):
        from core.phase_router import PhaseRouter

        orch = MagicMock()
        ep_root = tmp_path / "episodes"
        ep_root.mkdir()
        orch.paths = MagicMock()
        orch.paths.temp_episodes = ep_root
        orch._load_phase_state.return_value = {}
        report = MagicMock()
        report.success = True
        orch.run.return_value = report

        router = PhaseRouter.__new__(PhaseRouter)
        router._orchestrator = orch
        router._state_manager = MagicMock()
        state = MagicMock()
        state.script_data = {"episode_number": 1}

        outputs = router._run_phase_2(episode_number=1, state=state)
        assert outputs["asset_paths"]["_deep_visuals"] == []
        assert outputs["asset_paths"]["_tts_directions"] == {}


# ════════════════════════════════════════════════════════════════
# Fix 4 — Phase 2 main loop refreshes script after deep visuals
# ════════════════════════════════════════════════════════════════
class TestPhase2RefreshesScriptAfterDeepVisuals:
    """After _run_phase2_deep_visuals augments the episode JSON, the
    main pipeline loop must rebuild the in-memory script object so
    its ayah_scenes[i].visual_prompt fields reflect the fresh deep
    visuals (otherwise AI-image generation falls back to CSS)."""

    def test_orchestrator_source_calls_reload_after_deep_visuals(self):
        """Static check: between the deep-visuals call and the AI-image
        stage, the orchestrator main loop must call _reload_episode_script."""
        import inspect
        from orchestrator import Orchestrator

        run_src = inspect.getsource(Orchestrator._run_pipeline)
        deep_idx = run_src.find("_run_phase2_deep_visuals")
        refresh_idx = run_src.find(
            "refreshed = self._reload_episode_script", deep_idx,
        )
        ai_idx = run_src.find('"ai_images"', deep_idx)

        assert deep_idx > 0, "main loop must call _run_phase2_deep_visuals"
        assert refresh_idx > deep_idx, (
            "main loop must call _reload_episode_script AFTER deep visuals"
        )
        assert ai_idx > refresh_idx, (
            "the refresh must happen BEFORE the AI image stage so the "
            "script's visual_prompt fields are populated"
        )


# ════════════════════════════════════════════════════════════════
# Fix 5 — _reload_episode_script uses PERSISTENT state, not ephemeral
# ════════════════════════════════════════════════════════════════
class TestReloadFromPersistentState:
    """The first cut of v22.6.3 read snapshots from the orchestrator's
    ephemeral _phase_state.json. The corrected version reads from
    PhaseStateManager (state/phases/episode_NNN.json) which IS
    persisted across runs."""

    def test_reload_restores_temp_json_from_persistent_script_data(
        self, tmp_path,
    ):
        from orchestrator import Orchestrator

        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()
        ep_path = ep_dir / "episode_001.json"
        assert not ep_path.exists()

        persistent_script = {
            "episode_number": 1,
            "title": "اختبار",
            "ayah_scenes": [
                {"hook_text": "h", "ayah": {"number": 1, "text": "..."}},
            ],
        }
        fake_persistent_state = MagicMock()
        fake_persistent_state.script_data = persistent_script

        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir
        loaded_script = MagicMock(name="loaded_script")
        instance.script_engine = MagicMock()
        instance.script_engine.load_from_disk = MagicMock(
            return_value=loaded_script,
        )

        with patch("core.phase_state.PhaseStateManager") as psm_cls:
            psm = MagicMock()
            psm.load.return_value = fake_persistent_state
            psm_cls.return_value = psm

            result = Orchestrator._reload_episode_script(instance, 1)

        assert ep_path.exists()
        loaded = json.loads(ep_path.read_text(encoding="utf-8"))
        assert loaded["title"] == "اختبار"
        assert result is loaded_script

    def test_reload_does_not_overwrite_existing_temp_json(self, tmp_path):
        from orchestrator import Orchestrator

        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()
        ep_path = ep_dir / "episode_001.json"
        ep_path.write_text(
            '{"title": "in-memory", "ayah_scenes": []}',
            encoding="utf-8",
        )

        fake_persistent_state = MagicMock()
        fake_persistent_state.script_data = {"title": "from-state"}

        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir
        instance.script_engine = MagicMock()
        instance.script_engine.load_from_disk = MagicMock(
            return_value=MagicMock(),
        )

        with patch("core.phase_state.PhaseStateManager") as psm_cls:
            psm = MagicMock()
            psm.load.return_value = fake_persistent_state
            psm_cls.return_value = psm
            Orchestrator._reload_episode_script(instance, 1)

        existing = json.loads(ep_path.read_text(encoding="utf-8"))
        assert existing["title"] == "in-memory"

    def test_reload_falls_through_to_regenerate_when_state_is_empty(
        self, tmp_path,
    ):
        from orchestrator import Orchestrator

        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir()

        fake_persistent_state = MagicMock()
        fake_persistent_state.script_data = None

        instance = MagicMock(spec=Orchestrator)
        instance.paths = MagicMock()
        instance.paths.temp_episodes = ep_dir
        instance.script_engine = MagicMock()
        instance.script_engine.load_from_disk = MagicMock(return_value=None)
        instance.hook_optimizer = MagicMock()
        instance._current_strategy = MagicMock()

        with patch("core.phase_state.PhaseStateManager") as psm_cls:
            psm = MagicMock()
            psm.load.return_value = fake_persistent_state
            psm_cls.return_value = psm

            with patch(
                "engines.script_engine_unified.UnifiedScriptEngine"
            ) as unified_cls:
                unified = MagicMock()
                unified.generate.return_value = MagicMock(name="regenerated")
                unified_cls.return_value = unified
                result = Orchestrator._reload_episode_script(instance, 1)

        assert result is not None
        assert not (ep_dir / "episode_001.json").exists()
        unified.generate.assert_called_once()


# ════════════════════════════════════════════════════════════════
# End-to-end: cross-run survivability via PhaseStateManager
# ════════════════════════════════════════════════════════════════
class TestCrossRunSurvivability:
    """The whole point of v22.6.3 is that Phase 2 outputs (visuals,
    TTS directions) survive when Phase 3 runs on a fresh runner with
    an empty disk. This test exercises the PhaseStateManager directly
    to confirm the round-trip works."""

    def test_persistent_state_round_trip_survives_temp_wipe(self, tmp_path):
        """Save → wipe in-memory → load → verify all Phase 2 outputs survive."""
        from core.phase_state import PhaseStateManager, Phase

        state_dir = tmp_path / "state" / "phases"
        state_dir.mkdir(parents=True)
        psm = PhaseStateManager(state_dir)

        state = psm.load(1)
        script = {
            "episode_number": 1,
            "title": "تجربة",
            "ayah_scenes": [
                {"ayah": {"number": 1, "text": "..."}, "hook_text": "h"},
            ],
        }
        state.script_data = script
        state = psm.mark_phase_complete(
            state, phase=Phase.PLANNING,
            outputs={"script_data": script},
        )

        state = psm.mark_phase_complete(
            state, phase=Phase.ASSETS,
            outputs={
                "asset_paths": {
                    "audio_map": {"intro": "x.mp3"},
                    "mastered_map": {"intro": "x.m4a"},
                    "_deep_visuals": [{"subject": "seed", "is_usable": True}],
                    "_tts_directions": {
                        "intro_text": {
                            "directed_text": 'أهلاً <break/>',
                            "pace": "normal",
                        },
                    },
                },
            },
        )

        # Simulate runner restart: NEW PhaseStateManager pointing to same dir
        psm_2 = PhaseStateManager(state_dir)
        reloaded = psm_2.load(1)

        assert reloaded.script_data["title"] == "تجربة"
        assert reloaded.asset_paths["audio_map"]["intro"] == "x.mp3"
        assert reloaded.asset_paths["_deep_visuals"][0]["subject"] == "seed"
        assert "intro_text" in reloaded.asset_paths["_tts_directions"]
        assert (
            reloaded.asset_paths["_tts_directions"]["intro_text"]["pace"]
            == "normal"
        )


# ════════════════════════════════════════════════════════════════
# Phase 3 hydration: temp JSON gets reconstructed from persistent state
# ════════════════════════════════════════════════════════════════
class TestPhase3Hydration:
    """When Phase 3 runs standalone on a fresh runner, the orchestrator
    main loop must reconstruct the temp episode JSON from persistent
    state's script_data + asset_paths so the rebuilt script has
    populated visual_prompts and SSML directions."""

    def test_phase3_main_loop_hydrates_temp_json_from_persistent_state(self):
        """Static source check: the Phase 3 standalone branch must call
        PhaseStateManager and hydrate temp JSON before _reload_episode_script."""
        import inspect
        from orchestrator import Orchestrator

        run_src = inspect.getsource(Orchestrator._run_pipeline)
        # Find Phase 3 standalone branch marker
        phase3_idx = run_src.find('Phase 3 standalone')
        assert phase3_idx > 0

        # The hydration block must reference both PhaseStateManager AND
        # asset_paths/_deep_visuals
        psm_idx = run_src.find('PhaseStateManager', phase3_idx)
        deep_idx = run_src.find('_deep_visuals', phase3_idx)
        tts_idx = run_src.find('_tts_directions', phase3_idx)
        reload_idx = run_src.find(
            '_reload_episode_script(episode_number)', phase3_idx,
        )

        assert psm_idx > 0, (
            "Phase 3 must use PhaseStateManager (not the orchestrator's "
            "ephemeral _load_phase_state)"
        )
        assert deep_idx > 0, "Phase 3 hydration must include _deep_visuals"
        assert tts_idx > 0, "Phase 3 hydration must include _tts_directions"
        # Hydration must happen BEFORE _reload_episode_script
        assert deep_idx < reload_idx, (
            "Phase 3 must hydrate temp JSON before _reload_episode_script "
            "(otherwise the script is rebuilt without visual_prompts)"
        )
