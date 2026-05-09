"""
engines/subtitle_engine.py — VALUE / QEEMA v22.5 — ASS subtitle renderer
==========================================================
Generates ASS (Advanced SubStation Alpha) subtitle files from
narration timing data. Embedded into the final video via FFmpeg.

[Why ASS not SRT]
- ASS supports Arabic RTL natively
- Font size, color, shadow all customizable
- Word-level karaoke timing possible
- Better rendering in Arabic

[Usage]
    engine = SubtitleEngine(paths=paths)
    ass_path = engine.generate(script, audio_timing_map, output_dir)
    # Then in FFmpeg: -vf "ass=path/to/subs.ass"

[Timing estimation]
Since we don't have per-word timestamps from ElevenLabs (only per-file),
we use speech rate estimation: Arabic average ~4.5 words/second.
The file-level duration is split proportionally by word count.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.models import EpisodeScript

logger = logging.getLogger(__name__)

# ASS header template
_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayDepth: 0
Timer: 100.0000
Video Aspect Ratio: c1.7778
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Amiri,68,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,3,2,2,40,40,50,1
Style: Ayah,Amiri,82,&H00FFD700,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,3,2,40,40,60,1
Style: Moral,Amiri,64,&H00D4EDDA,&H000000FF,&H00000000,&H90000000,-1,1,0,0,100,100,0,0,1,2,2,2,40,40,50,1
Style: Hook,Amiri,78,&H00FFE566,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,2,2,40,40,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

WORDS_PER_SECOND = 4.2  # Average Arabic speech rate


@dataclass
class SubtitleEntry:
    start_sec: float
    end_sec: float
    text: str
    style: str = "Default"


def _sec_to_ass(seconds: float) -> str:
    """Convert seconds to ASS time format: H:MM:SS.cc"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    cs = int((s % 1) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def _estimate_duration(text: str, actual_duration: Optional[float] = None) -> float:
    """Estimate duration from word count if actual not available."""
    if actual_duration and actual_duration > 0:
        return actual_duration
    words = len(text.split())
    # Add padding for pauses
    return max(words / WORDS_PER_SECOND + 0.5, 1.5)


def _split_long_line(text: str, max_chars: int = 40) -> str:
    """Break long Arabic lines with \\N (ASS hard line break)."""
    if len(text) <= max_chars:
        return text
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > max_chars and current:
            lines.append(current.strip())
            current = word
        else:
            current = (current + " " + word).strip() if current else word
    if current:
        lines.append(current.strip())
    return "\\N".join(lines)


class SubtitleEngine:
    """
    Generates ASS subtitle files from episode scripts + audio timing.

    The timing_map should be:
        {segment_key: duration_in_seconds}
    e.g. {"intro": 8.3, "ayah_10_hook": 4.1, "ayah_10_ayah": 12.5, ...}
    """

    def __init__(self, *, paths) -> None:
        self._paths = paths

    def generate(
        self,
        script: EpisodeScript,
        timing_map: Dict[str, float],
        output_dir: Path,
    ) -> str:
        """
        Generate ASS subtitle file. Returns path to .ass file.
        """
        entries: List[SubtitleEntry] = []
        cursor = 0.0  # current time position in seconds

        # ── Intro
        intro_dur = timing_map.get("intro", _estimate_duration(script.intro_scene.narrator_text))
        if script.intro_scene.narrator_text:
            entries.append(SubtitleEntry(
                start_sec=cursor,
                end_sec=cursor + intro_dur,
                text=_split_long_line(script.intro_scene.narrator_text),
                style="Default",
            ))
        cursor += intro_dur
        cursor += 0.3  # gap between scenes

        # ── Ayah scenes
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"

            # Hook
            if scene.hook_text:
                dur = timing_map.get(f"{sid}_hook", _estimate_duration(scene.hook_text))
                entries.append(SubtitleEntry(
                    start_sec=cursor, end_sec=cursor + dur,
                    text=_split_long_line(scene.hook_text), style="Hook",
                ))
                cursor += dur + 0.2

            # Intro text
            if scene.intro_text:
                dur = timing_map.get(f"{sid}_intro", _estimate_duration(scene.intro_text))
                entries.append(SubtitleEntry(
                    start_sec=cursor, end_sec=cursor + dur,
                    text=_split_long_line(scene.intro_text), style="Default",
                ))
                cursor += dur + 0.2

            # Story
            if scene.story_text:
                dur = timing_map.get(f"{sid}_story", _estimate_duration(scene.story_text))
                # Split story into ~2 subtitle blocks for readability
                story_blocks = self._split_narration_blocks(scene.story_text, dur)
                for block_text, block_start, block_end in story_blocks:
                    entries.append(SubtitleEntry(
                        start_sec=cursor + block_start,
                        end_sec=cursor + block_end,
                        text=_split_long_line(block_text),
                        style="Default",
                    ))
                cursor += dur + 0.3

            # Quran recitation — Ayah text in gold
            ayah_dur = timing_map.get(f"{sid}_ayah", _estimate_duration(scene.ayah.text))
            entries.append(SubtitleEntry(
                start_sec=cursor,
                end_sec=cursor + ayah_dur,
                text=_split_long_line(scene.ayah.text, max_chars=35),
                style="Ayah",
            ))
            cursor += ayah_dur + 0.5

            # Explain
            if scene.explain_text:
                dur = timing_map.get(f"{sid}_explain", _estimate_duration(scene.explain_text))
                entries.append(SubtitleEntry(
                    start_sec=cursor, end_sec=cursor + dur,
                    text=_split_long_line(scene.explain_text), style="Default",
                ))
                cursor += dur + 0.3

            # Moral
            if scene.moral_text:
                dur = timing_map.get(f"{sid}_moral", _estimate_duration(scene.moral_text))
                entries.append(SubtitleEntry(
                    start_sec=cursor, end_sec=cursor + dur,
                    text=_split_long_line(scene.moral_text), style="Moral",
                ))
                cursor += dur + 0.3

        # ── Outro
        outro_dur = timing_map.get("outro", _estimate_duration(script.outro_scene.narrator_text))
        if script.outro_scene.narrator_text:
            entries.append(SubtitleEntry(
                start_sec=cursor, end_sec=cursor + outro_dur,
                text=_split_long_line(script.outro_scene.narrator_text), style="Default",
            ))

        # Build ASS content
        lines = [_ASS_HEADER]
        for e in entries:
            start = _sec_to_ass(e.start_sec)
            end = _sec_to_ass(e.end_sec)
            # ASS dialogue line
            lines.append(
                f"Dialogue: 0,{start},{end},{e.style},,0,0,0,,{{\\an2}}{e.text}"
            )

        ass_content = "\n".join(lines)

        # Write file
        output_dir.mkdir(parents=True, exist_ok=True)
        ep_num = script.episode_number
        ass_path = output_dir / f"episode_{ep_num:03d}_subtitles.ass"
        ass_path.write_text(ass_content, encoding="utf-8-sig")
        logger.info(f"✅ Subtitles: {ass_path} ({len(entries)} entries, ~{cursor:.0f}s)")
        return str(ass_path)

    def build_timing_map_from_audio(
        self,
        audio_map: Dict[str, str],
    ) -> Dict[str, float]:
        """
        Build timing map from actual audio file durations.
        audio_map: {segment_key: file_path}
        """
        from infrastructure.audio_utils import get_audio_duration
        timing: Dict[str, float] = {}
        for key, path in audio_map.items():
            try:
                if path and Path(path).exists():
                    timing[key] = get_audio_duration(path)
            except Exception as e:
                logger.warning(f"⚠️ Could not get duration for {key}: {e}")
        return timing

    @staticmethod
    def _split_narration_blocks(
        text: str,
        total_duration: float,
    ) -> List[Tuple[str, float, float]]:
        """
        Split long narration text into timed blocks for subtitles.
        Returns [(text_block, start_offset, end_offset), ...]
        """
        words = text.split()
        if len(words) <= 12:
            return [(text, 0.0, total_duration)]

        # Split at ~50% word mark
        mid = len(words) // 2
        block1 = " ".join(words[:mid])
        block2 = " ".join(words[mid:])
        mid_time = total_duration * (mid / len(words))

        return [
            (block1, 0.0, mid_time - 0.1),
            (block2, mid_time, total_duration),
        ]
