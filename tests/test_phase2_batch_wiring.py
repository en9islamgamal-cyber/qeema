"""Tests for v22.6 Phase 2 batch wiring in orchestrator.

These tests verify the integration points between the orchestrator and the
batch engines, focusing on the contracts that were broken or missing in
v22.5:

  1. _run_phase2_deep_visuals tries BatchVisualPromptEngine on Key 3 first
  2. _run_phase2_tts_director tries BatchTTSDirector on Key 2 first
  3. Both fall back to legacy on batch failure (no crash)
  4. The legacy fallback uses the correct EpisodeDirection.segments attr
     (was .directions in v22.5 — silent AttributeError every run)
  5. Phase 2 keys split: TTS uses KEY_2, visual uses KEY_3
  6. Output JSON shapes are exactly what downstream code expects
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from engines.batch_engines import (
    AyahReviewOut,
    AyahVisualOut,
    BatchReviewOut,
    BatchTTSDirector,
    BatchTTSOut,
    BatchVisualOut,
    BatchVisualPromptEngine,
    SegmentTTSOut,
)


# ════════════════════════════════════════════════════════════════
# Phase 2 key-splitting contract
# ════════════════════════════════════════════════════════════════
class TestPhase2KeySplit:
    """v22.6 splits Phase 2 across two keys to free Key 2 for TTS only."""

    def setup_method(self):
        # Save and clear all Gemini key envs for isolated tests
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
        }

    def teardown_method(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_visual_adapter_prefers_key_3(self):
        os.environ["GEMINI_API_KEY"] = "k1"
        os.environ["GEMINI_API_KEY_2"] = "k2"
        os.environ["GEMINI_API_KEY_3"] = "k3"
        from orchestrator import Orchestrator
        with patch(
            "infrastructure.llm_adapters.GeminiJsonAdapter"
        ) as adapter_cls:
            Orchestrator._build_phase2_adapter(
                primary_env="GEMINI_API_KEY_3",
                instance_name="phase2-gemini-visual",
                purpose="visual prompts",
            )
            args, _ = adapter_cls.call_args
            assert args[0] == "k3"

    def test_tts_adapter_prefers_key_2(self):
        os.environ["GEMINI_API_KEY"] = "k1"
        os.environ["GEMINI_API_KEY_2"] = "k2"
        os.environ["GEMINI_API_KEY_3"] = "k3"
        from orchestrator import Orchestrator
        with patch(
            "infrastructure.llm_adapters.GeminiJsonAdapter"
        ) as adapter_cls:
            Orchestrator._build_phase2_adapter(
                primary_env="GEMINI_API_KEY_2",
                instance_name="phase2-gemini-tts",
                purpose="TTS",
            )
            args, _ = adapter_cls.call_args
            assert args[0] == "k2"

    def test_visual_adapter_falls_back_to_key_2_then_key_1(self):
        """If KEY_3 isn't set, visual adapter falls back to KEY_2."""
        os.environ["GEMINI_API_KEY"] = "k1"
        os.environ["GEMINI_API_KEY_2"] = "k2"
        # GEMINI_API_KEY_3 NOT set
        from orchestrator import Orchestrator
        with patch(
            "infrastructure.llm_adapters.GeminiJsonAdapter"
        ) as adapter_cls:
            Orchestrator._build_phase2_adapter(
                primary_env="GEMINI_API_KEY_3",
                instance_name="x", purpose="x",
            )
            args, _ = adapter_cls.call_args
            assert args[0] == "k2"  # fell back to KEY_2

    def test_visual_adapter_falls_back_all_the_way_to_key_1(self):
        """KEY_2 and KEY_3 both missing → uses KEY_1."""
        os.environ["GEMINI_API_KEY"] = "k1"
        from orchestrator import Orchestrator
        with patch(
            "infrastructure.llm_adapters.GeminiJsonAdapter"
        ) as adapter_cls:
            Orchestrator._build_phase2_adapter(
                primary_env="GEMINI_API_KEY_3",
                instance_name="x", purpose="x",
            )
            args, _ = adapter_cls.call_args
            assert args[0] == "k1"

    def test_visual_adapter_returns_none_when_no_keys(self):
        from orchestrator import Orchestrator
        result = Orchestrator._build_phase2_adapter(
            primary_env="GEMINI_API_KEY_3",
            instance_name="x", purpose="x",
        )
        assert result is None

    def test_legacy_alias_routes_to_tts(self):
        """_phase2_gemini_adapter is preserved as a backward-compat alias.
        Verify by inspecting its source — it must call the TTS adapter."""
        import inspect
        from orchestrator import Orchestrator
        src = inspect.getsource(Orchestrator._phase2_gemini_adapter)
        assert "_phase2_tts_gemini_adapter" in src


