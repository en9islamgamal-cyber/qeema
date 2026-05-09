"""
core/ffmpeg_args.py — VALUE / QEEMA v22.5 — type-safe FFmpeg argument builder
==========================================================
Why this exists
---------------
The `256kk` bug shipped to production because there is no type discipline
around FFmpeg argv construction. f-strings concatenated config values
that already contained their unit suffix.

This module makes that class of bug structurally impossible:

  - `Bitrate("256k")` validates the format at construction time.
  - `FFmpegArgs(...)` is an immutable, typed builder.
  - Output is a list[str] ready for subprocess.run; no f-strings in callers.

Design properties
-----------------
- Validation lives where the value is created, not at call time.
- Builders are immutable dataclasses; mutation returns a new instance.
- Every public type has a __repr__ that prints round-trippable values.
- Zero runtime dependencies beyond stdlib.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

# ════════════════════════════════════════════════════════════════
# Validated value objects
# ════════════════════════════════════════════════════════════════

# Format: digits + optional unit (k|m|M).
# "256k", "192k", "1M", "320000" — all valid.
# "256kk", "256 k", "abc" — all rejected.
_BITRATE_RE = re.compile(r"^[1-9][0-9]*[kKmM]?$")


@dataclass(frozen=True, slots=True)
class Bitrate:
    """An FFmpeg bitrate spec (audio or video). Validated at construction."""
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(f"Bitrate must be str, got {type(self.value).__name__}")
        if not _BITRATE_RE.match(self.value):
            raise ValueError(
                f"Invalid bitrate {self.value!r}. "
                f"Expected digits + optional k/M suffix (e.g. '256k', '192k', '1M')."
            )

    def __str__(self) -> str:  # for use in arg lists
        return self.value


@dataclass(frozen=True, slots=True)
class Resolution:
    """A WxH resolution. Both must be positive."""
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"Resolution must be positive, got {self.width}x{self.height}"
            )

    def as_ffmpeg(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True, slots=True)
class Framerate:
    """Frames per second. Must be positive integer."""
    fps: int

    def __post_init__(self) -> None:
        if self.fps <= 0 or self.fps > 240:
            raise ValueError(f"fps must be in (0, 240], got {self.fps}")


@dataclass(frozen=True, slots=True)
class CRF:
    """Constant Rate Factor for x264/x265. 0=lossless, 51=worst."""
    value: int

    def __post_init__(self) -> None:
        if not 0 <= self.value <= 51:
            raise ValueError(f"CRF must be 0–51, got {self.value}")


@dataclass(frozen=True, slots=True)
class Duration:
    """A duration in seconds. Always serialized to 3 decimal places."""
    seconds: float

    def __post_init__(self) -> None:
        if self.seconds <= 0:
            raise ValueError(f"Duration must be > 0, got {self.seconds}")

    def __str__(self) -> str:
        return f"{self.seconds:.3f}"


# ════════════════════════════════════════════════════════════════
# Encoder profiles (presets that compose validated values)
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class VideoEncoderSpec:
    """All video-encoder settings as one immutable bundle."""
    codec: str = "libx264"
    preset: str = "medium"
    crf: CRF = field(default_factory=lambda: CRF(18))
    pix_fmt: str = "yuv420p"
    profile: Optional[str] = None  # e.g. "high"

    _ALLOWED_PRESETS = (
        "ultrafast", "superfast", "veryfast", "faster", "fast",
        "medium", "slow", "slower", "veryslow",
    )

    def __post_init__(self) -> None:
        if self.preset not in self._ALLOWED_PRESETS:
            raise ValueError(
                f"Invalid preset {self.preset!r}. "
                f"Allowed: {self._ALLOWED_PRESETS}"
            )

    def to_args(self) -> List[str]:
        args: List[str] = [
            "-c:v", self.codec,
            "-preset", self.preset,
            "-crf", str(self.crf.value),
            "-pix_fmt", self.pix_fmt,
        ]
        if self.profile:
            args += ["-profile:v", self.profile]
        return args


@dataclass(frozen=True, slots=True)
class AudioEncoderSpec:
    """All audio-encoder settings as one immutable bundle."""
    codec: str = "aac"
    bitrate: Bitrate = field(default_factory=lambda: Bitrate("192k"))
    sample_rate_hz: int = 44100

    def __post_init__(self) -> None:
        if self.sample_rate_hz not in (22050, 44100, 48000, 96000):
            raise ValueError(
                f"Unusual sample rate {self.sample_rate_hz}. "
                f"Expected one of 22050/44100/48000/96000."
            )

    def to_args(self) -> List[str]:
        return [
            "-c:a", self.codec,
            "-b:a", str(self.bitrate),    # NEVER an f-string of bitrate.value + "k"
            "-ar", str(self.sample_rate_hz),
        ]


# ════════════════════════════════════════════════════════════════
# Top-level command builders
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class EncodeSegmentArgs:
    """
    Build argv for: webm + audio → mp4 (single encode).

    All public attributes are validated value objects. Construction
    of this dataclass is the only way to produce these arguments,
    and it is checked exhaustively.

    [v17] Added optional video_filter (vf) for inline color grading.
    Bakes color grade into per-scene encode → no separate global
    color_grade stage needed (saves 10+ minutes of re-encode).
    """
    video_input: Path
    audio_input: Path
    output: Path
    resolution: Resolution
    framerate: Framerate
    video: VideoEncoderSpec = field(default_factory=VideoEncoderSpec)
    audio: AudioEncoderSpec = field(default_factory=AudioEncoderSpec)
    max_duration: Optional[Duration] = None
    faststart: bool = True
    overwrite: bool = True
    video_filter: Optional[str] = None  # v17: -vf for inline color grade

    def to_argv(self) -> List[str]:
        argv: List[str] = ["ffmpeg"]
        if self.overwrite:
            argv.append("-y")
        argv += ["-i", str(self.video_input), "-i", str(self.audio_input)]
        if self.max_duration is not None:
            argv += ["-t", str(self.max_duration)]
        # v17: apply video filter (color grade) before scaling/codec
        if self.video_filter:
            # Combine vf with scale to ensure final dimensions
            combined_vf = f"{self.video_filter},scale={self.resolution.as_ffmpeg()}"
            argv += ["-vf", combined_vf]
        argv += self.video.to_args()
        argv += self.audio.to_args()
        if self.faststart:
            argv += ["-movflags", "+faststart"]
        argv += ["-r", str(self.framerate.fps)]
        # If we used -vf for scaling, don't pass -s (avoids double-scale)
        if not self.video_filter:
            argv += ["-s", self.resolution.as_ffmpeg()]
        argv += ["-f", "mp4"]  # ← CRITICAL FIX: explicit format for .partial files
        argv += [str(self.output)]
        return argv

    def to_shell(self) -> str:
        """Pretty-printed shell line, for logging or copy-paste debugging."""
        return " ".join(shlex.quote(a) for a in self.to_argv())


@dataclass(frozen=True, slots=True)
class ConcatStreamCopyArgs:
    """Argv for concat demuxer with stream-copy (no re-encode)."""
    list_file: Path
    output: Path
    faststart: bool = True
    overwrite: bool = True

    def to_argv(self) -> List[str]:
        argv: List[str] = ["ffmpeg"]
        if self.overwrite:
            argv.append("-y")
        argv += [
            "-f", "concat",
            "-safe", "0",
            "-i", str(self.list_file),
            "-c", "copy",
        ]
        if self.faststart:
            argv += ["-movflags", "+faststart"]
        argv += ["-f", "mp4"]  # ← CRITICAL FIX: explicit format for .partial files
        argv += [str(self.output)]
        return argv


@dataclass(frozen=True, slots=True)
class ConcatReencodeArgs:
    """Argv for concat demuxer with full re-encode (slower, always works)."""
    list_file: Path
    output: Path
    video: VideoEncoderSpec = field(default_factory=VideoEncoderSpec)
    audio: AudioEncoderSpec = field(default_factory=AudioEncoderSpec)
    faststart: bool = True
    overwrite: bool = True

    def to_argv(self) -> List[str]:
        argv: List[str] = ["ffmpeg"]
        if self.overwrite:
            argv.append("-y")
        argv += [
            "-f", "concat",
            "-safe", "0",
            "-i", str(self.list_file),
        ]
        argv += self.video.to_args()
        argv += self.audio.to_args()
        if self.faststart:
            argv += ["-movflags", "+faststart"]
        argv += ["-f", "mp4"]  # ← CRITICAL FIX: explicit format for .partial files
        argv += [str(self.output)]
        return argv


# ════════════════════════════════════════════════════════════════
# Concat list file writer (escapes paths properly)
# ════════════════════════════════════════════════════════════════
def write_concat_list(paths: Sequence[Path], destination: Path) -> Path:
    """
    Write a concat-demuxer list file, escaping single quotes in paths.

    FFmpeg concat demuxer requires:
        file 'path with spaces.mp4'
    Single quotes inside paths must be escaped as: '\\''
    """
    if not paths:
        raise ValueError("Cannot write concat list with zero paths")
    lines: List[str] = []
    for p in paths:
        abs_path = str(Path(p).resolve())
        escaped = abs_path.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination
