"""
video/audio_director.py
====================================================================
Audio mixing for the final episode.

Timeline (per spec):
   [0:00] Hook narration       (from ElevenLabs)
          silence ~600ms
   [...]  Intro narration      (from ElevenLabs)
          silence ~800ms
   [...]  Opening tilawah      (Husary, concatenated MP3s)
          silence ~800ms
   [...]  Full explanation     (all ayah narrations merged)
          silence ~500ms
   [...]  Transition narration (from ElevenLabs)
          silence ~600ms
   [...]  Closing tilawah      (SAME file as opening — reused)
          silence ~500ms
   [...]  Outro narration      (from ElevenLabs)

Fade-out 200ms at end of each speech segment to avoid abrupt cuts.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

from core.config import TEMP_DIR, get_pipeline_config


log = logging.getLogger(__name__)


class AudioMixError(Exception):
    pass


@dataclass
class AudioSegment:
    """One named audio segment in the timeline."""
    label: str          # e.g. "hook", "intro", "tilawah_1"
    path: Path          # MP3 file
    is_speech: bool     # if True, apply fade-out
    silence_after_ms: int  # silence to append


@dataclass
class MixedAudio:
    """Result of mixing the episode audio."""
    output_path: Path
    timeline_seconds: List[float]   # cumulative time at start of each segment
    total_duration_sec: float


def build_episode_audio(
    *,
    hook_audio: Path,
    intro_audio: Path,
    explanation_audio: Path,
    transition_audio: Path,
    outro_audio: Path,
    tilawah_full: Path,
    output: Path,
) -> MixedAudio:
    """
    Build the final episode audio file with all transitions.

    Returns: MixedAudio with cumulative timeline + total duration.
    """
    cfg = get_pipeline_config()

    segments = [
        AudioSegment("hook",            hook_audio,        True,
                     cfg.silence_medium_ms),
        AudioSegment("intro",           intro_audio,       True,
                     cfg.silence_medium_ms),
        AudioSegment("tilawah_1",       tilawah_full,      False,
                     cfg.silence_medium_ms),
        AudioSegment("explanation",     explanation_audio, True,
                     cfg.silence_short_ms),
        AudioSegment("transition",      transition_audio,  True,
                     cfg.silence_medium_ms),
        AudioSegment("tilawah_2",       tilawah_full,      False,
                     cfg.silence_short_ms),
        AudioSegment("outro",           outro_audio,       True,
                     0),
    ]

    return _mix_segments(segments, output, cfg)


def _mix_segments(
    segments: List[AudioSegment],
    output: Path,
    cfg,
) -> MixedAudio:
    """
    Use ffmpeg to concatenate segments with silence padding + fades.

    Strategy:
      1. For each speech segment, generate a temp WAV with fade-out
      2. For each silence gap, generate a silent WAV of right length
      3. Concat everything with the concat demuxer
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = TEMP_DIR / "audio_mix"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Build the list of intermediate files
    intermediate_paths: List[Path] = []
    timeline_seconds: List[float] = []
    cumulative_sec = 0.0
    fade_sec = 0.2  # 200ms fade out

    for i, seg in enumerate(segments):
        # Add the segment itself
        if seg.is_speech:
            # Apply fade-out
            faded = work_dir / f"{i:02d}_{seg.label}_faded.mp3"
            _apply_fade_out(seg.path, faded, fade_sec)
            intermediate_paths.append(faded)
        else:
            intermediate_paths.append(seg.path)

        seg_duration = _get_audio_duration_sec(intermediate_paths[-1])
        timeline_seconds.append(cumulative_sec)
        cumulative_sec += seg_duration

        # Add silence after, except for the last segment
        if seg.silence_after_ms > 0:
            sil_path = work_dir / f"{i:02d}_{seg.label}_silence.mp3"
            _make_silence(sil_path, seg.silence_after_ms / 1000.0)
            intermediate_paths.append(sil_path)
            cumulative_sec += seg.silence_after_ms / 1000.0

    # Concat all
    _concat_with_demuxer(intermediate_paths, output)
    total_duration = _get_audio_duration_sec(output)

    log.info(
        "✓ Episode audio mixed: %.1fs total, %d segments",
        total_duration, len(segments),
    )

    return MixedAudio(
        output_path=output,
        timeline_seconds=timeline_seconds,
        total_duration_sec=total_duration,
    )


# ════════════════════════════════════════════════════════════════════
# FFmpeg primitives
# ════════════════════════════════════════════════════════════════════

def _apply_fade_out(src: Path, dst: Path, fade_sec: float) -> None:
    """Re-encode `src` with a fade-out applied at the end."""
    duration = _get_audio_duration_sec(src)
    start_fade = max(0.0, duration - fade_sec)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(src),
        "-af", f"afade=t=out:st={start_fade:.3f}:d={fade_sec:.3f}",
        "-c:a", "libmp3lame", "-q:a", "2",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioMixError(f"Fade-out failed: {r.stderr[:300]}")


def _make_silence(dst: Path, duration_sec: float) -> None:
    """Generate a silent MP3 of given duration."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "lavfi", "-i",
        f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", f"{duration_sec:.3f}",
        "-c:a", "libmp3lame", "-q:a", "2",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioMixError(f"Silence gen failed: {r.stderr[:300]}")


def _concat_with_demuxer(files: List[Path], dst: Path) -> None:
    """Concat MP3s losslessly via the concat demuxer."""
    list_file = dst.parent / f"{dst.stem}_list.txt"
    with list_file.open("w") as f:
        for p in files:
            f.write(f"file '{p.resolve()}'\n")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if r.returncode != 0:
        raise AudioMixError(f"Concat failed: {r.stderr[:300]}")


def _get_audio_duration_sec(path: Path) -> float:
    """Use ffprobe to get the duration of an audio file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioMixError(f"ffprobe failed: {r.stderr[:300]}")
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise AudioMixError(f"Bad ffprobe output: {r.stdout!r}")
