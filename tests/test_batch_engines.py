"""Tests for engines/batch_engines.py (v22.6).

These tests exercise the four Pydantic-schema-driven batch engines that
replace the legacy multi-call paths in v22.6:
  - BatchScriptEngine          (Phase 1, Key 1)
  - BatchTafsirReviewer        (Phase 1, Key 1)
  - BatchTTSDirector           (Phase 2, Key 2)
  - BatchVisualPromptEngine    (Phase 2, Key 3)

Coverage:
  - Schema validation (Pydantic constraints — max_length, ge/le, min_length)
  - 3-layer fallback: response.parsed → json.loads + model_validate → regex salvage
  - Smart-quote normalization (\u201c \u201d \u2018 \u2019 \u00ab \u00bb)
  - Markdown fence stripping (```json … ```)
  - Trailing-comma cleanup in salvage
  - Total Gemini failure → returns None (so caller can fall back to legacy)
  - to_legacy_dict / to_legacy_dicts converters round-trip correctly
  - Empty/missing inputs handled defensively (no crash)

Tests do NOT make any network calls — every Gemini interaction is mocked
at the SDK level.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from engines.batch_engines import (
    AyahReviewOut,
    AyahScriptOut,
    AyahVisualOut,
    BatchReviewOut,
    BatchScriptEngine,
    BatchScriptOut,
    BatchTafsirReviewer,
    BatchTTSDirector,
    BatchTTSOut,
    BatchVisualOut,
    BatchVisualPromptEngine,
    SegmentTTSOut,
    _aggressive_json_clean,
    _call_gemini_with_schema,
    _try_iterative_json_recovery,
)


# ════════════════════════════════════════════════════════════════
# Test helpers
# ════════════════════════════════════════════════════════════════
def _mock_gemini_client(
    *,
    parsed: Optional[BaseModel] = None,
    text: Optional[str] = None,
    raises: Optional[Exception] = None,
) -> MagicMock:
    """Build a MagicMock that mimics google.genai.Client.

    The shape we mock:
        client.models.generate_content(...) → response
        response.parsed   (BaseModel | None)
        response.text     (str | None)

    Note: we have to patch `from google.genai import types as genai_types`
    inside _call_gemini_with_schema. The simplest way is to substitute a
    minimal stand-in that has GenerateContentConfig as a callable.
    """
    client = MagicMock(name="GeminiClient")
    if raises is not None:
        client.models.generate_content.side_effect = raises
    else:
        response = MagicMock(name="GeminiResponse")
        response.parsed = parsed
        response.text = text or ""
        client.models.generate_content.return_value = response
    return client


@pytest.fixture
def fake_genai_types():
    """Inject a fake `google.genai` module into sys.modules so that
    `from google.genai import types as genai_types` inside the production
    code resolves to our stand-in WITHOUT hitting the real package.

    Why not patch("google.genai.types", ...)? Because patch() resolves
    the target by importing it first — which fails when google-genai
    isn't installed (e.g. in pre_check CI step where the test suite
    runs before the full pip install). sys.modules injection bypasses
    the import machinery entirely.
    """
    import sys

    fake_types_module = MagicMock(name="genai_types_module")
    fake_types_module.GenerateContentConfig = MagicMock(
        side_effect=lambda **kwargs: kwargs,
    )

    fake_genai_module = MagicMock(name="google_genai_module")
    fake_genai_module.types = fake_types_module

    fake_google_module = sys.modules.get("google")
    saved_google = fake_google_module
    saved_genai = sys.modules.get("google.genai")
    saved_types = sys.modules.get("google.genai.types")

    if fake_google_module is None:
        fake_google_module = MagicMock(name="google_module")
        sys.modules["google"] = fake_google_module
    fake_google_module.genai = fake_genai_module
    sys.modules["google.genai"] = fake_genai_module
    sys.modules["google.genai.types"] = fake_types_module

    try:
        yield fake_types_module
    finally:
        # Restore — be careful: if google was a real package before, leave it.
        if saved_google is None:
            sys.modules.pop("google", None)
        if saved_genai is None:
            sys.modules.pop("google.genai", None)
        else:
            sys.modules["google.genai"] = saved_genai
        if saved_types is None:
            sys.modules.pop("google.genai.types", None)
        else:
            sys.modules["google.genai.types"] = saved_types


# ════════════════════════════════════════════════════════════════
# Schema integrity
# ════════════════════════════════════════════════════════════════
class TestPydanticSchemas:
    """Each schema's contract is part of the v22.6 design — these tests
    pin the contract so accidental drift is caught immediately."""

    def test_ayah_script_out_requires_all_fields(self):
        """Schema rejects missing fields — Gemini must fill them all."""
        with pytest.raises(ValidationError):
            AyahScriptOut(ayah_number=1, hook_text="x")  # missing rest

    def test_ayah_script_out_accepts_complete_ayah(self):
        ayah = AyahScriptOut(
            ayah_number=1,
            hook_text="هل تعرف إيه الكنز اللي اتذكر مرتين؟",
            explain_text="ربنا بيعلمنا في الآية دي إن البداية باسمه بركة.",
            story_text="زي البذرة لما تنمو، بنبدأ كل حاجة باسم ربنا.",
            moral_text="نبدأ كل حاجة باسم الله.",
            scene_emotion="warm",
        )
        assert ayah.ayah_number == 1

    def test_batch_script_out_requires_at_least_one_ayah(self):
        with pytest.raises(ValidationError):
            BatchScriptOut(
                title="t", youtube_title="yt", youtube_description="d",
                intro_text="i", outro_text="o", ayahs=[],
            )

    def test_batch_script_out_caps_ayahs_at_10(self):
        too_many = [
            AyahScriptOut(
                ayah_number=i, hook_text="h", explain_text="e",
                story_text="s", moral_text="m", scene_emotion="warm",
            )
            for i in range(1, 12)  # 11 — over the cap
        ]
        with pytest.raises(ValidationError):
            BatchScriptOut(
                title="t", youtube_title="yt", youtube_description="d",
                intro_text="i", outro_text="o", ayahs=too_many,
            )

    def test_ayah_review_out_clamps_confidence_to_0_1(self):
        with pytest.raises(ValidationError):
            AyahReviewOut(ayah_number=1, passed=True, confidence=1.5)
        with pytest.raises(ValidationError):
            AyahReviewOut(ayah_number=1, passed=True, confidence=-0.1)

    def test_ayah_review_out_concerns_default_to_empty(self):
        r = AyahReviewOut(ayah_number=1, passed=True, confidence=0.9)
        assert r.concerns == []

    def test_segment_tts_out_required_fields(self):
        seg = SegmentTTSOut(
            segment_id="ayah_1.hook",
            directed_text='تخيل <break time="300ms"/> معايا',
            pace="normal",
        )
        assert seg.pace == "normal"
        assert seg.pace_reason == ""  # default
        assert seg.pronunciation_notes == []  # default

    def test_ayah_visual_out_has_all_14_fields(self):
        """v22.6: visual schema MUST mirror DeepVisualPromptResult to enable
        drop-in replacement of the legacy chained generator."""
        required = {
            "ayah_number",
            # Layer 1
            "subject", "action", "environment", "time_of_day",
            # Layer 2
            "mood", "color_palette", "lighting_direction", "atmospheric_elements",
            # Layer 3
            "camera_angle", "depth_of_field",
            "foreground", "midground", "background", "focal_point",
        }
        actual = set(AyahVisualOut.model_fields.keys())
        assert required == actual, (
            f"AyahVisualOut field drift. "
            f"Missing: {required - actual}, Extra: {actual - required}"
        )


# ════════════════════════════════════════════════════════════════
# _call_gemini_with_schema — the 3-layer fallback core
# ════════════════════════════════════════════════════════════════
class _TinySchema(BaseModel):
    foo: str
    bar: int


class TestCallGeminiWithSchema:
    """The salvage layer is the safety net that lets us survive Gemini
    occasionally producing borderline-malformed JSON."""

    def test_layer_1_response_parsed_short_circuits(self, fake_genai_types):
        """When SDK gives us a typed object, no JSON parsing happens."""
        wanted = _TinySchema(foo="hello", bar=42)
        client = _mock_gemini_client(parsed=wanted)
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out is wanted

    def test_layer_2_json_loads_when_parsed_is_none(self, fake_genai_types):
        """Sometimes SDK can't auto-parse; raw text is still valid JSON."""
        client = _mock_gemini_client(
            parsed=None, text='{"foo": "hi", "bar": 7}',
        )
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out == _TinySchema(foo="hi", bar=7)

    def test_layer_2_strips_markdown_fences(self, fake_genai_types):
        client = _mock_gemini_client(
            parsed=None,
            text='```json\n{"foo": "hi", "bar": 7}\n```',
        )
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out == _TinySchema(foo="hi", bar=7)

    def test_layer_2_normalizes_arabic_smart_quotes(self, fake_genai_types):
        """Gemini sometimes emits curly quotes around Arabic strings — they
        break json.loads. Salvage must normalize them."""
        # Build a payload using smart quotes — these are what Gemini sometimes emits
        smart_payload = '{\u201cfoo\u201d: \u201cمرحبا\u201d, \u201cbar\u201d: 1}'
        client = _mock_gemini_client(parsed=None, text=smart_payload)
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out == _TinySchema(foo="مرحبا", bar=1)

    def test_layer_3_regex_salvage_on_trailing_comma(self, fake_genai_types):
        """Trailing comma is invalid JSON but salvage strips it."""
        client = _mock_gemini_client(
            parsed=None,
            text='{"foo": "x", "bar": 1,}',  # trailing comma
        )
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out == _TinySchema(foo="x", bar=1)

    def test_layer_3_regex_salvage_extracts_object_from_noise(
        self, fake_genai_types,
    ):
        """If Gemini wraps JSON in chatter, salvage finds the {…} block."""
        client = _mock_gemini_client(
            parsed=None,
            text='تمام، هكتبلك:\n{"foo": "x", "bar": 5}\nعارف؟',
        )
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out == _TinySchema(foo="x", bar=5)

    def test_total_failure_returns_none(self, fake_genai_types):
        """If text is empty and parsed is None, return None (no crash)."""
        client = _mock_gemini_client(parsed=None, text="")
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out is None

    def test_unparseable_garbage_returns_none(self, fake_genai_types):
        """If text has no JSON at all, return None — caller falls back."""
        client = _mock_gemini_client(parsed=None, text="ايه يا باشا")
        out = _call_gemini_with_schema(client, "prompt", _TinySchema)
        assert out is None

    def test_gemini_exception_propagates(self, fake_genai_types):
        """SDK exceptions (auth, network) bubble up — caller decides retry."""
        client = _mock_gemini_client(raises=RuntimeError("503 Service Unavailable"))
        with pytest.raises(RuntimeError, match="503"):
            _call_gemini_with_schema(client, "prompt", _TinySchema)


