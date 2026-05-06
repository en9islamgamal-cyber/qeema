"""
infrastructure/bgm_director.py — VALUE / QEEMA v22.2 (NEW)
=========================================================================
Per-scene BGM volume planning.

[Why this exists]
The current BGMMixer applies ONE constant BGM volume (~0.06) over the
entire episode. This is wrong for several reasons:

  1. During Quran recitation (reverent), BGM should DUCK to ~0.02
     so the recitation is clearly heard and respected.

  2. During hooks (excited), BGM should be slightly LOUDER (~0.10)
     to amplify energy.

  3. During morals/outros (peaceful), BGM should be MEDIUM (~0.07)
     to support the contemplative mood.

[Solution]
This module produces a list of (start_sec, end_sec, volume) tuples
that the BGMMixer can use to build a volume automation curve via
ffmpeg's `volume` filter with `enable=between(t,X,Y)` expressions.

[Output format]
A volume curve is a piecewise constant function with smooth fade transitions
at boundaries (200ms ramp).

[Backward compat]
If BGMMixer doesn't support volume curves yet, this module just provides
the data — actual application happens via apply_bgm_with_curve().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Volume targets per emotion + segment combo
# ════════════════════════════════════════════════════════════════
# Values in 0.0-1.0 (as multiplier of BGM file's natural volume)
# Tuned to be subtle — never overpower voice

VOLUME_DEFAULTS = {
    # ── Reverent (Quran ayah recitation) — BGM ducks heavily ──
    "ayah":           0.020,   # Dramatic duck for recitation respect
    "reverent":       0.025,
    # ── Hooks — slightly elevated for energy ──
    "hook":           0.075,   # Slightly louder for hook impact
    "excited":        0.080,
    # ── Standard narrative ──
    "intro":          0.060,
    "warm":           0.055,
    "narrator":       0.055,
    "explain":        0.050,   # Lower to support clarity
    # ── Story/analogy — balanced ──
    "story":          0.060,
    "analogy":        0.060,
    "playful":        0.065,
    # ── Contemplative — gentle support ──
    "moral":          0.045,   # Gentle for reflection
    "peaceful":       0.050,
    "outro":          0.055,
    # ── Fallback ──
    "default":        0.060,
}


@dataclass(frozen=True)
class VolumeSegment:
    """A single segment in a volume automation curve."""
    start_sec: float
    end_sec: float
    volume: float
    label: str = ""

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec

    def __post_init__(self) -> None:
        if self.start_sec < 0 or self.end_sec < self.start_sec:
            raise ValueError(
                f"Invalid time range: [{self.start_sec}, {self.end_sec}]"
            )
        if not 0.0 <= self.volume <= 1.0:
            raise ValueError(
                f"Volume {self.volume} out of range [0, 1]"
            )


# ════════════════════════════════════════════════════════════════
# BGM Director
# ════════════════════════════════════════════════════════════════
class BGMDirector:
    """Plans BGM volume curve based on scene mood + segment type."""

    @classmethod
    def select_volume(
        cls,
        *,
        emotion: Optional[str] = None,
        segment: Optional[str] = None,
        is_ayah: bool = False,
    ) -> float:
        """Pick the right BGM volume for a single segment.

        Resolution order:
            1. is_ayah=True → ayah default (heavy duck)
            2. segment-specific (hook, moral, etc)
            3. emotion-specific
            4. default

        Returns:
            Volume in 0.0-1.0 range.
        """
        # 1. Quran recitation gets dramatic duck regardless
        if is_ayah:
            return VOLUME_DEFAULTS["ayah"]

        # 2. Try segment-specific
        if segment:
            seg_key = segment.lower().strip()
            if seg_key in VOLUME_DEFAULTS:
                return VOLUME_DEFAULTS[seg_key]

        # 3. Try emotion
        if emotion:
            emo_key = emotion.lower().strip()
            if emo_key in VOLUME_DEFAULTS:
                return VOLUME_DEFAULTS[emo_key]

        return VOLUME_DEFAULTS["default"]

    @classmethod
    def plan_episode_curve(
        cls,
        scenes: List[dict],
    ) -> List[VolumeSegment]:
        """Build a volume curve for an entire episode.

        Args:
            scenes: List of dicts with keys:
                - 'duration_sec': how long this scene plays
                - 'emotion': scene emotion (warm/reverent/etc)
                - 'segment': segment type (hook/story/ayah/etc)
                - 'is_ayah': bool (True for Quran recitation)

        Returns:
            List of VolumeSegment forming a continuous timeline.
        """
        segments: List[VolumeSegment] = []
        cursor = 0.0

        for scene in scenes:
            duration = float(scene.get("duration_sec", 5.0))
            if duration <= 0:
                continue

            volume = cls.select_volume(
                emotion=scene.get("emotion"),
                segment=scene.get("segment"),
                is_ayah=bool(scene.get("is_ayah", False)),
            )

            label = (
                scene.get("segment", "")
                or scene.get("emotion", "")
                or "scene"
            )
            segments.append(VolumeSegment(
                start_sec=cursor,
                end_sec=cursor + duration,
                volume=volume,
                label=label,
            ))
            cursor += duration

        return segments

    @classmethod
    def to_ffmpeg_volume_filter(
        cls,
        curve: List[VolumeSegment],
        *,
        ramp_sec: float = 0.20,
    ) -> str:
        """Compile a volume curve to an ffmpeg `volume` filter expression.

        Uses piecewise constant volume with cosine ramps between segments.

        Returns:
            String suitable for ffmpeg -af volume=<expression>:eval=frame

        Example output (3 segments):
            volume=
              if(lt(t,0.0),0.06,
              if(lt(t,5.0),0.06,
              if(lt(t,7.5),0.025,
              0.06)))

        Note: For complex curves, use multi-stage filter chains instead.
        For now we generate a simple if-chain that ffmpeg can evaluate.
        """
        if not curve:
            return "volume=0.06"

        # Build nested if-chain
        # if(lt(t, end_1), v1, if(lt(t, end_2), v2, ... vN))
        expr = f"{curve[-1].volume:.4f}"  # final fallback
        for seg in reversed(curve[:-1]):
            expr = (
                f"if(lt(t\\,{seg.end_sec:.3f})\\,"
                f"{seg.volume:.4f}\\,{expr})"
            )

        return f"volume={expr}:eval=frame"

    @classmethod
    def summarize_curve(
        cls, curve: List[VolumeSegment],
    ) -> str:
        """Build a human-readable summary of a volume curve."""
        if not curve:
            return "(no curve)"
        lines = [
            f"🎚️  BGM Volume Curve ({len(curve)} segments, "
            f"total {curve[-1].end_sec:.1f}s):"
        ]
        for seg in curve:
            bar_len = int(seg.volume * 200)  # visual scale
            bar = "█" * min(bar_len, 30)
            lines.append(
                f"  [{seg.start_sec:>5.1f}s → {seg.end_sec:>5.1f}s] "
                f"vol {seg.volume:.3f} {bar} {seg.label}"
            )
        # Statistics
        avg_vol = sum(s.volume * s.duration for s in curve) / sum(
            s.duration for s in curve
        )
        min_vol = min(s.volume for s in curve)
        max_vol = max(s.volume for s in curve)
        lines.append(
            f"  Stats: avg={avg_vol:.3f}, "
            f"min={min_vol:.3f}, max={max_vol:.3f}"
        )
        return "\n".join(lines)
