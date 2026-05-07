"""Tests for v22.5: phase split + Gemini tafsir reviewer.

These tests verify the new phase architecture without actually running
the full pipeline (no Gemini/Leonardo/ElevenLabs calls). They focus on:
  - EpisodePhase enum logic
  - Phase status transitions
  - APIKeysConfig key splitting
  - GeminiReviewer interface compatibility
  - Phase state save/load
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.models import EpisodePhase, EpisodeStatus
from core.config import APIKeysConfig
from engines.tafsir_validator import (
    TafsirValidator, ClaudeReviewer, GeminiReviewer, HeuristicReviewer,
)


# ════════════════════════════════════════════════════════════════
# EpisodePhase enum
# ════════════════════════════════════════════════════════════════
class TestEpisodePhaseEnum:
    def test_all_phases_exist(self):
        assert EpisodePhase.PHASE_1.value == "phase_1"
        assert EpisodePhase.PHASE_2.value == "phase_2"
        assert EpisodePhase.PHASE_3.value == "phase_3"
        assert EpisodePhase.AUTO.value == "auto"
        assert EpisodePhase.ALL.value == "all"

    def test_constructible_from_string(self):
        assert EpisodePhase("phase_1") == EpisodePhase.PHASE_1
        assert EpisodePhase("auto") == EpisodePhase.AUTO

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            EpisodePhase("phase_99")


class TestNextPhaseLogic:
    def test_pending_starts_phase1(self):
        nxt = EpisodePhase.next_phase_for_status(EpisodeStatus.PENDING)
        assert nxt == EpisodePhase.PHASE_1

    def test_processing_runs_phase1(self):
        nxt = EpisodePhase.next_phase_for_status(EpisodeStatus.PROCESSING)
        assert nxt == EpisodePhase.PHASE_1

    def test_script_ready_advances_to_phase2(self):
        nxt = EpisodePhase.next_phase_for_status(EpisodeStatus.SCRIPT_READY)
        assert nxt == EpisodePhase.PHASE_2

    def test_assets_ready_advances_to_phase3(self):
        nxt = EpisodePhase.next_phase_for_status(EpisodeStatus.ASSETS_READY)
        assert nxt == EpisodePhase.PHASE_3

    def test_completed_does_not_auto_restart(self):
        nxt = EpisodePhase.next_phase_for_status(EpisodeStatus.COMPLETED)
        # Returns PHASE_1 default — caller should check status before running
        assert nxt == EpisodePhase.PHASE_1


# ════════════════════════════════════════════════════════════════
# EpisodeStatus phase markers
# ════════════════════════════════════════════════════════════════
class TestEpisodeStatusPhases:
    def test_script_ready_exists(self):
        assert EpisodeStatus.SCRIPT_READY.value == "script_ready"

    def test_assets_ready_exists(self):
        assert EpisodeStatus.ASSETS_READY.value == "assets_ready"

    def test_awaiting_phase2_review_exists(self):
        assert EpisodeStatus.AWAITING_PHASE2_REVIEW.value == "awaiting_phase2_review"


# ════════════════════════════════════════════════════════════════
# APIKeysConfig key splitting
# ════════════════════════════════════════════════════════════════
class TestAPIKeySplits:
    def _build(self, *gemini_keys: str) -> APIKeysConfig:
        return APIKeysConfig(
            gemini_keys=gemini_keys,
            groq='', cohere='', anthropic='', elevenlabs='',
            elevenlabs_voice_id='',
            leonardo='', leonardo_character_ref='',
            supabase_url='', supabase_key='',
            youtube_client_id='', youtube_client_secret='',
            youtube_refresh_token='',
        )

    def test_three_keys_split_two_one(self):
        k = self._build('k1', 'k2', 'k3')
        assert k.script_pool_keys == ('k1', 'k2')
        assert k.tafsir_review_key == 'k3'

    def test_two_keys_split_one_one(self):
        k = self._build('k1', 'k2')
        assert k.script_pool_keys == ('k1',)
        assert k.tafsir_review_key == 'k2'

    def test_one_key_shared(self):
        k = self._build('only')
        assert k.script_pool_keys == ('only',)
        assert k.tafsir_review_key == 'only'

    def test_no_keys_empty(self):
        k = self._build()
        assert k.script_pool_keys == ()
        assert k.tafsir_review_key == ''

    def test_four_keys_keeps_three_for_script(self):
        k = self._build('k1', 'k2', 'k3', 'k4')
        assert k.script_pool_keys == ('k1', 'k2', 'k3')
        assert k.tafsir_review_key == 'k4'


# ════════════════════════════════════════════════════════════════
# GeminiReviewer interface compatibility with ClaudeReviewer
# ════════════════════════════════════════════════════════════════
class TestGeminiReviewerInterface:
    def test_same_review_signature_as_claude(self):
        import inspect
        gemini_sig = inspect.signature(GeminiReviewer.review)
        claude_sig = inspect.signature(ClaudeReviewer.review)
        assert list(gemini_sig.parameters.keys()) == list(claude_sig.parameters.keys())

    def test_instantiate_without_genai_pkg_does_not_crash(self):
        # If google-genai is missing, should not raise — just disable
        reviewer = GeminiReviewer(gemini_api_key="fake")
        # _available may be True or False depending on env; either way, no exception

    def test_review_returns_failure_when_unavailable(self):
        reviewer = GeminiReviewer(gemini_api_key="")
        reviewer._available = False  # force unavailable
        result = reviewer.review(
            ayah_text="test", surah_name="test", ayah_number=1,
            llm_explanation="test", llm_analogy="test",
            authentic_tafsir="test",
        )
        assert not result.passed
        assert result.confidence == 0.0


# ════════════════════════════════════════════════════════════════
# TafsirValidator chain logic
# ════════════════════════════════════════════════════════════════
class TestTafsirValidatorChain:
    def test_no_keys_uses_heuristic_only(self):
        v = TafsirValidator(anthropic_key=None, gemini_review_key=None)
        assert v._claude_reviewer is None
        assert v._gemini_reviewer is None
        assert v._heuristic_reviewer is not None

    def test_only_gemini_key(self):
        v = TafsirValidator(anthropic_key=None, gemini_review_key="fake-gemini")
        assert v._claude_reviewer is None
        assert v._gemini_reviewer is not None

    def test_only_anthropic_key(self):
        v = TafsirValidator(anthropic_key="fake-anthropic", gemini_review_key=None)
        assert v._claude_reviewer is not None
        assert v._gemini_reviewer is None

    def test_both_keys_both_reviewers(self):
        v = TafsirValidator(
            anthropic_key="fake-anthropic", gemini_review_key="fake-gemini",
        )
        assert v._claude_reviewer is not None
        assert v._gemini_reviewer is not None

    def test_credit_exhausted_flag_starts_false(self):
        v = TafsirValidator(
            anthropic_key="fake", gemini_review_key="fake",
        )
        assert v._claude_credit_exhausted is False


# ════════════════════════════════════════════════════════════════
# Phase state save/load (via tmpdir)
# ════════════════════════════════════════════════════════════════
class TestPhaseStatePersistence:
    """Test that phase state survives across orchestrator runs.

    Uses a synthetic orchestrator-like object with just the helpers we need.
    """

    def _make_fake_orchestrator(self, tmp_path):
        """Build a minimal mock with phase state methods extracted."""
        from orchestrator import Orchestrator
        # We just need access to the helpers, not full construction
        fake = MagicMock(spec=Orchestrator)
        # Bind real method implementations
        fake.paths = MagicMock()
        fake.paths.temp_episodes = tmp_path

        # Bind real methods
        fake._phase_state_path = lambda ep: Orchestrator._phase_state_path(fake, ep)
        fake._save_phase_state = lambda ep, **kw: Orchestrator._save_phase_state(fake, ep, **kw)
        fake._load_phase_state = lambda ep: Orchestrator._load_phase_state(fake, ep)
        return fake

    def test_save_creates_file(self, tmp_path):
        fake = self._make_fake_orchestrator(tmp_path)
        fake._save_phase_state(1, foo="bar", count=42)
        state_path = fake._phase_state_path(1)
        assert state_path.exists()

    def test_save_then_load_returns_data(self, tmp_path):
        fake = self._make_fake_orchestrator(tmp_path)
        fake._save_phase_state(1, audio_map={"a": "/tmp/a.mp3"})
        state = fake._load_phase_state(1)
        assert state == {"audio_map": {"a": "/tmp/a.mp3"}}

    def test_save_merges_not_overwrites(self, tmp_path):
        fake = self._make_fake_orchestrator(tmp_path)
        fake._save_phase_state(1, phase1_done=True)
        fake._save_phase_state(1, audio_map={"a": "x"})
        state = fake._load_phase_state(1)
        assert state["phase1_done"] is True
        assert state["audio_map"] == {"a": "x"}

    def test_load_missing_returns_empty(self, tmp_path):
        fake = self._make_fake_orchestrator(tmp_path)
        state = fake._load_phase_state(99)  # never saved
        assert state == {}

    def test_paths_serialized_as_strings(self, tmp_path):
        fake = self._make_fake_orchestrator(tmp_path)
        fake._save_phase_state(1, my_path=Path("/tmp/test.mp4"))
        state = fake._load_phase_state(1)
        # Should be a string after round-trip
        assert state["my_path"] == "/tmp/test.mp4"
        assert isinstance(state["my_path"], str)


# ════════════════════════════════════════════════════════════════
# Strict tests of new modules (visual_prompt_deep, tts_director)
# ════════════════════════════════════════════════════════════════
class TestVisualPromptDeep:
    def test_empty_result_not_usable(self):
        from engines.visual_prompt_deep import DeepVisualPromptResult
        r = DeepVisualPromptResult()
        assert not r.is_usable
        assert not r.is_complete

    def test_layer1_only_is_usable(self):
        from engines.visual_prompt_deep import DeepVisualPromptResult
        r = DeepVisualPromptResult(
            subject="ant", layers_completed=1,
        )
        assert r.is_usable
        assert not r.is_complete

    def test_full_three_layers(self):
        from engines.visual_prompt_deep import DeepVisualPromptResult
        r = DeepVisualPromptResult(
            subject="ant",
            layers_completed=3,
        )
        assert r.is_complete
        assert r.is_usable

    def test_merge_to_leonardo_prompt_includes_all_filled(self):
        from engines.visual_prompt_deep import DeepVisualPromptResult
        r = DeepVisualPromptResult(
            subject="ant carrying leaf",
            mood="contemplative",
            camera_angle="low angle",
            layers_completed=3,
        )
        merged = r.merge_to_leonardo_prompt()
        assert "ant carrying leaf" in merged
        assert "contemplative" in merged
        assert "low angle" in merged

    def test_merge_skips_empty_fields(self):
        from engines.visual_prompt_deep import DeepVisualPromptResult
        r = DeepVisualPromptResult(
            subject="bird",
            # mood, camera_angle, etc. all empty
            layers_completed=1,
        )
        merged = r.merge_to_leonardo_prompt()
        # Should not have leading commas or "in " for empty environment
        assert merged == "bird"

    def test_visual_engineer_uses_deep_result(self):
        from engines.visual_prompt_deep import DeepVisualPromptResult
        from engines.visual_prompt_engineer import VisualPromptEngineer
        r = DeepVisualPromptResult(
            subject="ancient olive tree",
            mood="contemplative",
            camera_angle="low angle wide shot",
            layers_completed=3,
        )
        positive, negative = VisualPromptEngineer.build_from_deep_result(
            r, emotion="warm",
        )
        assert "ancient olive tree" in positive
        assert "Studio Ghibli" in positive  # LOCKED_STYLE preserved
        # Safety constraints are in LOCKED_STYLE (positive prompt)
        assert "no detailed human faces" in positive
        # Negative should have art-style avoidance terms
        assert "photorealistic" in negative or "photograph" in negative


class TestTTSDirector:
    def test_segment_direction_to_elevenlabs_input(self):
        from engines.tts_director import SegmentDirection
        sd = SegmentDirection(
            segment_id="scene1.hook",
            original_text="hello",
            directed_text='hello <break time="500ms"/>',
            pace="fast",
        )
        # Just verify the data structure exists and works
        assert sd.segment_id == "scene1.hook"
        assert "<break" in sd.directed_text
