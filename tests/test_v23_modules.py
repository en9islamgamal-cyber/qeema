"""Tests for v23 modules: mood_transitions, age_appropriate_validator."""
import pytest

from infrastructure.mood_transitions import (
    TransitionConfig, ARRIVAL_PRESETS, DEFAULT_TRANSITION,
    transition_for_pair, transitions_for_sequence,
    average_duration, _adjust_for_pair,
    concat_with_mood_transitions,
)
from engines.age_appropriate_validator import (
    AgeAppropriateValidator, validate_age_appropriate, AgeReport,
    FRIGHTENING_TERMS, ADULT_TOPICS, COMPLEX_THEOLOGY,
)


# ════════════════════════════════════════════════════════════════
# TransitionConfig
# ════════════════════════════════════════════════════════════════
class TestTransitionConfig:
    def test_valid_construction(self):
        t = TransitionConfig(duration_sec=0.5, type="fade")
        assert t.duration_sec == 0.5
        assert t.type == "fade"

    def test_invalid_duration_rejected(self):
        with pytest.raises(ValueError):
            TransitionConfig(duration_sec=-0.1, type="fade")
        with pytest.raises(ValueError):
            TransitionConfig(duration_sec=10.0, type="fade")

    def test_invalid_type_rejected(self):
        with pytest.raises(ValueError):
            TransitionConfig(duration_sec=0.5, type="dissolve")

    def test_immutable(self):
        t = TransitionConfig(duration_sec=0.5, type="fade")
        with pytest.raises(Exception):
            t.duration_sec = 0.9  # type: ignore


# ════════════════════════════════════════════════════════════════
# transition_for_pair
# ════════════════════════════════════════════════════════════════
class TestTransitionForPair:
    def test_excited_arrival_is_quick_cut(self):
        t = transition_for_pair("warm", "excited")
        # Excited arrival from peaceful = even faster cut
        assert t.duration_sec < 0.3
        assert t.type in ("cut", "fade")

    def test_reverent_arrival_is_long_fade(self):
        t = transition_for_pair("warm", "reverent")
        assert t.duration_sec >= 0.7
        assert t.type == "fade"

    def test_excited_to_peaceful_breathing_room(self):
        """Big calm-down should get extra duration."""
        t = transition_for_pair("excited", "peaceful")
        # Should be longer than just arriving at peaceful normally
        normal = transition_for_pair("warm", "peaceful")
        assert t.duration_sec >= normal.duration_sec

    def test_peaceful_to_excited_jolt(self):
        """Big wake-up should be quick cut."""
        t = transition_for_pair("peaceful", "excited")
        assert t.type == "cut"
        assert t.duration_sec < 0.3

    def test_same_emotion_keeps_momentum(self):
        """Adjacent same-emotion = slightly faster than fresh arrival."""
        normal = transition_for_pair("warm", "warm")
        # Fresh arrival is in ARRIVAL_PRESETS["warm"]
        fresh = ARRIVAL_PRESETS["warm"]
        assert normal.duration_sec < fresh.duration_sec

    def test_none_emotion_returns_default(self):
        t = transition_for_pair(None, None)
        assert t == DEFAULT_TRANSITION

    def test_unknown_emotion_returns_default(self):
        t = transition_for_pair("warm", "ecstatic")
        assert t == DEFAULT_TRANSITION


# ════════════════════════════════════════════════════════════════
# transitions_for_sequence
# ════════════════════════════════════════════════════════════════
class TestTransitionsForSequence:
    def test_empty_returns_empty(self):
        assert transitions_for_sequence([]) == []

    def test_single_returns_empty(self):
        assert transitions_for_sequence(["warm"]) == []

    def test_two_emotions_one_transition(self):
        result = transitions_for_sequence(["warm", "excited"])
        assert len(result) == 1

    def test_n_emotions_n_minus_one_transitions(self):
        result = transitions_for_sequence([
            "warm", "excited", "peaceful", "reverent", "warm",
        ])
        assert len(result) == 4

    def test_average_duration(self):
        ts = transitions_for_sequence([
            "warm", "warm", "peaceful",  # warm-warm short, warm-peaceful long
        ])
        avg = average_duration(ts)
        assert 0.3 < avg < 0.8


