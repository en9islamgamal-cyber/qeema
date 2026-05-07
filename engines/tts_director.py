"""
engines/tts_director.py — VALUE / QEEMA v22.5 (NEW)
=========================================================================
Per-segment voice direction for ElevenLabs TTS.

[Why this exists]
voice_emotion_mapper (v22.1) provides per-segment voice settings (stability,
style, speed) — a great start. But ElevenLabs supports SSML-like markup:
  - <break time="500ms"/> for natural pauses
  - emphasis hints
  - pronunciation hints

A storyteller doesn't just say words at uniform pace — they pause for
effect, slow for emotional moments, speed up for excitement. This
module uses Gemini in Phase 1 to analyze each segment's text and
prescribe specific delivery directions.

[What it produces]
For each segment (hook, story, explain, moral, etc.), produces a
"directed" version with:
  - <break> tags inserted at natural pause points
  - Per-sentence pace hints (rendered as comments — voice mapper picks up)
  - Pronunciation hints for difficult words

[Cost]
1 Gemini call per episode (one batched call for all segments).
~$0.001 per episode.

[Failure mode]
If Gemini fails, returns the original text unchanged. Voice mapper still
applies emotion-based settings.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SegmentDirection:
    """Voice direction for a single segment."""
    segment_id: str           # e.g., "scene1.hook"
    original_text: str
    directed_text: str        # with <break> tags
    pace: str = "normal"      # slow | normal | fast
    pace_reason: str = ""     # why this pace
    pronunciation_notes: List[str] = field(default_factory=list)

    def to_elevenlabs_input(self) -> str:
        """Return the text in the form to send to ElevenLabs.

        ElevenLabs Multilingual v2 accepts <break> tags inline.
        We strip our internal hint comments before sending.
        """
        # Remove any HTML-style comments we may have added
        text = re.sub(r'<!--.*?-->', '', self.directed_text)
        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text


@dataclass
class EpisodeDirection:
    """Direction notes for an entire episode's audio."""
    segments: Dict[str, SegmentDirection] = field(default_factory=dict)
    fallback_used: bool = False
    notes: List[str] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════
# Prompt for batched direction generation
# ════════════════════════════════════════════════════════════════
DIRECTION_BATCH_PROMPT = """أنت مخرج صوتي خبير. مهمتك: إدارة الأداء الصوتي لراوي قصص دينية للأطفال (٦-١٢ سنة).

[القواعد الذهبية للأداء الصوتي للأطفال]
1. **التوقفات الطبيعية:** بعد الجمل المهمة، توقف للحظة (300-500ms) ليفهم الطفل
2. **بطء عند الفكرة الأساسية:** الجملة اللي فيها العبرة، اتقالها بهدوء
3. **سرعة معقولة في التشويق:** الـ hook والاستفسارات ممكن تكون أسرع شوية
4. **توقفات أطول قبل الـ moral:** لإعطاء وقت للتأمل

[المقاطع المطلوب توجيه أدائها]
{segments_block}

[مهمتك]
لكل مقطع، حدد:
1. **directed_text:** نفس النص لكن مع `<break time="XXms"/>` في أماكن التوقف الطبيعية
   - استخدم 300ms للفواصل العادية
   - استخدم 500ms قبل الفكرة المهمة
   - استخدم 800ms قبل الـ moral
2. **pace:** "slow" | "normal" | "fast" حسب طبيعة المقطع
3. **pace_reason:** سبب اختيارك (جملة قصيرة)
4. **pronunciation_notes:** كلمات صعبة تحتاج توضيح نطق (لو في)

[قواعد صارمة]
- ✋ ما تغيرش الكلمات نفسها — بس ضيف <break> tags
- ✋ كل segment له ID محدد — استخدمه كـ key
- ✋ ارجع JSON فقط، بدون markdown

[الـ JSON المطلوب]
{{
  "directions": {{
    "<segment_id>": {{
      "directed_text": "النص الأصلي مع <break time=\"XXms\"/> tags",
      "pace": "slow|normal|fast",
      "pace_reason": "وصف قصير",
      "pronunciation_notes": []
    }}
  }}
}}

ابدأ بـ {{ مباشرة."""


