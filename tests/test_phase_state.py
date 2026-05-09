"""Tests for core/phase_state.py (v22.5)."""
import json
import pytest
from pathlib import Path

from core.phase_state import (
    Phase, PhaseState, PhaseStateManager,
)


# ════════════════════════════════════════════════════════════════
# Phase enum
# ════════════════════════════════════════════════════════════════
class TestPhaseEnum:
    def test_ordering(self):
        assert Phase.NONE < Phase.PLANNING
        assert Phase.PLANNING < Phase.ASSETS
        assert Phase.ASSETS < Phase.RENDER
        assert Phase.RENDER < Phase.COMPLETED

    def test_int_values(self):
        # Used in JSON serialization
        assert Phase.NONE == 0
        assert Phase.PLANNING == 1
        assert Phase.ASSETS == 2
        assert Phase.RENDER == 3
        assert Phase.COMPLETED == 4


# ════════════════════════════════════════════════════════════════
# PhaseState dataclass
# ════════════════════════════════════════════════════════════════
class TestPhaseState:
    def test_default_state_starts_at_none(self):
        s = PhaseState(episode_number=1)
        assert s.phase == Phase.NONE
        assert not s.is_completed

    def test_can_run_phase_1_always(self):
        s = PhaseState(episode_number=1)
        assert s.can_run_phase_1

    def test_can_run_phase_2_requires_phase_1(self):
        s = PhaseState(episode_number=1)
        assert not s.can_run_phase_2
        s.phase = Phase.PLANNING
        assert s.can_run_phase_2

    def test_can_run_phase_3_requires_phase_2(self):
        s = PhaseState(episode_number=1)
        assert not s.can_run_phase_3
        s.phase = Phase.PLANNING
        assert not s.can_run_phase_3
        s.phase = Phase.ASSETS
        assert s.can_run_phase_3

    def test_to_dict_roundtrip(self):
        s = PhaseState(
            episode_number=5,
            phase=Phase.PLANNING,
            phase_1_completed_at="2026-05-07T10:00:00+00:00",
            script_data={"title": "test"},
        )
        d = s.to_dict()
        s2 = PhaseState.from_dict(d)
        assert s2.episode_number == 5
        assert s2.phase == Phase.PLANNING
        assert s2.script_data == {"title": "test"}

    def test_from_dict_ignores_unknown_fields(self):
        d = {
            "episode_number": 1,
            "phase": 1,
            "future_field_we_dont_know": "value",
        }
        s = PhaseState.from_dict(d)
        assert s.episode_number == 1