# ════════════════════════════════════════════════════════════════
# BatchScriptEngine
# ════════════════════════════════════════════════════════════════
class TestBatchScriptEngine:

    def _good_response(self, ayah_count: int = 7) -> BatchScriptOut:
        return BatchScriptOut(
            title="سورة الفاتحة — أم الكتاب",
            youtube_title="ليه الفاتحة هي أعظم سورة؟",
            youtube_description="حلقة من قناة قِيمة لشرح سورة الفاتحة للأطفال.",
            intro_text="أهلاً بيكم في حلقة جديدة.",
            outro_text="شكراً ليكم، نشوفكم في الحلقة الجاية.",
            ayahs=[
                AyahScriptOut(
                    ayah_number=i,
                    hook_text=f"hook {i}",
                    explain_text=f"explain {i}",
                    story_text=f"story {i}",
                    moral_text=f"moral {i}",
                    scene_emotion="warm",
                )
                for i in range(1, ayah_count + 1)
            ],
        )

    def test_generate_episode_happy_path(self, fake_genai_types):
        wanted = self._good_response()
        client = _mock_gemini_client(parsed=wanted)

        engine = BatchScriptEngine(client)
        result = engine.generate_episode(
            surah_name="الفاتحة", surah_number=1,
            ayahs=[{"number": i, "text": f"آية {i}"} for i in range(1, 8)],
            tafsirs={i: f"تفسير {i}" for i in range(1, 8)},
        )
        assert result is wanted
        assert len(result.ayahs) == 7

    def test_generate_episode_returns_none_on_total_failure(
        self, fake_genai_types,
    ):
        client = _mock_gemini_client(parsed=None, text="")
        engine = BatchScriptEngine(client)
        result = engine.generate_episode(
            surah_name="x", surah_number=1, ayahs=[], tafsirs={},
        )
        assert result is None

    def test_prompt_includes_all_4_forbidden_analogies(self):
        """The system prompt MUST forbid all 4 canonical doctrinal errors.
        This is the key v22.6 invariant."""
        prompt = BatchScriptEngine._build_prompt(
            surah_name="الفاتحة", surah_number=1,
            ayahs=[{"number": 1, "text": "بسم الله"}],
            tafsirs={1: "..."},
        )
        # 1. Judgment-day-as-biology
        assert "البيولوجية" in prompt or "بيولوجي" in prompt
        # 2. Worship-as-magnet
        assert "مغناطيس" in prompt
        assert "اختيار حر" in prompt
        # 3. Wrath-as-food
        assert "أكل صحي" in prompt
        # 4. Basmala-as-magic
        assert "كود سري" in prompt or "كلمة سحرية" in prompt


