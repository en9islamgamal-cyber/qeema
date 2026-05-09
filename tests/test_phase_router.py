"""Tests for core/phase_router.py (v22.5 — delegated architecture).

PhaseRouter is now a thin shim:
  - It manages PhaseState (which phase is done, attempts, etc.)
  - It calls orchestrator.run(episode, phase=EpisodePhase.PHASE_X)
  - It captures the orchestrator's outputs into PhaseState

So these tests mock orchestrator.run() to simulate phase completion.
"""
import json
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from core.phase_state import Phase, PhaseState, PhaseStateManager
from core.phase_router import PhaseRouter, PhaseRunResult


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════
@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "phases"


@pytest.fixture
def state_manager(state_dir):
    return PhaseStateManager(state_dir)


@pytest.fixture
def temp_episodes_dir(tmp_path):
    """Mock temp_episodes location — used by PhaseRouter._load_script_json."""
    d = tmp_path / "temp_episodes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_success_report(phase_value: str = "phase_1"):
    """Build a mock successful EpisodeRunReport."""
    report = MagicMock()
    report.success = True
    report.error = None
    report.final_status = "completed"
    report.video_url = "https://youtube.com/watch?v=test"
    report.total_duration_sec = 5.0
    report.phase_run = phase_value
    report.next_phase = None
    return report


def _make_failure_report(error: str = "test failure"):
    report = MagicMock()
    report.success = False
    report.error = error
    report.final_status = "failed"
    report.video_url = None
    report.total_duration_sec = 0.5
    return report


@pytest.fixture
def mock_orchestrator(temp_episodes_dir):
    """Mock orchestrator with the SINGLE method PhaseRouter calls: run()."""
    orch = MagicMock()
    orch.run.return_value = _make_success_report()
    orch.paths.temp_episodes = temp_episodes_dir
    orch._load_phase_state.return_value = {
        "audio_map": {"intro": "/tmp/intro.mp3"},
        "mastered_map": {"intro": "/tmp/intro_mastered.mp3"},
    }
    return orch


@pytest.fixture
def episode_json_file(temp_episodes_dir):
    """Pre-create episode_001.json so _load_script_json finds something."""
    ep_path = temp_episodes_dir / "episode_001.json"
    ep_path.write_text(json.dumps({
        "title": "Test Episode",
        "ayah_scenes": [{"ayah_number": 1, "hook_text": "test"}],
    }), encoding="utf-8")
    return ep_path