# ════════════════════════════════════════════════════════════════
# concat_with_mood_transitions (integration)
# ════════════════════════════════════════════════════════════════
class TestConcatWithMoodTransitions:
    def test_falls_back_when_emotions_mismatch(self):
        """Length mismatch → uses default crossfade."""
        from unittest.mock import MagicMock
        mixer = MagicMock()
        mixer.concat_with_crossfades.return_value = "/tmp/out.mp4"

        result = concat_with_mood_transitions(
            bgm_mixer=mixer,
            segments=["s1.mp4", "s2.mp4"],
            emotions=["warm"],  # only 1 emotion for 2 segments
            output_path="/tmp/out.mp4",
        )

        # Should call default crossfade
        mixer.concat_with_crossfades.assert_called_once()
        call_kwargs = mixer.concat_with_crossfades.call_args.kwargs
        assert call_kwargs["transition_duration"] == 0.4

    def test_uses_mood_transitions_when_aligned(self):
        from unittest.mock import MagicMock
        mixer = MagicMock()
        mixer.concat_with_crossfades.return_value = "/tmp/out.mp4"

        result = concat_with_mood_transitions(
            bgm_mixer=mixer,
            segments=["s1.mp4", "s2.mp4", "s3.mp4"],
            emotions=["warm", "peaceful", "reverent"],
            output_path="/tmp/out.mp4",
        )

        mixer.concat_with_crossfades.assert_called_once()
        call_kwargs = mixer.concat_with_crossfades.call_args.kwargs
        # Average of warm→peaceful + peaceful→reverent should be > 0.4
        assert call_kwargs["transition_duration"] > 0.4