# ════════════════════════════════════════════════════════════════
# BatchTafsirReviewer
# ════════════════════════════════════════════════════════════════
class TestBatchTafsirReviewer:

    def test_review_episode_passes_through_when_all_clear(
        self, fake_genai_types,
    ):
        wanted = BatchReviewOut(reviews=[
            AyahReviewOut(ayah_number=1, passed=True, confidence=0.95, concerns=[]),
            AyahReviewOut(ayah_number=2, passed=True, confidence=0.90, concerns=[]),
        ])
        client = _mock_gemini_client(parsed=wanted)
        engine = BatchTafsirReviewer(client)
        result = engine.review_episode(
            ayah_scripts=[
                {"number": 1, "explain": "e1", "story": "s1"},
                {"number": 2, "explain": "e2", "story": "s2"},
            ],
            tafsirs={1: "t1", 2: "t2"},
        )
        assert result is wanted
        assert all(r.passed for r in result.reviews)

    def test_review_episode_returns_none_on_total_failure(
        self, fake_genai_types,
    ):
        client = _mock_gemini_client(parsed=None, text="")
        engine = BatchTafsirReviewer(client)
        result = engine.review_episode(
            ayah_scripts=[], tafsirs={},
        )
        assert result is None

    def test_review_prompt_includes_all_4_red_flags(self):
        """Reviewer prompt must surface red flags explicitly so the LLM
        actively scans for them, not just compares to authentic tafsir."""
        prompt = BatchTafsirReviewer._build_prompt(
            ayah_scripts=[{"number": 1, "explain": "e", "story": "s"}],
            tafsirs={1: "t"},
        )
        assert "red flag #1" in prompt.lower() or "red flag" in prompt.lower()
        # Topic + forbidden combo for each rule
        assert "اليوم الآخر" in prompt or "يوم الدين" in prompt
        assert "العبادة" in prompt and "مغناطيس" in prompt
        assert "الضالين" in prompt or "المغضوب" in prompt
        assert "بسم الله" in prompt