# ════════════════════════════════════════════════════════════════
# PhaseRouter — core dispatch (delegated architecture)
# ════════════════════════════════════════════════════════════════
class TestPhaseRouter:
    def test_auto_phase_starts_at_1(
        self, mock_orchestrator, state_manager, episode_json_file,
    ):
        """Fresh episode with no state → runs Phase 1 (delegates to orchestrator)."""
        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )
        result = router.run_phase(episode_number=1, phase=None)
        assert result.success
        assert result.phase == Phase.PLANNING

        from core.models import EpisodePhase
        mock_orchestrator.run.assert_called_once()
        call_args = mock_orchestrator.run.call_args
        assert call_args[0][0] == 1 or call_args.kwargs.get("episode_number") == 1
        assert call_args.kwargs["phase"] == EpisodePhase.PHASE_1

    def test_phase_2_requires_phase_1(self, mock_orchestrator, state_manager):
        """Phase 2 explicitly requested without Phase 1 done → fails."""
        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )
        result = router.run_phase(episode_number=1, phase=Phase.ASSETS)
        assert not result.success
        assert "prerequisite" in result.error.lower()

    def test_phase_2_runs_after_phase_1(
        self, mock_orchestrator, state_manager, episode_json_file,
    ):
        """Phase 1 then Phase 2 — both delegate to orchestrator.run."""
        from core.models import EpisodePhase
        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )

        r1 = router.run_phase(episode_number=1, phase=Phase.PLANNING)
        assert r1.success

        r2 = router.run_phase(episode_number=1, phase=Phase.ASSETS)
        assert r2.success
        assert r2.phase == Phase.ASSETS

        assert mock_orchestrator.run.call_count == 2
        phases_called = [
            call.kwargs["phase"] for call in mock_orchestrator.run.call_args_list
        ]
        assert phases_called == [EpisodePhase.PHASE_1, EpisodePhase.PHASE_2]

    def test_phase_3_runs_after_phase_2(
        self, mock_orchestrator, state_manager, episode_json_file,
    ):
        from core.models import EpisodePhase
        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )
        router.run_phase(episode_number=1, phase=Phase.PLANNING)
        router.run_phase(episode_number=1, phase=Phase.ASSETS)
        r3 = router.run_phase(episode_number=1, phase=Phase.RENDER)
        assert r3.success
        last_call = mock_orchestrator.run.call_args_list[-1]
        assert last_call.kwargs["phase"] == EpisodePhase.PHASE_3

    def test_already_completed_episode_skipped(
        self, mock_orchestrator, state_manager,
    ):
        state = PhaseState(episode_number=1, phase=Phase.COMPLETED)
        state_manager.save(state)

        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )
        result = router.run_phase(episode_number=1, phase=None)
        assert result.success
        assert result.next_phase is None
        mock_orchestrator.run.assert_not_called()

    def test_max_retries_blocks_after_n_attempts(
        self, mock_orchestrator, state_manager,
    ):
        state = PhaseState(
            episode_number=1, phase=Phase.NONE, phase_1_attempts=2,
        )
        state_manager.save(state)

        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
            max_retries_per_phase=2,
        )
        result = router.run_phase(episode_number=1, phase=Phase.PLANNING)
        assert not result.success
        assert "exhausted" in result.error.lower()
        mock_orchestrator.run.assert_not_called()

    def test_phase_failure_is_recorded(
        self, mock_orchestrator, state_manager,
    ):
        """If orchestrator.run returns failure, PhaseRouter records it."""
        mock_orchestrator.run.return_value = _make_failure_report("script failed")

        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )
        result = router.run_phase(episode_number=1, phase=Phase.PLANNING)
        assert not result.success
        assert "script failed" in result.error

        state = state_manager.load(1)
        assert state.phase_1_attempts == 1
        assert state.phase == Phase.NONE

    def test_orchestrator_exception_propagates(
        self, mock_orchestrator, state_manager,
    ):
        """If orchestrator.run raises, PhaseRouter catches + records."""
        mock_orchestrator.run.side_effect = RuntimeError("orch crashed")

        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )
        result = router.run_phase(episode_number=1, phase=Phase.PLANNING)
        assert not result.success
        assert "orch crashed" in result.error

    def test_full_3_phase_run(
        self, mock_orchestrator, state_manager, episode_json_file,
    ):
        """End-to-end: run all 3 phases sequentially, all succeed."""
        router = PhaseRouter(
            orchestrator=mock_orchestrator,
            state_manager=state_manager,
        )

        r1 = router.run_phase(episode_number=1, phase=Phase.PLANNING)
        assert r1.success
        assert r1.next_phase == Phase.ASSETS

        r2 = router.run_phase(episode_number=1, phase=Phase.ASSETS)
        assert r2.success
        assert r2.next_phase == Phase.RENDER

        r3 = router.run_phase(episode_number=1, phase=Phase.RENDER)
        assert r3.success


# ════════════════════════════════════════════════════════════════
# _load_script_json helper
# ════════════════════════════════════════════════════════════════
class TestLoadScriptJson:
    def test_loads_existing_episode_json(
        self, mock_orchestrator, state_manager, temp_episodes_dir,
    ):
        ep_path = temp_episodes_dir / "episode_001.json"
        ep_path.write_text(
            json.dumps({"title": "test", "key": "value"}),
            encoding="utf-8",
        )

        router = PhaseRouter(
            orchestrator=mock_orchestrator, state_manager=state_manager,
        )
        data = router._load_script_json(1)
        assert data == {"title": "test", "key": "value"}

    def test_returns_empty_dict_when_missing(
        self, mock_orchestrator, state_manager,
    ):
        router = PhaseRouter(
            orchestrator=mock_orchestrator, state_manager=state_manager,
        )
        data = router._load_script_json(99)
        assert data == {}

    def test_returns_empty_dict_when_invalid_json(
        self, mock_orchestrator, state_manager, temp_episodes_dir,
    ):
        ep_path = temp_episodes_dir / "episode_001.json"
        ep_path.write_text("{not valid json", encoding="utf-8")

        router = PhaseRouter(
            orchestrator=mock_orchestrator, state_manager=state_manager,
        )
        data = router._load_script_json(1)
        assert data == {}