# ════════════════════════════════════════════════════════════════
# JSON shape contracts — what downstream code expects
# ════════════════════════════════════════════════════════════════
class TestDownstreamJsonShapes:
    """The orchestrator persists Phase 2 outputs into the episode JSON.
    Downstream code (script_engine_unified, TTS synthesis) reads back
    these shapes — they MUST match exactly."""

    def test_deep_visuals_shape_matches_legacy_consumer(self):
        """script_engine_unified._build_visual_prompt reads `_deep_visuals`
        as list-of-dicts, each with the 14 cinematic fields + is_usable."""
        result = BatchVisualOut(prompts=[
            AyahVisualOut(
                ayah_number=1,
                subject="seed", action="sprouting",
                environment="garden", time_of_day="dawn",
                mood="peaceful", color_palette="ochre, sage",
                lighting_direction="soft side", atmospheric_elements="dust",
                camera_angle="close-up", depth_of_field="shallow",
                foreground="earth", midground="seed",
                background="hills", focal_point="tip",
            ),
        ])
        legacy = BatchVisualPromptEngine.to_legacy_dicts(result)
        # script_engine_unified.py:776-792 reads exactly these keys:
        expected_keys = {
            "subject", "action", "environment", "time_of_day",
            "mood", "color_palette", "lighting_direction",
            "atmospheric_elements", "camera_angle", "depth_of_field",
            "foreground", "midground", "background", "focal_point",
            "layers_completed", "is_usable",
        }
        assert set(legacy[0].keys()) == expected_keys

    def test_tts_directions_shape_matches_legacy_consumer(self):
        """orchestrator.py:1545 reads `_tts_directions[segment_id]['directed_text']`.
        Our output dict MUST be keyed by segment_id with that nested field."""
        result = BatchTTSOut(directions=[
            SegmentTTSOut(
                segment_id="ayah_1.hook",
                directed_text="hello <break/>",
                pace="fast",
                pronunciation_notes=[],
            ),
        ])
        legacy = BatchTTSDirector.to_legacy_dict(result)
        assert "ayah_1.hook" in legacy
        assert "directed_text" in legacy["ayah_1.hook"]
        assert "pace" in legacy["ayah_1.hook"]
        assert "pronunciation_notes" in legacy["ayah_1.hook"]


# ════════════════════════════════════════════════════════════════
# v22.5 attribute bug fix — EpisodeDirection.segments not .directions
# ════════════════════════════════════════════════════════════════
class TestLegacyTtsAttributeBugFix:
    """v22.5 BUG: orchestrator's legacy TTS path read
    `episode_direction.directions` but EpisodeDirection's field is `segments`.
    Result: AttributeError → except → log "audio will use base settings".
    Legacy TTSDirector was effectively broken in production.

    v22.6 fix: read .segments correctly. Verify here so it doesn't regress.
    """

    def test_legacy_tts_reads_segments_attr(self):
        """Inspect the orchestrator source for the bug pattern. The
        docstring legitimately mentions the old buggy pattern by name to
        document the fix — so we strip the docstring before scanning."""
        import ast
        import inspect
        from orchestrator import Orchestrator

        src = inspect.getsource(Orchestrator._legacy_tts)
        # Parse the function and remove the docstring node so we scan the
        # actual code body, not the comment that describes the bug.
        tree = ast.parse(src.strip())
        funcdef = tree.body[0]
        if (
            funcdef.body
            and isinstance(funcdef.body[0], ast.Expr)
            and isinstance(funcdef.body[0].value, ast.Constant)
            and isinstance(funcdef.body[0].value.value, str)
        ):
            funcdef.body = funcdef.body[1:]
        code_only = ast.unparse(funcdef)

        # Code body must read .segments
        assert ".segments" in code_only or "'segments'" in code_only
        # Code body must NOT read .directions on episode_direction
        # (the v22.5 bug pattern)
        assert "episode_direction.directions" not in code_only
        assert "episode_dir.directions" not in code_only