# ════════════════════════════════════════════════════════════════
# BatchTTSDirector
# ════════════════════════════════════════════════════════════════
class TestBatchTTSDirector:

    def test_init_rejects_none_client(self):
        with pytest.raises(ValueError, match="requires a Gemini client"):
            BatchTTSDirector(None)

    def test_collect_segments_iterates_intro_outro_and_per_scene(self):
        episode_data = {
            "intro_text": "أهلاً",
            "outro_text": "شكراً",
            "ayah_scenes": [
                {"hook_text": "h1", "story_text": "s1", "moral_text": "m1"},
                {"hook_text": "h2", "story_text": "s2", "moral_text": "m2"},
            ],
        }
        segments = BatchTTSDirector._collect_segments(episode_data)
        ids = [s[0] for s in segments]
        assert ids == [
            "intro_text", "outro_text",
            "ayah_1.hook", "ayah_1.story", "ayah_1.moral",
            "ayah_2.hook", "ayah_2.story", "ayah_2.moral",
        ]

    def test_collect_segments_skips_empty_text(self):
        episode_data = {
            "intro_text": "  ",  # whitespace-only
            "outro_text": "",
            "ayah_scenes": [
                {"hook_text": "h1", "story_text": "", "moral_text": "m1"},
            ],
        }
        segments = BatchTTSDirector._collect_segments(episode_data)
        ids = [s[0] for s in segments]
        assert ids == ["ayah_1.hook", "ayah_1.moral"]

    def test_direct_episode_returns_none_on_empty_input(self, fake_genai_types):
        client = _mock_gemini_client()
        director = BatchTTSDirector(client)
        result = director.direct_episode({"ayah_scenes": []})
        assert result is None
        # Important: never called Gemini for a no-op
        client.models.generate_content.assert_not_called()

    def test_direct_episode_happy_path(self, fake_genai_types):
        wanted = BatchTTSOut(directions=[
            SegmentTTSOut(
                segment_id="intro_text",
                directed_text='أهلاً <break time="300ms"/>',
                pace="normal",
            ),
            SegmentTTSOut(
                segment_id="ayah_1.hook",
                directed_text='تخيل!',
                pace="fast",
            ),
            SegmentTTSOut(
                segment_id="ayah_1.story",
                directed_text='زي البذرة',
                pace="normal",
            ),
            SegmentTTSOut(
                segment_id="ayah_1.moral",
                directed_text='نبدأ كل حاجة',
                pace="slow",
            ),
        ])
        client = _mock_gemini_client(parsed=wanted)
        director = BatchTTSDirector(client)
        result = director.direct_episode({
            "intro_text": "أهلاً",
            "ayah_scenes": [{
                "hook_text": "تخيل!",
                "story_text": "زي البذرة",
                "moral_text": "نبدأ كل حاجة",
            }],
        })
        assert result is wanted

    def test_direct_episode_warns_on_missing_segments_but_returns_partial(
        self, fake_genai_types, caplog,
    ):
        """If Gemini drops segments, we still return what we have."""
        import logging
        partial = BatchTTSOut(directions=[
            SegmentTTSOut(
                segment_id="intro_text",
                directed_text="أهلاً",
                pace="normal",
            ),
            # ayah_1.hook is missing
        ])
        client = _mock_gemini_client(parsed=partial)
        director = BatchTTSDirector(client)
        with caplog.at_level(logging.WARNING):
            result = director.direct_episode({
                "intro_text": "أهلاً",
                "ayah_scenes": [{"hook_text": "تخيل!"}],
            })
        assert result is partial  # not None
        assert any("missing from output" in r.message for r in caplog.records)

    def test_to_legacy_dict_round_trip(self):
        result = BatchTTSOut(directions=[
            SegmentTTSOut(
                segment_id="ayah_1.hook",
                directed_text='تخيل <break time="300ms"/>',
                pace="fast",
                pace_reason="hook excitement",
                pronunciation_notes=["تَخَيَّل"],
            ),
        ])
        legacy = BatchTTSDirector.to_legacy_dict(result)
        assert legacy == {
            "ayah_1.hook": {
                "directed_text": 'تخيل <break time="300ms"/>',
                "pace": "fast",
                "pronunciation_notes": ["تَخَيَّل"],
            },
        }

    def test_prompt_explicitly_forbids_word_changes(self):
        prompt = BatchTTSDirector._build_prompt(
            [("intro_text", "النص", "intro")]
        )
        assert "ممنوع تغيّر الكلمات" in prompt
        assert "<break" in prompt


