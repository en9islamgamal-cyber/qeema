"""Tests for v22.1 quality modules: voice_emotion_mapper, script_polisher."""
import pytest

from engines.voice_emotion_mapper import (
    get_voice_settings, VoiceSettings, list_segment_types, list_emotions,
    PRESETS, EMOTION_FALLBACKS, DEFAULT_FALLBACK,
)
from engines.script_polisher import (
    ScriptPolisher, polish_script, PolishReport,
    BANNED_PATTERNS, MSA_MARKERS, EGYPTIAN_MARKERS,
)
from engines.script_engine_v20 import (
    build_full_episode_prompt, parse_full_episode_response,
    BANNED_PHRASES, HOOK_EXAMPLES,
)


# ════════════════════════════════════════════════════════════════
# VoiceSettings dataclass
# ════════════════════════════════════════════════════════════════
class TestVoiceSettings:
    def test_valid_construction(self):
        s = VoiceSettings(stability=0.5, similarity=0.85, style=0.5, speed=1.0)
        assert s.stability == 0.5

    def test_invalid_stability_rejected(self):
        with pytest.raises(ValueError):
            VoiceSettings(stability=1.5, similarity=0.85, style=0.5, speed=1.0)
        with pytest.raises(ValueError):
            VoiceSettings(stability=-0.1, similarity=0.85, style=0.5, speed=1.0)

    def test_invalid_speed_rejected(self):
        with pytest.raises(ValueError):
            VoiceSettings(stability=0.5, similarity=0.85, style=0.5, speed=3.0)
        with pytest.raises(ValueError):
            VoiceSettings(stability=0.5, similarity=0.85, style=0.5, speed=0.3)

    def test_as_dict(self):
        s = VoiceSettings(stability=0.5, similarity=0.85, style=0.5, speed=1.0)
        d = s.as_dict()
        assert d["stability"] == 0.5
        assert d["similarity_boost"] == 0.85
        assert d["style"] == 0.5
        assert d["speed"] == 1.0

    def test_immutable(self):
        s = VoiceSettings(stability=0.5, similarity=0.85, style=0.5, speed=1.0)
        with pytest.raises(Exception):
            s.stability = 0.9  # type: ignore


# ════════════════════════════════════════════════════════════════
# get_voice_settings
# ════════════════════════════════════════════════════════════════
class TestGetVoiceSettings:
    def test_exact_combo_match(self):
        s = get_voice_settings(segment_type="hook", emotion="excited")
        # Excited hooks should have low stability, high style
        assert s.stability < 0.5
        assert s.style > 0.5
        assert s.speed == 1.0  # target handoff preset

    def test_moral_is_calmer_than_hook(self):
        hook = get_voice_settings(segment_type="hook", emotion="warm")
        moral = get_voice_settings(segment_type="moral", emotion="warm")
        assert moral.stability > hook.stability  # more stable
        assert moral.style < hook.style          # less dramatic
        assert moral.speed < hook.speed          # slower

    def test_explain_more_stable_than_hook(self):
        hook = get_voice_settings(segment_type="hook", emotion="warm")
        explain = get_voice_settings(segment_type="explain", emotion="warm")
        assert explain.stability > hook.stability

    def test_unknown_segment_falls_back_to_emotion(self):
        s = get_voice_settings(segment_type="nonexistent", emotion="warm")
        # Should fall back to bare warm emotion
        assert s == EMOTION_FALLBACKS["warm"]

    def test_unknown_emotion_falls_back_to_default(self):
        s = get_voice_settings(segment_type="hook", emotion="nonexistent")
        # Should be default
        assert s == DEFAULT_FALLBACK

    def test_use_adaptive_false_returns_default(self):
        s = get_voice_settings(
            segment_type="hook", emotion="excited", use_adaptive=False,
        )
        assert s == DEFAULT_FALLBACK

    def test_use_adaptive_false_honors_override(self):
        custom = VoiceSettings(stability=0.99, similarity=0.5, style=0.0, speed=0.8)
        s = get_voice_settings(
            segment_type="hook", emotion="excited",
            use_adaptive=False, static_default=custom,
        )
        assert s == custom