# ════════════════════════════════════════════════════════════════
# Phase 2 fallback ordering: batch → legacy → graceful skip
# ════════════════════════════════════════════════════════════════
class TestPhase2FallbackChain:
    """When BatchVisualPromptEngine fails, must NOT crash — must fall back
    to legacy DeepVisualPromptGenerator. Same for TTS.
    """

    def test_visual_helper_returns_none_when_no_adapter(self):
        """Without a Gemini key, _try_batch_visual_prompts returns None
        gracefully (caller falls back)."""
        from orchestrator import Orchestrator
        instance = MagicMock(spec=Orchestrator)
        instance._phase2_visual_gemini_adapter = MagicMock(return_value=None)
        result = Orchestrator._try_batch_visual_prompts(
            instance, ayah_scenes=[{"ayah": {"number": 1}}],
        )
        assert result is None

    def test_tts_helper_returns_none_when_no_adapter(self):
        from orchestrator import Orchestrator
        instance = MagicMock(spec=Orchestrator)
        instance._phase2_tts_gemini_adapter = MagicMock(return_value=None)
        result = Orchestrator._try_batch_tts(instance, episode_data={})
        assert result is None

    def test_visual_helper_returns_none_when_batch_engine_returns_none(
        self,
    ):
        """If BatchVisualPromptEngine returns None (Gemini parse failure),
        we propagate None so the caller falls back to legacy."""
        from orchestrator import Orchestrator
        # Build a fake adapter with a fake _client
        adapter = MagicMock()
        adapter._client = MagicMock()
        instance = MagicMock(spec=Orchestrator)
        instance._phase2_visual_gemini_adapter = MagicMock(return_value=adapter)

        # Patch the engine to return None
        with patch(
            "engines.batch_engines.BatchVisualPromptEngine"
        ) as engine_cls:
            engine = MagicMock()
            engine.generate_visuals.return_value = None
            engine_cls.return_value = engine
            result = Orchestrator._try_batch_visual_prompts(
                instance, ayah_scenes=[{"ayah": {"number": 1}}],
            )
        assert result is None

    def test_visual_helper_succeeds_and_returns_legacy_shape(self):
        """Happy path: batch engine returns BatchVisualOut, helper converts
        to _deep_visuals shape."""
        from orchestrator import Orchestrator

        adapter = MagicMock()
        adapter._client = MagicMock()
        instance = MagicMock(spec=Orchestrator)
        instance._phase2_visual_gemini_adapter = MagicMock(return_value=adapter)

        good_visual = BatchVisualOut(prompts=[
            AyahVisualOut(
                ayah_number=1,
                subject="seed", action="sprouting",
                environment="garden", time_of_day="dawn",
                mood="peaceful", color_palette="ochre",
                lighting_direction="soft", atmospheric_elements="dust",
                camera_angle="close-up", depth_of_field="shallow",
                foreground="earth", midground="seed",
                background="hills", focal_point="tip",
            ),
        ])

        # Patch only generate_visuals — keep to_legacy_dicts as the real
        # static method so it produces a real list (not a MagicMock).
        with patch(
            "engines.batch_engines.BatchVisualPromptEngine.generate_visuals",
            return_value=good_visual,
        ):
            result = Orchestrator._try_batch_visual_prompts(
                instance,
                ayah_scenes=[{
                    "ayah": {"number": 1},
                    "explain_text": "e",
                    "story_text": "s",
                    "scene_emotion": "warm",
                }],
            )

        assert result is not None
        assert len(result) == 1
        assert result[0]["subject"] == "seed"
        assert result[0]["is_usable"] is True
        assert result[0]["layers_completed"] == 3