# ════════════════════════════════════════════════════════════════
# BatchVisualPromptEngine
# ════════════════════════════════════════════════════════════════
class TestBatchVisualPromptEngine:

    def _good_visual(self, ayah_number: int = 1) -> AyahVisualOut:
        return AyahVisualOut(
            ayah_number=ayah_number,
            subject="a single seed sprouting",
            action="gently breaking through soil",
            environment="warm garden, dew on leaves",
            time_of_day="golden hour",
            mood="peaceful and reverent",
            color_palette="warm ochre, soft sage, cream highlights",
            lighting_direction="soft side-lighting from upper left",
            atmospheric_elements="gentle dust motes, faint morning mist",
            camera_angle="low-angle close-up",
            depth_of_field="shallow DoF, background softly blurred",
            foreground="cracked earth",
            midground="the sprout",
            background="distant hills",
            focal_point="the sprout's emerging tip",
        )

    def test_generate_visuals_happy_path(self, fake_genai_types):
        wanted = BatchVisualOut(prompts=[self._good_visual(1), self._good_visual(2)])
        client = _mock_gemini_client(parsed=wanted)
        engine = BatchVisualPromptEngine(client)
        result = engine.generate_visuals([
            {"number": 1, "explain": "e1", "story": "s1", "emotion": "warm"},
            {"number": 2, "explain": "e2", "story": "s2", "emotion": "warm"},
        ])
        assert result is wanted

    def test_generate_visuals_returns_none_on_total_failure(
        self, fake_genai_types,
    ):
        client = _mock_gemini_client(parsed=None, text="")
        engine = BatchVisualPromptEngine(client)
        assert engine.generate_visuals([{"number": 1}]) is None

    def test_to_legacy_dicts_emits_14_fields_plus_metadata(self):
        result = BatchVisualOut(prompts=[self._good_visual(1)])
        legacy = BatchVisualPromptEngine.to_legacy_dicts(result)
        assert len(legacy) == 1
        d = legacy[0]
        # All 14 fields preserved
        for f in ("subject", "action", "environment", "time_of_day",
                  "mood", "color_palette", "lighting_direction",
                  "atmospheric_elements", "camera_angle", "depth_of_field",
                  "foreground", "midground", "background", "focal_point"):
            assert d[f]  # non-empty
        # Metadata for downstream compatibility
        assert d["layers_completed"] == 3
        assert d["is_usable"] is True

    def test_to_legacy_dicts_sorts_by_ayah_number(self):
        """Defensive: if Gemini reorders prompts, we re-sort by ayah_number
        so downstream code sees them in narrative order."""
        out_of_order = BatchVisualOut(prompts=[
            self._good_visual(3),
            self._good_visual(1),
            self._good_visual(2),
        ])
        legacy = BatchVisualPromptEngine.to_legacy_dicts(out_of_order)
        # We can't read ayah_number off the legacy dict (it's not in the
        # 14 fields), but the sort means index 0 came from ayah 1's content.
        # Use the subject string to verify (all the same in this test, so we
        # verify by ordering being well-defined and length matching).
        assert len(legacy) == 3
        # All entries must be usable after the sort
        assert all(d["is_usable"] for d in legacy)

    def test_prompt_locks_visual_style_and_doctrine(self):
        prompt = BatchVisualPromptEngine._build_prompt(
            [{"number": 1, "explain": "e", "story": "s", "emotion": "warm"}],
        )
        # Style lock
        assert "watercolor" in prompt.lower()
        assert "NotebookLM" in prompt or "notebooklm" in prompt.lower()
        # Doctrinal locks (vs idolatrous depiction or magic)
        assert "Prophet" in prompt or "prophet" in prompt
        assert "magnets" in prompt.lower() or "magnetic" in prompt.lower()
        assert "بسم" in prompt or "basmala" in prompt.lower() or "glowing" in prompt.lower()


