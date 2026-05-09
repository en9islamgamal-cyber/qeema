"""
tests/test_visual_prompt_clip_budget.py — VALUE / QEEMA v22.5

Guards Leonardo CLIP's 77-token limit. If anyone makes the LOCKED_STYLE
or LOCKED_NEGATIVE longer in the future, these tests fail loudly.

[Why this matters]
Leonardo's CLIP encoder truncates everything past 77 tokens. If our prompt
is too long, the END of the prompt gets silently dropped — and the END is
where we put the safety constraints (no faces, no text, NotebookLM-style).

A truncated safety constraint = pipeline could generate human faces in
religious children's content. This is a CRITICAL regression risk.

[How tokens are estimated]
We use word_count × 1.3 as a conservative approximation of CLIP BPE tokens.
The actual CLIP tokenizer would give a slightly different number depending
on the specific words (compound words split into subwords). 1.3× is a safe
overestimate — if our test passes at this ratio, we're definitely under
the real limit.
"""
from __future__ import annotations

import pytest

from engines.visual_prompt_engineer import VisualPromptEngineer


CLIP_TOKEN_LIMIT = 77
WORD_TO_TOKEN_RATIO = 1.3  # Conservative overestimate


def estimate_tokens(text: str) -> int:
    """Conservative CLIP token estimate."""
    return int(len(text.split()) * WORD_TO_TOKEN_RATIO)


# ════════════════════════════════════════════════════════════════
# CLIP token budget enforcement
# ════════════════════════════════════════════════════════════════
class TestCLIPTokenBudget:
    def test_locked_style_fits_with_room_for_dynamic_parts(self):
        """LOCKED_STYLE alone must leave headroom for subject + emotion + composition.
        Reserve ~30 tokens for dynamic parts → LOCKED_STYLE ≤ 47 tokens.
        """
        locked = VisualPromptEngineer.LOCKED_STYLE
        tokens = estimate_tokens(locked)
        assert tokens <= 47, (
            f"LOCKED_STYLE estimated {tokens} tokens — too long. "
            f"Must leave room for subject + emotion + composition. "
            f"Current: {locked!r}"
        )

    def test_locked_negative_fits_in_clip_budget(self):
        """Negative is a separate field but ALSO goes through CLIP."""
        negative = VisualPromptEngineer.LOCKED_NEGATIVE
        tokens = estimate_tokens(negative)
        assert tokens <= CLIP_TOKEN_LIMIT, (
            f"LOCKED_NEGATIVE estimated {tokens} tokens — exceeds CLIP "
            f"limit {CLIP_TOKEN_LIMIT}"
        )

    def test_full_positive_prompt_under_clip_limit(self):
        """Real composed prompt (subject + emotion + LOCKED_STYLE) ≤ 77 tokens."""
        # Realistic Phase 2 inputs after deep visual prompt generation
        positive, _ = VisualPromptEngineer.build_prompt(
            subject="ancient olive tree on a quiet hillside",
            action="standing alone under a wide sky",
            environment="Mediterranean countryside at dusk",
            emotion="reverent",
        )
        tokens = estimate_tokens(positive)
        assert tokens <= CLIP_TOKEN_LIMIT, (
            f"Composed prompt = {tokens} tokens, exceeds CLIP {CLIP_TOKEN_LIMIT}. "
            f"The TAIL (safety constraints + NotebookLM tag) WILL get truncated. "
            f"Prompt: {positive!r}"
        )

    def test_safety_constraints_survive_clip_truncation(self):
        """Even at the budget edge, the critical phrases must be reachable.
        Build a prompt with the LONGEST plausible inputs and verify the
        safety constraints still appear in the first 77 tokens.
        """
        # Longest realistic deep-visual subject
        positive, _ = VisualPromptEngineer.build_prompt(
            subject="a magnificent ancient olive tree with twisted gnarled branches",
            action="standing solitary on a windswept rocky hilltop overlooking the valley",
            environment="vast Mediterranean countryside under a dramatic sunset sky",
            emotion="reverent",
        )

        # Take only the first 77 tokens worth (~59 words at 1.3 ratio)
        max_visible_words = int(CLIP_TOKEN_LIMIT / WORD_TO_TOKEN_RATIO)
        visible = ' '.join(positive.split()[:max_visible_words])

        # Critical safety phrases that must NOT be lost to truncation
        critical_must_appear = [
            "no faces",  # prevents AI-rendered human faces in religious content
            "no text",   # prevents random Arabic gibberish in image
            "watercolor",  # core visual identity
        ]
        missing = [p for p in critical_must_appear if p not in visible]
        assert not missing, (
            f"After CLIP truncation at {CLIP_TOKEN_LIMIT} tokens, "
            f"these CRITICAL phrases are LOST: {missing}. "
            f"Visible part: {visible!r}"
        )


# ════════════════════════════════════════════════════════════════
# Per-emotion variations
# ════════════════════════════════════════════════════════════════
class TestPromptAcrossEmotions:
    @pytest.mark.parametrize("emotion", ["warm", "reverent", "playful", "peaceful", "excited"])
    def test_each_emotion_yields_clip_safe_prompt(self, emotion):
        positive, _ = VisualPromptEngineer.build_prompt(
            subject="elephant carrying ornate howdah",
            action="walking on desert sand",
            environment="vast desert landscape",
            emotion=emotion,
        )
        tokens = estimate_tokens(positive)
        assert tokens <= CLIP_TOKEN_LIMIT, (
            f"emotion={emotion!r}: {tokens} tokens > {CLIP_TOKEN_LIMIT}"
        )


# ════════════════════════════════════════════════════════════════
# Regression — front-loading order
# ════════════════════════════════════════════════════════════════
class TestPromptStructure:
    def test_subject_appears_after_safety_prefix(self):
        """CLIP weights early tokens more, but safety comes first.
        Subject must appear right after SAFETY_PREFIX."""
        positive, _ = VisualPromptEngineer.build_prompt(
            subject="distinctive subject XYZ",
            emotion="warm",
        )
        # Subject must appear before any LOCKED_STYLE tags
        subject_pos = positive.find("distinctive subject XYZ")
        watercolor_pos = positive.find("watercolor")
        assert subject_pos > 0 and watercolor_pos > 0, "Both must exist"
        assert subject_pos < watercolor_pos, (
            f"Subject must come before LOCKED_STYLE. "
            f"subject@{subject_pos}, watercolor@{watercolor_pos}"
        )

    def test_safety_constraints_come_first(self):
        """SAFETY_PREFIX must appear at the very start of the prompt so it
        survives any truncation, even from a long subject from
        DeepVisualPromptGenerator."""
        positive, _ = VisualPromptEngineer.build_prompt(
            subject="distinctive subject XYZ",
            emotion="warm",
        )
        # Safety prefix MUST be at the start
        assert positive.startswith("no faces, no text, no logos"), (
            f"SAFETY_PREFIX must come first. Got start: {positive[:60]!r}"
        )
