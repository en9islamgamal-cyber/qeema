"""Tests for v22.2 BGM director."""
import pytest

from infrastructure.bgm_director import (
    BGMDirector, VolumeSegment, VOLUME_DEFAULTS,
)


# ════════════════════════════════════════════════════════════════
# VolumeSegment validation
# ════════════════════════════════════════════════════════════════
class TestVolumeSegment:
    def test_valid_construction(self):
        s = VolumeSegment(start_sec=0.0, end_sec=5.0, volume=0.06)
        assert s.duration == 5.0

    def test_negative_start_rejected(self):
        with pytest.raises(ValueError):
            VolumeSegment(start_sec=-1.0, end_sec=5.0, volume=0.06)

    def test_end_before_start_rejected(self):
        with pytest.raises(ValueError):
            VolumeSegment(start_sec=10.0, end_sec=5.0, volume=0.06)

    def test_volume_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            VolumeSegment(start_sec=0.0, end_sec=5.0, volume=1.5)
        with pytest.raises(ValueError):
            VolumeSegment(start_sec=0.0, end_sec=5.0, volume=-0.1)

    def test_zero_duration_allowed(self):
        s = VolumeSegment(start_sec=5.0, end_sec=5.0, volume=0.06)
        assert s.duration == 0.0


# ════════════════════════════════════════════════════════════════
# BGMDirector.select_volume
# ════════════════════════════════════════════════════════════════
class TestSelectVolume:
    def test_ayah_gets_dramatic_duck(self):
        # Ayah recitation should be quietest
        v = BGMDirector.select_volume(is_ayah=True)
        assert v < 0.04
        # Even with explicit emotion, ayah override wins
        v2 = BGMDirector.select_volume(is_ayah=True, emotion="excited")
        assert v2 < 0.04

    def test_hook_louder_than_explain(self):
        hook = BGMDirector.select_volume(segment="hook")
        explain = BGMDirector.select_volume(segment="explain")
        assert hook > explain

    def test_moral_quieter_than_hook(self):
        moral = BGMDirector.select_volume(segment="moral")
        hook = BGMDirector.select_volume(segment="hook")
        assert moral < hook

    def test_ayah_quieter_than_anything(self):
        ayah = BGMDirector.select_volume(segment="ayah")
        for seg in ["hook", "explain", "moral", "intro", "outro"]:
            other = BGMDirector.select_volume(segment=seg)
            assert ayah < other, f"ayah ({ayah}) should be < {seg} ({other})"

    def test_segment_takes_precedence_over_emotion(self):
        # If both given, segment wins (more specific)
        v = BGMDirector.select_volume(emotion="excited", segment="moral")
        # moral default takes precedence
        assert v == VOLUME_DEFAULTS["moral"]

    def test_emotion_used_when_segment_unknown(self):
        v = BGMDirector.select_volume(segment="unknown", emotion="peaceful")
        assert v == VOLUME_DEFAULTS["peaceful"]

    def test_default_when_nothing_known(self):
        v = BGMDirector.select_volume()
        assert v == VOLUME_DEFAULTS["default"]

    def test_default_when_all_unknown(self):
        v = BGMDirector.select_volume(emotion="alien", segment="alien")
        assert v == VOLUME_DEFAULTS["default"]


# ════════════════════════════════════════════════════════════════
# BGMDirector.plan_episode_curve
# ════════════════════════════════════════════════════════════════
class TestPlanCurve:
    def test_empty_scenes(self):
        curve = BGMDirector.plan_episode_curve([])
        assert curve == []

    def test_single_scene(self):
        scenes = [
            {"duration_sec": 5.0, "emotion": "warm", "segment": "intro"}
        ]
        curve = BGMDirector.plan_episode_curve(scenes)
        assert len(curve) == 1
        assert curve[0].start_sec == 0.0
        assert curve[0].end_sec == 5.0

    def test_multiple_scenes_continuous_timeline(self):
        scenes = [
            {"duration_sec": 5.0, "emotion": "excited", "segment": "intro"},
            {"duration_sec": 8.0, "emotion": "warm", "segment": "explain"},
            {"duration_sec": 6.0, "emotion": "reverent", "segment": "ayah",
             "is_ayah": True},
            {"duration_sec": 4.0, "emotion": "peaceful", "segment": "moral"},
        ]
        curve = BGMDirector.plan_episode_curve(scenes)
        assert len(curve) == 4
        # Timeline continuity
        assert curve[0].start_sec == 0.0
        assert curve[0].end_sec == 5.0
        assert curve[1].start_sec == 5.0
        assert curve[1].end_sec == 13.0
        assert curve[2].start_sec == 13.0
        assert curve[3].end_sec == 23.0

        # Ayah should be quietest segment
        ayah_vol = curve[2].volume
        for i, s in enumerate(curve):
            if i != 2:
                assert s.volume > ayah_vol, (
                    f"Segment {i} volume {s.volume} should be > "
                    f"ayah volume {ayah_vol}"
                )

    def test_skips_zero_duration_scenes(self):
        scenes = [
            {"duration_sec": 5.0, "emotion": "warm", "segment": "intro"},
            {"duration_sec": 0.0, "emotion": "warm", "segment": "intro"},
            {"duration_sec": 3.0, "emotion": "warm", "segment": "explain"},
        ]
        curve = BGMDirector.plan_episode_curve(scenes)
        assert len(curve) == 2

    def test_ayah_duck_visible_in_curve(self):
        """End-to-end: a typical episode should have ayah dropouts."""
        scenes = [
            {"duration_sec": 5.0, "segment": "intro", "emotion": "excited"},
            {"duration_sec": 8.0, "segment": "hook",  "emotion": "playful"},
            {"duration_sec": 12.0, "segment": "story", "emotion": "warm"},
            {"duration_sec": 7.0, "segment": "ayah", "is_ayah": True},
            {"duration_sec": 10.0, "segment": "explain", "emotion": "warm"},
            {"duration_sec": 5.0, "segment": "moral", "emotion": "peaceful"},
        ]
        curve = BGMDirector.plan_episode_curve(scenes)
        ayah_seg = [s for s in curve if "ayah" in s.label][0]
        max_vol = max(s.volume for s in curve)

        # Ayah should be at least 2x quieter than loudest
        assert ayah_seg.volume * 2 < max_vol


# ════════════════════════════════════════════════════════════════
# FFmpeg filter compilation
# ════════════════════════════════════════════════════════════════
class TestFFmpegCompilation:
    def test_empty_curve_returns_default(self):
        s = BGMDirector.to_ffmpeg_volume_filter([])
        assert s.startswith("volume=")

    def test_single_segment_curve(self):
        curve = [VolumeSegment(0.0, 10.0, 0.06)]
        s = BGMDirector.to_ffmpeg_volume_filter(curve)
        assert "0.0600" in s

    def test_multi_segment_curve(self):
        curve = [
            VolumeSegment(0.0, 5.0, 0.06),
            VolumeSegment(5.0, 10.0, 0.02),
            VolumeSegment(10.0, 15.0, 0.06),
        ]
        s = BGMDirector.to_ffmpeg_volume_filter(curve)
        assert "0.0200" in s
        assert "0.0600" in s
        assert "if(lt" in s

    def test_summarize_curve(self):
        curve = [
            VolumeSegment(0.0, 5.0, 0.06, "intro"),
            VolumeSegment(5.0, 10.0, 0.02, "ayah"),
        ]
        summary = BGMDirector.summarize_curve(curve)
        assert "BGM Volume Curve" in summary
        assert "intro" in summary
        assert "ayah" in summary
        assert "Stats" in summary