# ════════════════════════════════════════════════════════════════
# ForbiddenAnalogyDetector wired into batch tafsir
# ════════════════════════════════════════════════════════════════
class TestBatchTafsirForbiddenIntegration:
    """v22.6: deterministic detector runs alongside Gemini batch reviewer.
    A keyword hit forces passed=False even if Gemini approved."""

    def test_forbidden_hit_forces_failure_even_if_gemini_passes(self):
        """If the LLM-generated analogy contains 'مغناطيس' for an عبادة
        ayah, the detector must override Gemini's 'pass'."""
        from orchestrator import Orchestrator

        # Mock all the things _try_batch_tafsir reaches for
        validator = MagicMock()
        reviewer = MagicMock()
        reviewer._client = MagicMock()
        validator._gemini_reviewer = reviewer
        fetcher = MagicMock()
        fetcher.fetch_combined.return_value = "تفسير معتمد"
        validator._fetcher = fetcher
        validator._confidence_threshold = 0.65

        instance = MagicMock(spec=Orchestrator)
        instance.tafsir_validator = validator

        # Build a script with a forbidden analogy
        ayah_obj = MagicMock()
        ayah_obj.number = 5
        ayah_obj.text = "إياك نعبد وإياك نستعين"
        scene = MagicMock()
        scene.ayah = ayah_obj
        scene.explain_text = "نعبد الله وحده"
        scene.story_text = "زي المغناطيس اللي بيشد الحديد"  # forbidden!

        script = MagicMock()
        script.ayah_scenes = [scene]

        # Gemini reviewer says "pass" (incorrectly, missing the forbidden pattern)
        gemini_pass = BatchReviewOut(reviews=[
            AyahReviewOut(
                ayah_number=5, passed=True, confidence=0.92,
                concerns=[],
            ),
        ])
        with patch(
            "engines.batch_engines.BatchTafsirReviewer"
        ) as engine_cls:
            engine = MagicMock()
            engine.review_episode.return_value = gemini_pass
            engine_cls.return_value = engine

            results = Orchestrator._try_batch_tafsir(
                instance,
                script=script, surah_name="الفاتحة", surah_num=1,
            )

        assert results is not None
        assert len(results) == 1
        # Despite Gemini's pass, the detector forces failure
        assert results[0]["passed"] is False
        # The concerns list MUST contain the forbidden hit
        assert any(
            "worship-as-magnet" in c
            for c in results[0]["concerns"]
        )
        # Method tag tracks that the detector contributed
        assert "forbidden-detector" in results[0]["method"]

    def test_clean_content_passes_through_unchanged(self):
        """A clean analogy should pass: Gemini's verdict is preserved."""
        from orchestrator import Orchestrator

        validator = MagicMock()
        reviewer = MagicMock()
        reviewer._client = MagicMock()
        validator._gemini_reviewer = reviewer
        fetcher = MagicMock()
        fetcher.fetch_combined.return_value = "تفسير معتمد"
        validator._fetcher = fetcher
        validator._confidence_threshold = 0.65

        instance = MagicMock(spec=Orchestrator)
        instance.tafsir_validator = validator

        ayah_obj = MagicMock()
        ayah_obj.number = 1
        ayah_obj.text = "بسم الله الرحمن الرحيم"
        scene = MagicMock()
        scene.ayah = ayah_obj
        scene.explain_text = "نبدأ بها لطلب البركة من الله"
        scene.story_text = "زي ما بنبدأ كل عمل بأهم اسم"  # clean

        script = MagicMock()
        script.ayah_scenes = [scene]

        gemini_pass = BatchReviewOut(reviews=[
            AyahReviewOut(
                ayah_number=1, passed=True, confidence=0.90,
                concerns=[],
            ),
        ])
        with patch(
            "engines.batch_engines.BatchTafsirReviewer"
        ) as engine_cls:
            engine = MagicMock()
            engine.review_episode.return_value = gemini_pass
            engine_cls.return_value = engine

            results = Orchestrator._try_batch_tafsir(
                instance,
                script=script, surah_name="الفاتحة", surah_num=1,
            )

        assert results[0]["passed"] is True
        assert results[0]["method"] == "gemini-2.5-flash-batch-v22.6"
        assert "forbidden-detector" not in results[0]["method"]
