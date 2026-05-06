"""
infrastructure/mood_transitions.py — VALUE / QEEMA v23 (NEW)
=========================================================================
Emotion-driven scene transition selection.

[Why this exists]
v22 used a single transition type (fade, 0.4s) for ALL scene changes.
This is wrong: a transition between an excited hook and a peaceful moral
should be SLOWER + softer. A transition between two peaceful scenes can
be longer. A transition into an excited hook should be a quick cut.

Cinematic editing principle: transition style carries meaning.

[Mapping logic]
For each transition between scene_A → scene_B, the LATER scene's emotion
dominates the choice (you're settling INTO emotion B):

  → excited:   quick cut (0.15s)        — sudden energy shift
  → playful:   short crossfade (0.30s)  — light handoff
  → warm:      standard fade (0.45s)    — comfortable arrival
  → reverent:  long fade (0.80s)        — sacred, deliberate
  → peaceful:  long crossfade (0.70s)   — settling in

Special cases:
  - Any → reverent (Quran ayah):    extra-slow fade, reverent
  - excited → peaceful (big shift): longer crossfade for breathing room
  - Same emotion adjacent:          medium duration

[Integration]
Wraps concat_with_crossfades but picks transitions per-pair.
Falls back to single duration if emotions unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransitionConfig:
    """A single transition's characteristics."""
    duration_sec: float
    type: str  # "fade", "crossfade", "cut"

    def __post_init__(self):
        if not 0.0 <= self.duration_sec <= 5.0:
            raise ValueError(
                f"transition duration {self.duration_sec} must be 0-5s"
            )
        if self.type not in ("fade", "crossfade", "cut"):
            raise ValueError(
                f"transition type {self.type!r} unknown"
            )


# ════════════════════════════════════════════════════════════════
# Per-emotion arrival presets — picked based on the LATER scene
# ════════════════════════════════════════════════════════════════
ARRIVAL_PRESETS = {
    "excited":  TransitionConfig(duration_sec=0.15, type="cut"),
    "playful":  TransitionConfig(duration_sec=0.30, type="crossfade"),
    "warm":     TransitionConfig(duration_sec=0.45, type="fade"),
    "reverent": TransitionConfig(duration_sec=0.80, type="fade"),
    "peaceful": TransitionConfig(duration_sec=0.70, type="crossfade"),
}

# Default fallback when emotion unknown
DEFAULT_TRANSITION = TransitionConfig(duration_sec=0.40, type="fade")


# ════════════════════════════════════════════════════════════════
# Special-case adjustments
# ════════════════════════════════════════════════════════════════
def _adjust_for_pair(
    from_emotion: str,
    to_emotion: str,
    base: TransitionConfig,
) -> TransitionConfig:
    """Adjust transition based on emotion delta.

    Examples:
      excited → peaceful:  big shift, needs breathing room (+0.3s)
      reverent → reverent: very deliberate (+0.2s)
      excited → excited:   keep momentum (-0.05s)
    """
    f = from_emotion.lower().strip()
    t = to_emotion.lower().strip()

    # Big calm-down: excited/playful → peaceful/reverent
    if f in ("excited", "playful") and t in ("peaceful", "reverent"):
        return TransitionConfig(
            duration_sec=min(1.5, base.duration_sec + 0.3),
            type="crossfade",  # always soft
        )

    # Big wake-up: peaceful/reverent → excited/playful
    if f in ("peaceful", "reverent") and t in ("excited", "playful"):
        # Even faster cut for jolt effect
        return TransitionConfig(
            duration_sec=max(0.10, base.duration_sec - 0.05),
            type="cut",
        )

    # Same emotion adjacent — keep momentum
    if f == t and f != "":
        # Slightly faster (you're already there)
        return TransitionConfig(
            duration_sec=max(0.10, base.duration_sec * 0.85),
            type=base.type,
        )

    return base


# ════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════
def transition_for_pair(
    from_emotion: Optional[str],
    to_emotion: Optional[str],
) -> TransitionConfig:
    """Get the right transition for a single scene pair.

    Args:
        from_emotion: Emotion of the outgoing scene (e.g., "excited").
        to_emotion: Emotion of the incoming scene (dominant).

    Returns:
        TransitionConfig with duration + type.

    If either emotion is None/empty, returns DEFAULT_TRANSITION.
    """
    if not to_emotion:
        return DEFAULT_TRANSITION

    base = ARRIVAL_PRESETS.get(
        to_emotion.lower().strip(),
        DEFAULT_TRANSITION,
    )

    if from_emotion:
        return _adjust_for_pair(from_emotion, to_emotion, base)

    return base


def transitions_for_sequence(
    emotions: List[Optional[str]],
) -> List[TransitionConfig]:
    """Get transitions for an entire scene sequence.

    For N scenes, returns N-1 transitions (between consecutive pairs).

    Args:
        emotions: List of N emotion strings (one per scene).

    Returns:
        List of N-1 TransitionConfigs.
    """
    if len(emotions) < 2:
        return []

    transitions = []
    for i in range(len(emotions) - 1):
        t = transition_for_pair(emotions[i], emotions[i + 1])
        transitions.append(t)
    return transitions


def average_duration(transitions: List[TransitionConfig]) -> float:
    """Compute mean transition duration (for logging/reporting)."""
    if not transitions:
        return 0.0
    return sum(t.duration_sec for t in transitions) / len(transitions)


# ════════════════════════════════════════════════════════════════
# Concat wrapper — used by orchestrator
# ════════════════════════════════════════════════════════════════
def concat_with_mood_transitions(
    bgm_mixer,                            # BGMMixer instance
    segments: List[str],
    emotions: List[Optional[str]],
    output_path: str,
    *,
    assembler=None,
) -> str:
    """Concat scenes using per-pair mood-aware transitions.

    Falls back to bgm_mixer.concat_with_crossfades() if:
      - Emotions list length doesn't match segments
      - All emotions are None
      - The mixer doesn't support per-pair durations (older API)

    The current bgm_mixer applies ONE duration to all transitions, so we
    compute the AVERAGE per-pair duration as a compromise. A future
    bgm_mixer with per-pair support would use the full transitions list.
    """
    if len(emotions) != len(segments):
        logger.debug(
            f"⚠️ mood transitions: emotions={len(emotions)} != "
            f"segments={len(segments)}, using default"
        )
        return bgm_mixer.concat_with_crossfades(
            segments, output_path,
            transition_duration=0.4,
            transition_type="fade",
            assembler=assembler,
        )

    transitions = transitions_for_sequence(emotions)

    if not transitions:
        return bgm_mixer.concat_with_crossfades(
            segments, output_path,
            transition_duration=0.4,
            transition_type="fade",
            assembler=assembler,
        )

    # Compute average duration; pick the most common type
    avg = average_duration(transitions)
    types_count = {}
    for t in transitions:
        types_count[t.type] = types_count.get(t.type, 0) + 1
    dominant_type = max(types_count, key=types_count.get)

    # If "cut" dominates, use very short fade (mixer might not support 0)
    if dominant_type == "cut" and avg < 0.2:
        avg = 0.15
        dominant_type = "fade"

    logger.info(
        f"🎬 Mood transitions: {len(transitions)} transitions, "
        f"avg={avg:.2f}s, type={dominant_type} "
        f"(emotions: {[e for e in emotions if e][:5]}...)"
    )

    return bgm_mixer.concat_with_crossfades(
        segments, output_path,
        transition_duration=avg,
        transition_type=dominant_type,
        assembler=assembler,
    )