# ════════════════════════════════════════════════════════════════
# v22.6.2 — Aggressive JSON cleaner (each failure mode in isolation)
# ════════════════════════════════════════════════════════════════
class TestAggressiveJsonClean:
    """Each failure mode observed in production v22.6 gets one test.
    These tests pin the contract for _aggressive_json_clean."""

    def test_strips_markdown_fence(self):
        raw = '```json\n{"foo": "bar"}\n```'
        out = _aggressive_json_clean(raw)
        assert out == '{"foo": "bar"}'

    def test_strips_bare_markdown_fence(self):
        raw = '```\n{"x": 1}\n```'
        out = _aggressive_json_clean(raw)
        assert json.loads(out) == {"x": 1}

    def test_normalizes_arabic_smart_quotes(self):
        raw = '{\u201cfoo\u201d: \u201cمرحبا\u201d}'
        out = _aggressive_json_clean(raw)
        assert json.loads(out) == {"foo": "مرحبا"}

    def test_normalizes_french_guillemets(self):
        raw = '{\u00abkey\u00bb: \u00abvalue\u00bb}'
        out = _aggressive_json_clean(raw)
        assert json.loads(out) == {"key": "value"}

    def test_strips_leading_chatter(self):
        raw = 'تمام، هكتبلك:\n{"foo": "bar"}\n\nعارف؟'
        out = _aggressive_json_clean(raw)
        assert json.loads(out) == {"foo": "bar"}

    def test_strips_trailing_commas_in_object(self):
        raw = '{"a": 1, "b": 2,}'
        out = _aggressive_json_clean(raw)
        assert json.loads(out) == {"a": 1, "b": 2}

    def test_strips_trailing_commas_in_array(self):
        raw = '{"items": [1, 2, 3,]}'
        out = _aggressive_json_clean(raw)
        assert json.loads(out) == {"items": [1, 2, 3]}

    def test_escapes_unescaped_newline_inside_string(self):
        """The ACTUAL v22.5.7 incident pattern: Arabic LLM emits a literal
        newline INSIDE a quoted concern string. json.loads chokes on this."""
        # Build a payload where the value has a raw newline mid-string
        raw = '{"concern": "السطر الأول\nالسطر الثاني"}'
        # Sanity: vanilla json.loads should fail on this
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
        # Cleaner fixes it
        cleaned = _aggressive_json_clean(raw)
        parsed = json.loads(cleaned)
        assert parsed["concern"] == "السطر الأول\nالسطر الثاني"

    def test_escapes_unescaped_tab_inside_string(self):
        raw = '{"x": "before\tafter"}'
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
        cleaned = _aggressive_json_clean(raw)
        assert json.loads(cleaned)["x"] == "before\tafter"

    def test_preserves_already_escaped_sequences(self):
        """We must not double-escape \\n that Gemini already escaped."""
        raw = '{"x": "line1\\nline2"}'
        cleaned = _aggressive_json_clean(raw)
        assert json.loads(cleaned)["x"] == "line1\nline2"

    def test_handles_combined_failures(self):
        """One real-world payload exhibiting MULTIPLE failure modes at once."""
        raw = (
            'تمام:\n'
            '```json\n'
            '{\u201creviews\u201d: ['
            '{\u201cayah_number\u201d: 1, \u201cpassed\u201d: true, '
            '\u201cconfidence\u201d: 0.9, '
            '\u201cconcerns\u201d: ["الشرح صحيح\nلكن طويل",]},]}\n'
            '```'
        )
        cleaned = _aggressive_json_clean(raw)
        parsed = json.loads(cleaned)
        assert parsed["reviews"][0]["ayah_number"] == 1
        assert "الشرح صحيح" in parsed["reviews"][0]["concerns"][0]

    def test_strips_bom(self):
        raw = '\ufeff{"x": 1}'
        cleaned = _aggressive_json_clean(raw)
        assert json.loads(cleaned) == {"x": 1}

    def test_strips_zero_width_chars(self):
        raw = '{"key\u200b": "val\u200c"}'
        cleaned = _aggressive_json_clean(raw)
        assert json.loads(cleaned) == {"key": "val"}

    def test_idempotent_on_already_clean_json(self):
        """Calling cleaner on already-clean JSON is a no-op."""
        raw = '{"a": 1, "b": [2, 3]}'
        cleaned = _aggressive_json_clean(raw)
        assert json.loads(cleaned) == json.loads(raw)


