"""
core/models.py — VALUE / QEEMA v14.0 (Cinematic Upgrade)
====================================================
Pydantic v2 models with cinematic storytelling fields.

[v14.0 NEW FIELDS]
- AyahScene.hook_text       : opening hook (grabs kids' attention)
- AyahScene.story_text      : mini-story/analogy from child's daily life
- AyahScene.moral_text      : clear lesson extracted from the ayah
- AyahScene.scene_emotion   : emotional tone hint for TTS + visuals
- AyahScene.transition_type : visual transition to next scene
- NarratorScene.scene_emotion
- EpisodeScript.cta_text    : subscribe/follow CTA before outro
- New VisualScene types: golden_field, starry_night, child_reading, rainbow, flowers
- New PaletteName: soft_morning, deep_teal
- New Enum SceneEmotion, TransitionType
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ════════════════════════════════════════════════════════════════
# Enums
# ════════════════════════════════════════════════════════════════
class AudioMood(str, Enum):
    INTRO = "intro"
    CALM = "calm"
    HAPPY = "happy"
    EXCITED = "excited"
    REVERENT = "reverent"
    OUTRO = "outro"


class SceneType(str, Enum):
    INTRO = "intro"
    HOOK = "hook"
    EXPLANATION = "explanation"
    STORY = "story"
    AYAH = "ayah"
    MORAL = "moral"
    OUTRO = "outro"


class EpisodeStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_QUALITY = "failed_quality"
    FAILED_PERMANENT = "failed_permanent"


class VisualScene(str, Enum):
    GARDEN = "garden"
    SKY = "sky"
    HOUSE = "house"
    MOSQUE = "mosque"
    OCEAN = "ocean"
    DESERT = "desert"
    MOUNTAINS = "mountains"
    CHILD_PRAYING = "child_praying"
    FAMILY = "family"
    ABSTRACT_WARM = "abstract_warm"
    # v14 NEW — richer environments
    GOLDEN_FIELD = "golden_field"
    STARRY_NIGHT = "starry_night"
    CHILD_READING = "child_reading"
    RAINBOW = "rainbow"
    FLOWERS = "flowers"


class PaletteName(str, Enum):
    WARM_SUNSET = "warm_sunset"
    CALM_BLUE = "calm_blue"
    LUSH_GREEN = "lush_green"
    NIGHT_STARS = "night_stars"
    GOLDEN_HOUR = "golden_hour"
    # v14 NEW
    SOFT_MORNING = "soft_morning"
    DEEP_TEAL = "deep_teal"


class SceneEmotion(str, Enum):
    """Emotional tone — drives TTS voice settings + visual color grading."""
    WARM = "warm"           # دافئ — family / home scenes
    REVERENT = "reverent"   # خشوع — Quran recitation display
    PLAYFUL = "playful"     # مرح — hook / story / analogy
    PEACEFUL = "peaceful"   # هدوء — moral / outro
    EXCITED = "excited"     # حماس — intro / CTA


class TransitionType(str, Enum):
    """Visual transition between scenes (applied in FFmpeg assembly)."""
    FADE = "fade"
    ZOOM_IN = "zoom_in"
    SLIDE_RIGHT = "slide_right"
    DISSOLVE = "dissolve"
    NONE = "none"


# ════════════════════════════════════════════════════════════════
# Arabic text normalization
# ════════════════════════════════════════════════════════════════
_TASHKEEL_END_RE = re.compile(r"[\u064B-\u0652]+(?=\s|$|[،.؟!])")
_WS_RE = re.compile(r"\s+")
_PAUSE_WORDS = ("الله", "سبحانه", "تعالى", "الرحمن", "الرحيم")


def humanize_arabic_text(text: str) -> str:
    if not text:
        return ""
    text = _TASHKEEL_END_RE.sub("", text)
    text = text.replace("،", " ... ").replace("؛", " ... ")
    for word in _PAUSE_WORDS:
        text = text.replace(word, f"{word} ..")
    text = _WS_RE.sub(" ", text).strip()
    return text


# ════════════════════════════════════════════════════════════════
# VerifiedAyah
# ════════════════════════════════════════════════════════════════
class VerifiedAyah(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    surah: int = Field(..., ge=1, le=114)
    number: int = Field(..., ge=1, le=286)
    text: str = Field(..., min_length=1)
    audio_url: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# NarratorScene (intro / outro / mid)
# ════════════════════════════════════════════════════════════════
class NarratorScene(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    scene_id: int = Field(..., ge=0)
    scene_type: SceneType
    narrator_text: str = Field(..., description="نص السرد بالعامية المصرية")
    visual_prompt: str = Field("", description="English visual description")
    visual_scene: VisualScene = VisualScene.ABSTRACT_WARM
    palette: PaletteName = PaletteName.WARM_SUNSET
    keywords: List[str] = Field(default_factory=list, max_length=10)
    mood: AudioMood = AudioMood.CALM
    scene_emotion: SceneEmotion = SceneEmotion.WARM      # v14
    transition_type: TransitionType = TransitionType.FADE  # v14

    # Runtime artifacts
    image_path: Optional[str] = None
    audio_path: Optional[str] = None

    @field_validator("narrator_text", mode="before")
    @classmethod
    def normalize_text(cls, v: Any) -> str:
        return humanize_arabic_text(str(v)) if v else ""


# ════════════════════════════════════════════════════════════════
# AyahScene — v14 Cinematic Upgrade
# ════════════════════════════════════════════════════════════════
class AyahScene(BaseModel):
    """
    Cinematic ayah scene structure:
    hook → intro → story/analogy → [Quran recitation] → explain → moral

    This maps to 5-6 individual video sub-segments per ayah,
    producing ~2-3 minutes of rich educational content per ayah.
    """
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    scene_id: int = Field(..., ge=0)
    ayah: VerifiedAyah

    # Original fields (kept for backward compat)
    intro_text: str = Field("", description="تمهيد قبل الآية")
    explain_text: str = Field("", description="شرح مباشر للآية")

    # v14 NEW — Cinematic storytelling layers
    hook_text: Optional[str] = Field(
        None,
        description="Hook جملة واحدة تشد الطفل — سؤال أو موقف مثير (max 30 كلمة)"
    )
    story_text: Optional[str] = Field(
        None,
        description="قصة قصيرة أو تشبيه من حياة الطفل يوضح معنى الآية (max 70 كلمة)"
    )
    moral_text: Optional[str] = Field(
        None,
        description="الحكمة أو الدرس المستفاد في جملة واضحة (max 25 كلمة)"
    )
    scene_emotion: SceneEmotion = SceneEmotion.WARM
    transition_type: TransitionType = TransitionType.FADE

    visual_prompt: str = Field(
        "",
        description="Detailed English visual prompt for image generation"
    )
    visual_scene: VisualScene = VisualScene.ABSTRACT_WARM
    palette: PaletteName = PaletteName.WARM_SUNSET
    keywords: List[str] = Field(default_factory=list, max_length=10)

    # Runtime artifacts
    image_path: Optional[str] = None
    intro_audio: Optional[str] = None
    hook_audio: Optional[str] = None
    story_audio: Optional[str] = None
    explain_audio: Optional[str] = None
    moral_audio: Optional[str] = None
    ayah_audio: Optional[str] = None

    @field_validator("intro_text", "explain_text", mode="before")
    @classmethod
    def normalize_text(cls, v: Any) -> str:
        return humanize_arabic_text(str(v)) if v else ""

    @field_validator("hook_text", "story_text", "moral_text", mode="before")
    @classmethod
    def normalize_optional_text(cls, v: Any) -> Optional[str]:
        if not v:
            return None
        result = humanize_arabic_text(str(v))
        return result if result else None

    def all_narration_segments(self) -> List[tuple]:
        """
        Ordered (segment_key, text) pairs for all non-empty narration.
        Used by VoiceEngine for batch TTS synthesis.
        """
        segs = []
        if self.hook_text:
            segs.append(("hook", self.hook_text))
        if self.intro_text:
            segs.append(("intro", self.intro_text))
        if self.story_text:
            segs.append(("story", self.story_text))
        if self.explain_text:
            segs.append(("explain", self.explain_text))
        if self.moral_text:
            segs.append(("moral", self.moral_text))
        return segs


# ════════════════════════════════════════════════════════════════
# EpisodeScript — v14
# ════════════════════════════════════════════════════════════════
class EpisodeScript(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    episode_number: int = Field(..., ge=1)
    surah_name: str
    title: str
    youtube_title: str = Field(..., max_length=100)
    youtube_description: str = Field("", max_length=5000)
    youtube_tags: List[str] = Field(default_factory=list, max_length=20)

    intro_scene: NarratorScene
    ayah_scenes: List[AyahScene] = Field(..., min_length=1)
    mid_scenes: List[NarratorScene] = Field(default_factory=list)
    outro_scene: NarratorScene

    # v14 NEW
    cta_text: Optional[str] = Field(
        None,
        description="Call-to-action قبل الخاتمة (max 20 كلمة)"
    )

    # Pipeline metadata
    episode_id: Optional[str] = None

    @field_validator("youtube_tags")
    @classmethod
    def clean_tags(cls, v: List[str]) -> List[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for t in v:
            if not t or not isinstance(t, str):
                continue
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                cleaned.append(t)
        return cleaned[:20]