# ════════════════════════════════════════════════════════════════
# TTSDirector
# ════════════════════════════════════════════════════════════════
class TTSDirector:
    """Generate per-segment voice direction for an episode.

    Args:
        gemini_adapter: GeminiJsonAdapter instance (from llm_adapters).
                       Should be from script_pool_keys (NOT tafsir-dedicated).
    """

    def __init__(self, gemini_adapter: Any) -> None:
        if gemini_adapter is None:
            raise ValueError("TTSDirector requires a Gemini adapter")
        self._adapter = gemini_adapter

    def direct_episode(
        self,
        episode_data: Dict[str, Any],
        *,
        max_retries: int = 2,
    ) -> EpisodeDirection:
        """Direct an entire episode's audio.

        Builds a single batched Gemini call covering all segments.
        Modifies episode_data in-place by adding `tts_direction` field
        to each scene.

        Returns EpisodeDirection summary.
        """
        # Collect all segments to direct
        segments_to_direct: List[tuple] = []  # (segment_id, text, segment_type)

        # Episode-level segments
        for field_name in ("intro_text", "outro_text", "cta_text"):
            text = episode_data.get(field_name, "")
            if text:
                segments_to_direct.append((
                    field_name, text, field_name.replace("_text", ""),
                ))

        # Per-scene segments
        for i, scene in enumerate(episode_data.get("ayah_scenes", []), start=1):
            for kind in ("hook_text", "story_text", "explain_text",
                         "analogy_text", "moral_text"):
                text = scene.get(kind, "")
                if text:
                    segments_to_direct.append((
                        f"scene{i}.{kind}", text, kind.replace("_text", ""),
                    ))

        if not segments_to_direct:
            logger.warning("No segments to direct")
            return EpisodeDirection(notes=["No segments found"])

        logger.info(
            f"🎬 TTSDirector: directing {len(segments_to_direct)} segments"
        )

        # Build the batched prompt
        segments_block = self._format_segments_block(segments_to_direct)
        prompt = DIRECTION_BATCH_PROMPT.format(segments_block=segments_block)

        # Call Gemini with retry
        directions_dict = self._call_with_retry(prompt, max_retries)

        # Build the EpisodeDirection result
        episode_dir = EpisodeDirection()
        if directions_dict is None:
            # Total failure — populate fallback (no breaks, normal pace)
            episode_dir.fallback_used = True
            episode_dir.notes.append("TTSDirector Gemini call failed — using originals")
            for seg_id, text, _ in segments_to_direct:
                episode_dir.segments[seg_id] = SegmentDirection(
                    segment_id=seg_id,
                    original_text=text,
                    directed_text=text,
                    pace="normal",
                    pace_reason="fallback (no direction)",
                )
        else:
            # Parse Gemini's response, fill in any missing segments with fallback
            for seg_id, text, _ in segments_to_direct:
                d = directions_dict.get(seg_id, {})
                directed_text = d.get("directed_text", text) if d else text
                # Sanity check — directed_text must contain the original words
                # (we strip break tags and compare lengths roughly)
                if not self._direction_preserves_text(text, directed_text):
                    logger.warning(
                        f"⚠️ {seg_id}: directed_text seems modified beyond breaks "
                        f"— using original"
                    )
                    directed_text = text

                episode_dir.segments[seg_id] = SegmentDirection(
                    segment_id=seg_id,
                    original_text=text,
                    directed_text=directed_text,
                    pace=d.get("pace", "normal") if d else "normal",
                    pace_reason=d.get("pace_reason", "") if d else "",
                    pronunciation_notes=(
                        d.get("pronunciation_notes", []) if d else []
                    ),
                )

        # Apply directions back to episode_data
        self._apply_to_episode(episode_data, episode_dir)
        return episode_dir

    # ─── Internal helpers ─────────────────────────────────────
    @staticmethod
    def _format_segments_block(
        segments: List[tuple],
    ) -> str:
        """Format segments for the batched prompt."""
        lines = []
        for seg_id, text, seg_type in segments:
            lines.append(f"[{seg_id}] (نوع: {seg_type})")
            lines.append(f"  {text}")
            lines.append("")
        return "\n".join(lines)

    def _call_with_retry(
        self, prompt: str, max_retries: int,
    ) -> Optional[Dict[str, Dict]]:
        """Call Gemini and parse the response. Returns None on failure."""
        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                response = self._adapter.generate_json(
                    prompt=prompt,
                    temperature=0.4,
                    max_tokens=3000,
                )
                if isinstance(response, dict):
                    parsed = response
                elif isinstance(response, str):
                    parsed = json.loads(response)
                else:
                    raise ValueError(
                        f"Unexpected response type: {type(response).__name__}"
                    )
                directions = parsed.get("directions", {})
                if not isinstance(directions, dict):
                    raise ValueError("'directions' field is not a dict")
                return directions
            except json.JSONDecodeError as e:
                last_err = e
                logger.warning(
                    f"⚠️ TTSDirector attempt {attempt}/{max_retries}: "
                    f"JSON error — {e}"
                )
            except Exception as e:
                last_err = e
                logger.warning(
                    f"⚠️ TTSDirector attempt {attempt}/{max_retries}: {e}"
                )
        logger.error(
            f"❌ TTSDirector failed after {max_retries} attempts: {last_err}"
        )
        return None

    @staticmethod
    def _direction_preserves_text(original: str, directed: str) -> bool:
        """Check that directed_text only adds <break> tags, not new content.

        We strip <break> tags from directed_text and check it's similar in
        length to the original (allowing small whitespace variation).
        """
        if not directed:
            return False
        stripped = re.sub(r'<break\s+time="[^"]*"\s*/>', '', directed)
        stripped = re.sub(r'\s+', ' ', stripped).strip()
        original_clean = re.sub(r'\s+', ' ', original).strip()

        # Length should be very close (within 10%)
        if len(original_clean) == 0:
            return len(stripped) == 0
        ratio = len(stripped) / len(original_clean)
        return 0.85 <= ratio <= 1.15

    @staticmethod
    def _apply_to_episode(
        episode_data: Dict[str, Any], direction: EpisodeDirection,
    ) -> None:
        """Apply directions back to the episode_data dict.

        Adds:
          - For top-level fields: replaces the field with the directed text
          - For scene fields: adds `<field>_directed` and `<field>_pace`
        """
        # Top-level fields
        for field_name in ("intro_text", "outro_text", "cta_text"):
            if field_name in direction.segments:
                seg = direction.segments[field_name]
                episode_data[f"{field_name}_directed"] = seg.directed_text
                episode_data[f"{field_name}_pace"] = seg.pace

        # Scene fields
        for i, scene in enumerate(
            episode_data.get("ayah_scenes", []), start=1,
        ):
            for kind in (
                "hook_text", "story_text", "explain_text",
                "analogy_text", "moral_text",
            ):
                seg_id = f"scene{i}.{kind}"
                if seg_id in direction.segments:
                    seg = direction.segments[seg_id]
                    scene[f"{kind}_directed"] = seg.directed_text
                    scene[f"{kind}_pace"] = seg.pace