# ════════════════════════════════════════════════════════════════
# Voice mapper exhaustiveness
# ════════════════════════════════════════════════════════════════
class TestPresetCoverage:
    def test_all_segment_types_covered(self):
        types = list_segment_types()
        assert "hook" in types
        assert "story" in types
        assert "explain" in types
        assert "moral" in types

    def test_main_emotions_present(self):
        emotions = list_emotions()
        assert "warm" in emotions
        assert "reverent" in emotions
        assert "playful" in emotions
        assert "peaceful" in emotions
        assert "excited" in emotions

    def test_quran_voice_inappropriate_for_kids_segments(self):
        """Hook for kids should NOT be reverent-low-style."""
        s = get_voice_settings(segment_type="hook", emotion="playful")
        # Playful hook should NOT be like Quran recitation
        assert s.style >= 0.6


# ════════════════════════════════════════════════════════════════
# ScriptPolisher
# ════════════════════════════════════════════════════════════════
class TestScriptPolisher:
    def _sample_script(self) -> dict:
        """Build a minimal valid script dict."""
        return {
            "title": "title",
            "youtube_title": "yt",
            "youtube_description": "description",
            "intro_text": "تخيل لو السماء بترسم",
            "outro_text": "ربنا يحفظكم",
            "cta_text": "اشترك في القناة",
            "ayah_scenes": [
                {
                    "ayah_number": 1,
                    "hook_text": "ايه اللي بيخلّي النحلة تطير؟",
                    "intro_text": "خلّيك معايا",
                    "analogy_text": "النحلة بتطير من 200 وردة",
                    "explain_text": "ربنا قال في الآية ده",
                    "moral_text": "كل واحد عنده دور",
                    "scene_emotion": "warm",
                }
            ],
        }

    def test_clean_script_no_issues(self):
        script = self._sample_script()
        _, report = ScriptPolisher.polish(script)
        assert not report.banned_phrases
        assert not report.long_sentences

    def test_detects_banned_phrase(self):
        script = self._sample_script()
        script["intro_text"] = "أحبائي، اليوم هنتكلم عن السورة"
        _, report = ScriptPolisher.polish(script)
        assert len(report.banned_phrases) >= 1
        assert any("أحبائي" in b for b in report.banned_phrases)

    def test_detects_long_sentence(self):
        script = self._sample_script()
        # 20-word sentence
        long_text = " ".join(["كلمة"] * 20)
        script["ayah_scenes"][0]["analogy_text"] = long_text
        _, report = ScriptPolisher.polish(script)
        assert len(report.long_sentences) >= 1

    def test_detects_msa_leakage(self):
        script = self._sample_script()
        script["intro_text"] = "إنّ الله تعالى يخبرنا أيها الإخوة"
        # 'تعالى' appears once, but 'أيها' is direct MSA
        # The marker check needs >= 2 occurrences
        script["outro_text"] = "إنّ الإيمان عظيم. إنّ الله رحيم."
        _, report = ScriptPolisher.polish(script)
        # 'إنّ' appears 2+ times → flagged
        assert len(report.msa_leakage) >= 1

    def test_egyptian_score_high_for_dialect(self):
        script = self._sample_script()
        script["intro_text"] = (
            "تخيل بقى لو ربنا قال لينا الكلام ده ازاي؟ "
            "كده يعني ايه؟ علشان كل لما تقرا الآية..."
        )
        _, report = ScriptPolisher.polish(script)
        assert report.egyptian_score > 0.3  # noticeable presence

    def test_egyptian_score_zero_for_msa(self):
        script = self._sample_script()
        script["intro_text"] = "إن الله تعالى ذكر في كتابه الكريم"
        script["outro_text"] = "والحمد لله رب العالمين"
        script["cta_text"] = "نسألكم الاشتراك في القناة"
        for scene in script["ayah_scenes"]:
            for k in ["hook_text", "intro_text", "analogy_text",
                      "explain_text", "moral_text"]:
                scene[k] = "نص فصيح بدون عامية مصرية"
        _, report = ScriptPolisher.polish(script)
        assert report.egyptian_score < 0.3

    def test_fix_aly_typo(self):
        script = self._sample_script()
        script["intro_text"] = "اللى بيقول حاجة لازم يفهم"
        polished, report = ScriptPolisher.polish(script, apply_fixes=True)
        assert "اللي" in polished["intro_text"]
        assert "اللى " not in polished["intro_text"]
        assert any("اللى" in f for f in report.fixes_applied)

    def test_no_fixes_when_apply_false(self):
        script = self._sample_script()
        script["intro_text"] = "اللى بيقول حاجة"
        polished, report = ScriptPolisher.polish(script, apply_fixes=False)
        assert "اللى " in polished["intro_text"]  # NOT fixed
        assert report.fixes_applied == []

    def test_normalizes_whitespace(self):
        script = self._sample_script()
        script["intro_text"] = "  نص   فيه    مسافات   كتير  "
        polished, _ = ScriptPolisher.polish(script, apply_fixes=True)
        assert polished["intro_text"] == "نص فيه مسافات كتير"

    def test_report_summary(self):
        script = self._sample_script()
        script["intro_text"] = "أحبائي" + " كلمة" * 20
        _, report = ScriptPolisher.polish(script)
        summary = report.summary()
        assert "Polish Report" in summary
        assert report.has_issues