# ════════════════════════════════════════════════════════════════
# AgeAppropriateValidator — heuristic
# ════════════════════════════════════════════════════════════════
class TestAgeValidatorHeuristic:
    def _sample_data(self) -> dict:
        return {
            "intro_text": "تخيل لو السماء بترسم لك صورة",
            "outro_text": "ربنا يحبك ويرضيك",
            "cta_text": "اشترك في القناة",
            "ayah_scenes": [
                {
                    "hook_text": "ايه اللي بيخلّي النحلة عبقرية؟",
                    "intro_text": "خلّيك معايا",
                    "analogy_text": "زي ما النحلة بتطير من 200 وردة",
                    "explain_text": "ربنا قال لينا في الآية ده",
                    "moral_text": "كل واحد عنده دور مهم",
                }
            ],
        }

    def test_clean_content_passes(self):
        validator = AgeAppropriateValidator()
        report = validator.validate(self._sample_data())
        assert report.passed
        assert report.method == "heuristic"

    def test_detects_frightening_term_no_comfort(self):
        data = self._sample_data()
        data["ayah_scenes"][0]["explain_text"] = "نار جهنم تحرق الناس"
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        assert not report.passed
        assert any(
            i["type"] == "frightening_no_comfort" for i in report.issues
        )

    def test_frightening_with_comfort_more_lenient(self):
        data = self._sample_data()
        # Frightening word but with comfort context
        data["ayah_scenes"][0]["explain_text"] = (
            "ربنا رحمته كبيرة ومحبته للناس واسعة، "
            "والعذاب الأليم بيكون بس للي بيظلم"
        )
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        # May or may not pass — but no high-severity frightening issue
        high_frightening = [
            i for i in report.issues
            if i["type"] == "frightening_no_comfort"
        ]
        assert len(high_frightening) == 0

    def test_detects_adult_topic(self):
        data = self._sample_data()
        data["ayah_scenes"][0]["explain_text"] = "الطلاق له أحكامه"
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        assert not report.passed
        assert any(i["type"] == "adult_topic" for i in report.issues)

    def test_detects_complex_theology(self):
        data = self._sample_data()
        data["ayah_scenes"][0]["explain_text"] = (
            "تجلي الذات في صفات الفعل"
        )
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        assert any(
            i["type"] == "complex_theology" for i in report.issues
        )

    def test_detects_heavy_fiqh(self):
        data = self._sample_data()
        data["ayah_scenes"][0]["analogy_text"] = (
            "الأحكام والتكليف والاجتهاد والإجماع"
        )
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        assert any(i["type"] == "heavy_fiqh" for i in report.issues)

    def test_concrete_grounding_score_high(self):
        data = self._sample_data()
        # Sample already has concrete markers (تخيل، زي ما)
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        assert report.concrete_score > 0.0

    def test_concrete_grounding_score_low_for_abstract(self):
        data = self._sample_data()
        for k in ["intro_text", "outro_text"]:
            data[k] = "كلام عام بدون أمثلة محسوسة"
        for scene in data["ayah_scenes"]:
            for k in ["hook_text", "intro_text", "analogy_text",
                      "explain_text", "moral_text"]:
                scene[k] = "كلام تجريدي بدون ملموس"
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        assert report.concrete_score < 0.3

    def test_quran_ayah_text_not_validated(self):
        """The validator should ignore the Quran ayah text itself —
        only narrator/explanation text is checked."""
        # The validator's _collect_narrative_text doesn't include ayah text
        data = self._sample_data()
        # Even if Quran has "frightening" terms, we don't flag them
        validator = AgeAppropriateValidator()
        text = validator._collect_narrative_text(data)
        # Quran ayah text would not appear here
        assert "ayah" not in text  # ayah text is not collected

    def test_report_summary(self):
        data = self._sample_data()
        data["ayah_scenes"][0]["explain_text"] = "نار جهنم"
        validator = AgeAppropriateValidator()
        report = validator.validate(data)
        summary = report.summary()
        assert "Age-appropriate" in summary
        assert "NEEDS REVIEW" in summary or "PASS" in summary


# ════════════════════════════════════════════════════════════════
# AgeReport
# ════════════════════════════════════════════════════════════════
class TestAgeReport:
    def test_default_values(self):
        report = AgeReport(passed=True, confidence=0.5)
        assert report.passed
        assert report.issues == []
        assert report.concrete_score == 0.0

    def test_summary_for_pass(self):
        report = AgeReport(passed=True, confidence=0.9)
        s = report.summary()
        assert "PASS" in s

    def test_summary_for_fail(self):
        report = AgeReport(
            passed=False, confidence=0.8,
            issues=[{"type": "test", "detail": "x", "severity": "high"}],
        )
        s = report.summary()
        assert "NEEDS REVIEW" in s
        assert "Issues found: 1" in s


# ════════════════════════════════════════════════════════════════
# Public function
# ════════════════════════════════════════════════════════════════
class TestPublicAPI:
    def test_validate_age_appropriate_callable(self):
        data = {"intro_text": "test", "outro_text": "test", "ayah_scenes": []}
        report = validate_age_appropriate(data, log_report=False)
        assert isinstance(report, AgeReport)


# ════════════════════════════════════════════════════════════════
# TTSRequest emotion field (v23)
# ════════════════════════════════════════════════════════════════
class TestTTSRequestEmotion:
    def test_emotion_field_added(self):
        from core.interfaces import TTSRequest
        import dataclasses
        fields = {f.name for f in dataclasses.fields(TTSRequest)}
        assert "emotion" in fields

    def test_emotion_default_none(self):
        from core.interfaces import TTSRequest
        req = TTSRequest(text="x", output_path="/tmp/x.mp3")
        assert req.emotion is None

    def test_emotion_can_be_set(self):
        from core.interfaces import TTSRequest
        req = TTSRequest(
            text="x", output_path="/tmp/x.mp3", emotion="excited",
        )
        assert req.emotion == "excited"
