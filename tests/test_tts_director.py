"""Tests for tts_director (v22.5)."""
import pytest
from unittest.mock import MagicMock

from engines.tts_director import (
    TTSDirector, SegmentDirection, EpisodeDirection,
)


# ════════════════════════════════════════════════════════════════
# SegmentDirection
# ════════════════════════════════════════════════════════════════
class TestSegmentDirection:
    def test_to_elevenlabs_strips_comments(self):
        seg = SegmentDirection(
            segment_id="x", original_text="hello",
            directed_text='hello <!-- internal note --> world',
        )
        assert seg.to_elevenlabs_input() == "hello world"

    def test_to_elevenlabs_normalizes_whitespace(self):
        seg = SegmentDirection(
            segment_id="x", original_text="hello",
            directed_text="hello   world\n\nfoo",
        )
        assert seg.to_elevenlabs_input() == "hello world foo"

    def test_to_elevenlabs_keeps_break_tags(self):
        """Break tags must NOT be stripped — ElevenLabs uses them."""
        seg = SegmentDirection(
            segment_id="x", original_text="hello",
            directed_text='hello <break time="500ms"/> world',
        )
        result = seg.to_elevenlabs_input()
        assert '<break time="500ms"/>' in result


# ════════════════════════════════════════════════════════════════
# TTSDirector — using mocked adapter
# ════════════════════════════════════════════════════════════════
class TestTTSDirector:
    def test_requires_adapter(self):
        with pytest.raises(ValueError, match="requires a Gemini adapter"):
            TTSDirector(gemini_adapter=None)

    def test_empty_episode_returns_no_segments(self):
        adapter = MagicMock()
        director = TTSDirector(gemini_adapter=adapter)
        episode = {"ayah_scenes": []}
        result = director.direct_episode(episode)
        assert result.segments == {}
        adapter.generate_json.assert_not_called()

    def test_directs_intro_outro_cta(self):
        adapter = MagicMock()
        adapter.generate_json.return_value = {
            "directions": {
                "intro_text": {
                    "directed_text": 'تخيل لو <break time="300ms"/> ربنا قال',
                    "pace": "normal",
                    "pace_reason": "introduction needs balance",
                    "pronunciation_notes": [],
                },
                "outro_text": {
                    "directed_text": 'فكر معايا <break time="500ms"/> في الكلام ده',
                    "pace": "slow",
                    "pace_reason": "moral takeaway needs reflection",
                    "pronunciation_notes": [],
                },
                "cta_text": {
                    "directed_text": "اشترك في القناة",
                    "pace": "normal",
                    "pace_reason": "friendly invitation",
                    "pronunciation_notes": [],
                },
            }
        }

        episode = {
            "intro_text": "تخيل لو ربنا قال",
            "outro_text": "فكر معايا في الكلام ده",
            "cta_text": "اشترك في القناة",
            "ayah_scenes": [],
        }

        director = TTSDirector(gemini_adapter=adapter)
        result = director.direct_episode(episode)

        assert "intro_text" in result.segments
        assert "outro_text" in result.segments
        assert "cta_text" in result.segments
        assert result.segments["outro_text"].pace == "slow"

        # Check episode_data was modified
        assert "intro_text_directed" in episode
        assert "<break" in episode["intro_text_directed"]
        assert episode["outro_text_pace"] == "slow"

    def test_directs_per_scene_segments(self):
        adapter = MagicMock()
        adapter.generate_json.return_value = {
            "directions": {
                "scene1.hook_text": {
                    "directed_text": 'ايه اللي بيخلّي <break time="300ms"/> النحلة تطير؟',
                    "pace": "normal",
                    "pace_reason": "curiosity hook",
                    "pronunciation_notes": [],
                },
                "scene1.moral_text": {
                    "directed_text": 'كل واحد <break time="800ms"/> عنده دور',
                    "pace": "slow",
                    "pace_reason": "moral takeaway",
                    "pronunciation_notes": [],
                },
            }
        }

        episode = {
            "ayah_scenes": [
                {
                    "hook_text": "ايه اللي بيخلّي النحلة تطير؟",
                    "moral_text": "كل واحد عنده دور",
                    "scene_emotion": "warm",
                },
            ],
        }

        director = TTSDirector(gemini_adapter=adapter)
        result = director.direct_episode(episode)

        assert "scene1.hook_text" in result.segments
        assert "scene1.moral_text" in result.segments

        # Check scene was modified
        scene = episode["ayah_scenes"][0]
        assert "hook_text_directed" in scene
        assert scene["hook_text_pace"] == "normal"
        assert "moral_text_directed" in scene
        assert scene["moral_text_pace"] == "slow"

    def test_gemini_failure_falls_back_to_original(self):
        adapter = MagicMock()
        adapter.generate_json.side_effect = Exception("Gemini died")

        episode = {
            "intro_text": "النص الأصلي",
            "ayah_scenes": [
                {
                    "hook_text": "هوك أصلي",
                    "scene_emotion": "warm",
                },
            ],
        }

        director = TTSDirector(gemini_adapter=adapter)
        result = director.direct_episode(episode, max_retries=1)

        assert result.fallback_used
        # Original text preserved (no breaks)
        assert result.segments["intro_text"].directed_text == "النص الأصلي"
        assert result.segments["intro_text"].pace == "normal"

    def test_directed_text_too_different_uses_original(self):
        """If Gemini returns rewritten text (not just adding breaks), reject."""
        adapter = MagicMock()
        adapter.generate_json.return_value = {
            "directions": {
                "intro_text": {
                    # Completely rewritten — much shorter
                    "directed_text": "نص قصير",
                    "pace": "normal",
                    "pace_reason": "x",
                    "pronunciation_notes": [],
                }
            }
        }

        episode = {
            "intro_text": "هذا نص طويل أصلاً ومن المفترض أن يبقى كما هو مع إضافة فواصل فقط",
            "ayah_scenes": [],
        }

        director = TTSDirector(gemini_adapter=adapter)
        result = director.direct_episode(episode)

        # Should fall back to original because Gemini rewrote too much
        assert result.segments["intro_text"].directed_text == episode["intro_text"]

    def test_text_preservation_check_allows_breaks(self):
        """Adding <break> tags should be allowed (no rejection)."""
        original = "كلمة كلمة كلمة كلمة كلمة"
        with_breaks = 'كلمة كلمة <break time="300ms"/> كلمة كلمة كلمة'
        assert TTSDirector._direction_preserves_text(original, with_breaks)

    def test_text_preservation_check_rejects_rewrite(self):
        original = "هذا نص أصلي طويل به كلمات كثيرة"
        rewritten = "نص"
        assert not TTSDirector._direction_preserves_text(original, rewritten)

    def test_text_preservation_check_rejects_empty(self):
        assert not TTSDirector._direction_preserves_text("text", "")

    def test_directs_all_scene_field_types(self):
        """All 5 segment types per scene should be processed."""
        adapter = MagicMock()
        # Build a response that handles all 5 types for scene1
        directions = {}
        for kind in ("hook_text", "story_text", "explain_text",
                     "analogy_text", "moral_text"):
            directions[f"scene1.{kind}"] = {
                "directed_text": f"directed-{kind}",
                "pace": "normal",
                "pace_reason": "test",
                "pronunciation_notes": [],
            }
        adapter.generate_json.return_value = {"directions": directions}

        episode = {
            "ayah_scenes": [
                {
                    "hook_text": "directed-hook_text",  # match the directed text
                    "story_text": "directed-story_text",
                    "explain_text": "directed-explain_text",
                    "analogy_text": "directed-analogy_text",
                    "moral_text": "directed-moral_text",
                    "scene_emotion": "warm",
                },
            ],
        }

        director = TTSDirector(gemini_adapter=adapter)
        result = director.direct_episode(episode)

        for kind in ("hook_text", "story_text", "explain_text",
                     "analogy_text", "moral_text"):
            seg_id = f"scene1.{kind}"
            assert seg_id in result.segments