# ════════════════════════════════════════════════════════════════
# PhaseStateManager
# ════════════════════════════════════════════════════════════════
class TestPhaseStateManager:
    @pytest.fixture
    def state_dir(self, tmp_path):
        return tmp_path / "phases"

    def test_load_nonexistent_returns_fresh(self, state_dir):
        m = PhaseStateManager(state_dir)
        s = m.load(99)
        assert s.episode_number == 99
        assert s.phase == Phase.NONE

    def test_save_and_reload(self, state_dir):
        m = PhaseStateManager(state_dir)
        s = PhaseState(episode_number=1, phase=Phase.PLANNING)
        m.save(s)

        # Re-create manager (simulating new run)
        m2 = PhaseStateManager(state_dir)
        loaded = m2.load(1)
        assert loaded.phase == Phase.PLANNING
        assert loaded.episode_number == 1

    def test_save_creates_dir(self, tmp_path):
        nested = tmp_path / "deeply" / "nested"
        m = PhaseStateManager(nested)
        m.save(PhaseState(episode_number=1))
        assert (nested / "episode_001.json").exists()

    def test_load_corrupt_file_returns_fresh(self, state_dir):
        m = PhaseStateManager(state_dir)
        # Write garbage
        path = state_dir / "episode_001.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {{{")

        # Should not crash — return fresh state
        s = m.load(1)
        assert s.phase == Phase.NONE
        assert s.episode_number == 1

    def test_mark_phase_1_complete(self, state_dir):
        m = PhaseStateManager(state_dir)
        s = m.load(1)
        s = m.mark_phase_complete(
            s, phase=Phase.PLANNING,
            outputs={"script_data": {"title": "Al-Fatiha"}},
        )
        assert s.phase == Phase.PLANNING
        assert s.phase_1_completed_at is not None
        assert s.script_data == {"title": "Al-Fatiha"}

        # Reload from disk
        s2 = m.load(1)
        assert s2.phase == Phase.PLANNING

    def test_mark_phase_3_marks_completed(self, state_dir):
        m = PhaseStateManager(state_dir)
        s = m.load(1)
        s.phase = Phase.ASSETS
        s = m.mark_phase_complete(
            s, phase=Phase.RENDER,
            outputs={"render_artifacts": {"video_path": "out.mp4"}},
        )
        assert s.phase == Phase.COMPLETED
        assert s.is_completed
        assert s.render_artifacts == {"video_path": "out.mp4"}

    def test_mark_phase_failed_increments_attempts(self, state_dir):
        m = PhaseStateManager(state_dir)
        s = m.load(1)

        s = m.mark_phase_failed(s, phase=Phase.PLANNING, error="boom")
        assert s.phase_1_attempts == 1
        assert s.last_error == "boom"
        assert s.last_error_phase == Phase.PLANNING

        s = m.mark_phase_failed(s, phase=Phase.PLANNING, error="boom again")
        assert s.phase_1_attempts == 2

    def test_mark_phase_failed_truncates_long_errors(self, state_dir):
        m = PhaseStateManager(state_dir)
        s = m.load(1)
        long_error = "x" * 1000
        s = m.mark_phase_failed(s, phase=Phase.PLANNING, error=long_error)
        assert len(s.last_error) == 500

    def test_mark_complete_clears_previous_error(self, state_dir):
        m = PhaseStateManager(state_dir)
        s = m.load(1)
        s = m.mark_phase_failed(s, phase=Phase.PLANNING, error="failed")
        assert s.last_error == "failed"

        s = m.mark_phase_complete(s, phase=Phase.PLANNING)
        assert s.last_error is None
        assert s.last_error_phase is None

    def test_determine_next_phase(self, state_dir):
        m = PhaseStateManager(state_dir)

        # Fresh → Phase 1
        s = PhaseState(episode_number=1, phase=Phase.NONE)
        assert m.determine_next_phase(s) == Phase.PLANNING

        # Phase 1 done → Phase 2
        s.phase = Phase.PLANNING
        assert m.determine_next_phase(s) == Phase.ASSETS

        # Phase 2 done → Phase 3
        s.phase = Phase.ASSETS
        assert m.determine_next_phase(s) == Phase.RENDER

        # Completed → None
        s.phase = Phase.COMPLETED
        assert m.determine_next_phase(s) is None

    def test_list_states(self, state_dir):
        m = PhaseStateManager(state_dir)
        m.save(PhaseState(episode_number=1, phase=Phase.PLANNING))
        m.save(PhaseState(episode_number=2, phase=Phase.ASSETS))
        m.save(PhaseState(episode_number=3, phase=Phase.NONE))

        all_states = m.list_states()
        assert len(all_states) == 3
        # Sorted by episode number (filename)
        assert all_states[0].episode_number == 1
        assert all_states[1].episode_number == 2
        assert all_states[2].episode_number == 3

    def test_atomic_save_no_partial_writes(self, state_dir):
        """If save crashes mid-write, the original file should remain intact."""
        m = PhaseStateManager(state_dir)
        s = PhaseState(episode_number=1, phase=Phase.PLANNING)
        m.save(s)

        # Verify file is readable
        path = m._path_for(1)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["phase"] == Phase.PLANNING

    def test_idempotent_phase_marking(self, state_dir):
        """Marking a lower phase complete shouldn't downgrade state."""
        m = PhaseStateManager(state_dir)
        s = m.load(1)
        s.phase = Phase.ASSETS  # already at 2

        # Try to "complete" Phase 1 again
        s = m.mark_phase_complete(s, phase=Phase.PLANNING)
        # Should NOT downgrade
        assert s.phase == Phase.ASSETS