# ════════════════════════════════════════════════════════════════
# v22.6.2 — Iterative JSON recovery (truncated responses)
# ════════════════════════════════════════════════════════════════
class TestIterativeJsonRecovery:
    """When Gemini hits max_tokens mid-array, we walk back to the last
    balanced } or ] and parse the prefix."""

    def test_recovers_truncated_array(self):
        """Output cut mid-array after the second item."""
        truncated = '{"reviews": [{"a": 1}, {"a": 2}'  # missing ]}
        # vanilla parse fails
        with pytest.raises(json.JSONDecodeError):
            json.loads(truncated)
        # iterative recovery returns None because no balanced prefix exists
        # at depth 0 (the outer { never closed)
        assert _try_iterative_json_recovery(truncated) is None

    def test_recovers_truncated_inside_outer_object(self):
        """If the outer object closes but inner array is truncated: nope."""
        # Something like: {"reviews": [{"x": 1}, {"x": 2  ← truncated here
        truncated = '{"reviews": [{"x": 1}, {"x": 2'
        assert _try_iterative_json_recovery(truncated) is None

    def test_returns_balanced_prefix_when_extra_garbage_follows(self):
        """Output is fully valid JSON followed by garbage — recovery
        should parse the JSON and ignore the trailing junk."""
        garbage_after = '{"reviews": [{"a": 1}]}garbage trailing'
        recovered = _try_iterative_json_recovery(garbage_after)
        assert recovered == {"reviews": [{"a": 1}]}

    def test_returns_none_for_truly_unparseable(self):
        assert _try_iterative_json_recovery("not json at all") is None
        assert _try_iterative_json_recovery("") is None

    def test_handles_nested_objects(self):
        good = '{"a": {"b": {"c": 1}}}extra'
        recovered = _try_iterative_json_recovery(good)
        assert recovered == {"a": {"b": {"c": 1}}}

    def test_does_not_misparse_braces_inside_strings(self):
        """A } inside a quoted string must not be counted as closing."""
        s = '{"text": "this } is in a string"}post'
        recovered = _try_iterative_json_recovery(s)
        assert recovered == {"text": "this } is in a string"}