# ════════════════════════════════════════════════════════════════
# Multi-task prompt builder
# ════════════════════════════════════════════════════════════════
class TestPromptBuilder:
    def test_prompt_includes_banned_phrases(self):
        prompt = build_full_episode_prompt(
            surah_name="الفاتحة", surah_number=1,
            ayahs=[{"number": 1, "text": "بسم الله الرحمن الرحيم"}],
        )
        # At least some banned phrases should appear in the prompt
        # (so LLM knows what to avoid)
        assert "أحبائي" in prompt
        assert "ممنوعة" in prompt

    def test_prompt_includes_hook_example(self):
        prompt = build_full_episode_prompt(
            surah_name="الفاتحة", surah_number=1,
            ayahs=[{"number": 1, "text": "test"}],
            hook_strategy_hint="vivid metaphor from nature",
        )
        # Should include the concrete metaphor example
        assert "تخيل" in prompt or "البحر" in prompt

    def test_prompt_emphasizes_egyptian_dialect(self):
        prompt = build_full_episode_prompt(
            surah_name="test", surah_number=1, ayahs=[{"number": 1, "text": "x"}],
        )
        assert "عامية مصرية" in prompt
        assert "فصحى" in prompt  # should explicitly contrast

    def test_prompt_includes_self_check(self):
        prompt = build_full_episode_prompt(
            surah_name="test", surah_number=1, ayahs=[{"number": 1, "text": "x"}],
        )
        assert "Self-Check" in prompt or "افحصهم" in prompt

    def test_prompt_includes_sentence_length_constraint(self):
        prompt = build_full_episode_prompt(
            surah_name="test", surah_number=1, ayahs=[{"number": 1, "text": "x"}],
        )
        assert "12 كلمة" in prompt


# ════════════════════════════════════════════════════════════════
# Response parser
# ════════════════════════════════════════════════════════════════
class TestResponseParser:
    def _valid_response(self, num_ayahs: int = 1) -> dict:
        return {
            "title": "title",
            "youtube_title": "yt title",
            "youtube_description": "description",
            "intro_text": "intro",
            "outro_text": "outro",
            "youtube_tags": ["tag1"],
            "ayah_scenes": [
                {
                    "ayah_number": i + 1,
                    "hook_text": f"hook {i}",
                    "explain_text": f"explain {i}",
                    "moral_text": f"moral {i}",
                    "scene_emotion": "warm",
                }
                for i in range(num_ayahs)
            ],
        }

    def test_valid_response_passes(self):
        data = self._valid_response(num_ayahs=2)
        ayahs = [{"number": i + 1, "text": f"ayah {i}"} for i in range(2)]
        result = parse_full_episode_response(data, ayahs)
        assert result == data

    def test_missing_top_level_fails(self):
        data = self._valid_response()
        del data["title"]
        with pytest.raises(ValueError, match="Missing"):
            parse_full_episode_response(data, [{"number": 1, "text": "x"}])

    def test_wrong_scene_count_fails(self):
        data = self._valid_response(num_ayahs=2)
        ayahs = [{"number": 1, "text": "x"}]  # only 1 expected
        with pytest.raises(ValueError, match="Expected"):
            parse_full_episode_response(data, ayahs)

    def test_invalid_emotion_normalized(self):
        data = self._valid_response()
        data["ayah_scenes"][0]["scene_emotion"] = "INVALID"
        ayahs = [{"number": 1, "text": "x"}]
        result = parse_full_episode_response(data, ayahs)
        assert result["ayah_scenes"][0]["scene_emotion"] == "warm"

    def test_emotion_lowercase_normalized(self):
        data = self._valid_response()
        data["ayah_scenes"][0]["scene_emotion"] = "WARM"
        ayahs = [{"number": 1, "text": "x"}]
        result = parse_full_episode_response(data, ayahs)
        assert result["ayah_scenes"][0]["scene_emotion"] == "warm"
