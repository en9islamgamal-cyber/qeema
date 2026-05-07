"""
tests/test_ffmpeg_args.py
==========================
Tests that prove the `256kk` class of bugs cannot recur.

Run with:
    pytest tests/test_ffmpeg_args.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.ffmpeg_args import (
    AudioEncoderSpec,
    Bitrate,
    ConcatReencodeArgs,
    ConcatStreamCopyArgs,
    CRF,
    Duration,
    EncodeSegmentArgs,
    Framerate,
    Resolution,
    VideoEncoderSpec,
    write_concat_list,
)


# ════════════════════════════════════════════════════════════════
# Bitrate validation — the bug that started this whole audit
# ════════════════════════════════════════════════════════════════
class TestBitrate:
    @pytest.mark.parametrize("value", ["1k", "192k", "256k", "320k", "1M", "2M", "320000"])
    def test_accepts_valid(self, value: str) -> None:
        assert str(Bitrate(value)) == value

    @pytest.mark.parametrize(
        "bad",
        [
            "256kk",     # the original bug
            "256 k",     # space
            "abc",
            "",
            "k256",
            "256kbit",
            "0",         # zero is not valid
            "-1k",
            "1.5k",      # decimals not allowed
        ],
    )
    def test_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            Bitrate(bad)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            Bitrate(256)  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        b = Bitrate("192k")
        with pytest.raises(Exception):
            b.value = "320k"  # type: ignore[misc]


class TestResolution:
    def test_valid(self) -> None:
        r = Resolution(1920, 1080)
        assert r.as_ffmpeg() == "1920x1080"

    @pytest.mark.parametrize("w,h", [(0, 1080), (1920, 0), (-1, 1080)])
    def test_invalid(self, w: int, h: int) -> None:
        with pytest.raises(ValueError):
            Resolution(w, h)


class TestFramerate:
    @pytest.mark.parametrize("fps", [24, 30, 60, 120, 240])
    def test_valid(self, fps: int) -> None:
        Framerate(fps)

    @pytest.mark.parametrize("fps", [0, -1, 241, 1000])
    def test_invalid(self, fps: int) -> None:
        with pytest.raises(ValueError):
            Framerate(fps)


class TestCRF:
    @pytest.mark.parametrize("v", [0, 1, 18, 23, 28, 51])
    def test_valid(self, v: int) -> None:
        CRF(v)

    @pytest.mark.parametrize("v", [-1, 52, 100])
    def test_invalid(self, v: int) -> None:
        with pytest.raises(ValueError):
            CRF(v)


class TestDuration:
    def test_serialization(self) -> None:
        assert str(Duration(5.5)) == "5.500"
        assert str(Duration(1.234567)) == "1.235"

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            Duration(0)
        with pytest.raises(ValueError):
            Duration(-1)


# ════════════════════════════════════════════════════════════════
# VideoEncoderSpec
# ════════════════════════════════════════════════════════════════
class TestVideoEncoderSpec:
    def test_default_args(self) -> None:
        spec = VideoEncoderSpec()
        args = spec.to_args()
        assert "-c:v" in args
        assert "libx264" in args
        assert "-preset" in args
        assert "medium" in args
        assert "-crf" in args
        assert "18" in args
        assert "-pix_fmt" in args
        assert "yuv420p" in args

    def test_invalid_preset(self) -> None:
        with pytest.raises(ValueError):
            VideoEncoderSpec(preset="not-a-preset")

    def test_with_profile(self) -> None:
        spec = VideoEncoderSpec(profile="high")
        args = spec.to_args()
        assert "-profile:v" in args
        assert "high" in args


# ════════════════════════════════════════════════════════════════
# AudioEncoderSpec — the bug-prone path
# ════════════════════════════════════════════════════════════════
class TestAudioEncoderSpec:
    def test_default_no_double_k(self) -> None:
        """The exact regression test for the 256kk bug."""
        spec = AudioEncoderSpec()
        args = spec.to_args()
        # Find the bitrate flag and its value
        idx = args.index("-b:a")
        bitrate_value = args[idx + 1]
        # Critical: must end with a single 'k', never 'kk'
        assert bitrate_value == "192k"
        assert not bitrate_value.endswith("kk")

    def test_custom_bitrate(self) -> None:
        spec = AudioEncoderSpec(bitrate=Bitrate("256k"))
        args = spec.to_args()
        idx = args.index("-b:a")
        assert args[idx + 1] == "256k"
        assert not args[idx + 1].endswith("kk")

    def test_invalid_sample_rate(self) -> None:
        with pytest.raises(ValueError):
            AudioEncoderSpec(sample_rate_hz=12345)

    def test_argv_immutable(self) -> None:
        """Calling to_args() twice yields identical output."""
        spec = AudioEncoderSpec()
        assert spec.to_args() == spec.to_args()


# ════════════════════════════════════════════════════════════════
# EncodeSegmentArgs — the full builder
# ════════════════════════════════════════════════════════════════
class TestEncodeSegmentArgs:
    def test_full_argv(self, tmp_path: Path) -> None:
        args = EncodeSegmentArgs(
            video_input=tmp_path / "in.webm",
            audio_input=tmp_path / "in.mp3",
            output=tmp_path / "out.mp4",
            resolution=Resolution(1920, 1080),
            framerate=Framerate(30),
            video=VideoEncoderSpec(crf=CRF(23)),
            audio=AudioEncoderSpec(bitrate=Bitrate("192k")),
        )
        argv = args.to_argv()
        assert argv[0] == "ffmpeg"
        assert "-y" in argv      # overwrite default
        assert "-i" in argv
        assert str(tmp_path / "in.webm") in argv
        assert str(tmp_path / "in.mp3") in argv
        # No 256kk-style bug:
        idx = argv.index("-b:a")
        assert not argv[idx + 1].endswith("kk")

    def test_with_max_duration(self, tmp_path: Path) -> None:
        args = EncodeSegmentArgs(
            video_input=tmp_path / "in.webm",
            audio_input=tmp_path / "in.mp3",
            output=tmp_path / "out.mp4",
            resolution=Resolution(1920, 1080),
            framerate=Framerate(30),
            max_duration=Duration(5.5),
        )
        argv = args.to_argv()
        idx = argv.index("-t")
        assert argv[idx + 1] == "5.500"

    def test_no_overwrite(self, tmp_path: Path) -> None:
        args = EncodeSegmentArgs(
            video_input=tmp_path / "in.webm",
            audio_input=tmp_path / "in.mp3",
            output=tmp_path / "out.mp4",
            resolution=Resolution(1920, 1080),
            framerate=Framerate(30),
            overwrite=False,
        )
        assert "-y" not in args.to_argv()

    def test_to_shell(self, tmp_path: Path) -> None:
        args = EncodeSegmentArgs(
            video_input=tmp_path / "in.webm",
            audio_input=tmp_path / "in.mp3",
            output=tmp_path / "out.mp4",
            resolution=Resolution(1920, 1080),
            framerate=Framerate(30),
        )
        shell = args.to_shell()
        assert shell.startswith("ffmpeg")
        assert "-y" in shell


# ════════════════════════════════════════════════════════════════
# write_concat_list
# ════════════════════════════════════════════════════════════════
class TestWriteConcatList:
    def test_basic(self, tmp_path: Path) -> None:
        # Create some real files so resolve() works
        a = tmp_path / "a.mp4"
        b = tmp_path / "b.mp4"
        a.touch()
        b.touch()

        list_file = tmp_path / "list.txt"
        write_concat_list([a, b], list_file)

        contents = list_file.read_text()
        assert "file '" in contents
        assert str(a.resolve()) in contents
        assert str(b.resolve()) in contents

    def test_empty_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            write_concat_list([], tmp_path / "list.txt")

    def test_escapes_single_quotes(self, tmp_path: Path) -> None:
        weird = tmp_path / "it's a file.mp4"
        weird.touch()
        list_file = tmp_path / "list.txt"
        write_concat_list([weird], list_file)
        contents = list_file.read_text()
        # The single quote in the filename must be escaped as: '\''
        assert "'\\''" in contents


# ════════════════════════════════════════════════════════════════
# Concat builders
# ════════════════════════════════════════════════════════════════
class TestConcatStreamCopyArgs:
    def test_basic(self, tmp_path: Path) -> None:
        args = ConcatStreamCopyArgs(
            list_file=tmp_path / "list.txt",
            output=tmp_path / "out.mp4",
        )
        argv = args.to_argv()
        assert "-c" in argv
        assert "copy" in argv
        assert "-f" in argv
        assert "concat" in argv
        assert "-safe" in argv
        assert "0" in argv


class TestConcatReencodeArgs:
    def test_no_double_k(self, tmp_path: Path) -> None:
        """Same regression check as encode — no 256kk in concat path either."""
        args = ConcatReencodeArgs(
            list_file=tmp_path / "list.txt",
            output=tmp_path / "out.mp4",
        )
        argv = args.to_argv()
        idx = argv.index("-b:a")
        assert not argv[idx + 1].endswith("kk")
        assert argv[idx + 1] == "192k"