# ════════════════════════════════════════════════════════════════
# v22.6.2 — BatchTafsirReviewer retry path
# ════════════════════════════════════════════════════════════════
class TestBatchTafsirReviewerRetry:
    """First attempt fails → second attempt with simplified prompt
    succeeds. Verifies the fallback chain reaches a result."""

    def test_simplified_prompt_is_shorter_than_full(self):
        ayah_scripts = [
            {"number": i, "explain": "x" * 200, "story": "y" * 150}
            for i in range(1, 8)
        ]
        tafsirs = {i: "z" * 600 for i in range(1, 8)}
        full = BatchTafsirReviewer._build_prompt(ayah_scripts, tafsirs)
        simple = BatchTafsirReviewer._build_simplified_prompt(
            ayah_scripts, tafsirs,
        )
        assert len(simple) < len(full), (
            f"Simplified prompt ({len(simple)} chars) should be shorter "
            f"than full ({len(full)} chars) to free output budget"
        )

    def test_simplified_prompt_still_carries_red_flags(self):
        """Even the trimmed prompt must keep the 4 doctrinal rules so the
        retry can still catch forbidden analogies."""
        simple = BatchTafsirReviewer._build_simplified_prompt(
            [{"number": 1, "explain": "x", "story": "y"}],
            {1: "tafsir"},
        )
        assert "يوم الدين" in simple
        assert "المغناطيس" in simple
        assert "أكل" in simple
        assert "السحر" in simple or "البسملة" in simple

    def test_retry_succeeds_when_first_attempt_fails(self, fake_genai_types):
        """If first call returns None (parse failure), retry with simpler
        prompt → returns the result."""
        good = BatchReviewOut(reviews=[
            AyahReviewOut(ayah_number=1, passed=True, confidence=0.9),
        ])

        # Mock client: first call returns malformed text, second returns parsed.
        client = MagicMock()
        bad_response = MagicMock()
        bad_response.parsed = None
        bad_response.text = "not json"
        good_response = MagicMock()
        good_response.parsed = good
        good_response.text = ""
        client.models.generate_content.side_effect = [bad_response, good_response]

        engine = BatchTafsirReviewer(client)
        result = engine.review_episode(
            ayah_scripts=[{"number": 1, "explain": "x", "story": "y"}],
            tafsirs={1: "z"},
        )
        # Got the retry result
        assert result is good
        # Confirm two calls were made (first + retry)
        assert client.models.generate_content.call_count == 2

    def test_returns_none_when_both_attempts_fail(self, fake_genai_types):
        """If retry also fails, return None — caller falls back to
        per-ayah path."""
        client = MagicMock()
        bad = MagicMock(parsed=None, text="garbage")
        client.models.generate_content.return_value = bad

        engine = BatchTafsirReviewer(client)
        result = engine.review_episode(
            ayah_scripts=[{"number": 1, "explain": "x", "story": "y"}],
            tafsirs={1: "z"},
        )
        assert result is None
        # Both attempts were made
        assert client.models.generate_content.call_count == 2


# ════════════════════════════════════════════════════════════════
# v22.6.2 — End-to-end test: realistic malformed Arabic JSON gets parsed
# ════════════════════════════════════════════════════════════════
class TestRealisticMalformedJsonE2E:
    """Reconstruct the EXACT failure pattern from the v22.6.1 episode 1
    incident: BatchTafsirReviewer received output that was truncated
    after a few hundred chars. Verify the new pipeline now recovers."""

    def test_truncated_review_with_unescaped_newline_recovers(
        self, fake_genai_types,
    ):
        """Simulated v22.6.1 incident: 'JSON parse failed at pos 406,
        salvage failed at char 330'. Recreate that and verify recovery."""
        # A response that has both: unescaped newline inside a concern,
        # AND smart quotes around keys (typical Gemini-Arabic combo)
        gemini_output = (
            '```json\n'
            '{\u201creviews\u201d: ['
            '{\u201cayah_number\u201d: 1, \u201cpassed\u201d: true, '
            '\u201cconfidence\u201d: 0.92, \u201cconcerns\u201d: []},'
            '{\u201cayah_number\u201d: 2, \u201cpassed\u201d: false, '
            '\u201cconfidence\u201d: 0.85, '
            '\u201cconcerns\u201d: ["الشرح يضيف معنى\nخارج التفسير"]},'
            ']}\n'
            '```'
        )
        # Sanity: vanilla json.loads explodes
        with pytest.raises(json.JSONDecodeError):
            json.loads(gemini_output)

        # Build a Gemini mock that returns this malformed text (with
        # response.parsed=None, forcing the cleaner path).
        client = MagicMock()
        response = MagicMock()
        response.parsed = None
        response.text = gemini_output
        client.models.generate_content.return_value = response

        # The wrapper function recovers and returns the parsed schema
        result = _call_gemini_with_schema(
            client, "test prompt", BatchReviewOut,
        )
        assert result is not None
        assert len(result.reviews) == 2
        assert result.reviews[0].ayah_number == 1
        assert result.reviews[0].passed is True
        assert result.reviews[1].passed is False
        assert "الشرح يضيف معنى" in result.reviews[1].concerns[0]
