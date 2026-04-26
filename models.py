"""
models.py — VALUE / QEEMA v10.0 (Procedural Edition)
=====================================================
Scene categorization for procedural rendering + semantic scene types.
"""
from __future__ import annotations
import re
import logging
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator

logger = logging.getLogger(__name__)


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
    EXPLANATION = "explanation"
    AYAH = "ayah"
    OUTRO = "outro"


class EpisodeStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class VisualScene(str, Enum):
    """✅ NEW v10: Semantic scene types for procedural rendering."""
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


# ════════════════════════════════════════════════════════════════
# NLP — تطبيع وتحضير النص العربي
# ════════════════════════════════════════════════════════════════
def humanize_arabic_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])', '', text)
    text = text.replace("،", " ... ").replace("؛", " ... ")
    words_to_pause = ["الله", "سبحانه", "تعالى", "الرحمن", "الرحيم"]
    for w in words_to_pause:
        text = text.replace(w, f"{w} ..")
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ════════════════════════════════════════════════════════════════
# Pydantic Models
# ════════════════════════════════════════════════════════════════
class VerifiedAyah(BaseModel):
    surah: int = Field(..., ge=1, le=114)
    number: int
    text: str
    audio_url: Optional[str] = None


class NarratorScene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scene_id: int
    scene_type: SceneType
    narrator_text: str
    visual_prompt: str = ""              # legacy field (مفيش لزمة في v10)
    visual_scene: VisualScene = VisualScene.ABSTRACT_WARM   # ✅ NEW
    palette: str = "warm_sunset"          # ✅ NEW: اسم palette من ProceduralConfig
    keywords: List[str] = Field(default_factory=list)        # ✅ NEW: للtransitions
    mood: AudioMood = AudioMood.CALM
    image_path: Optional[str] = None
    audio_path: Optional[str] = None

    @field_validator("narrator_text", mode="before")
    def process_text(cls, v):
        return humanize_arabic_text(v)


class AyahScene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scene_id: int
    ayah: VerifiedAyah
    intro_text: str
    explain_text: str
    visual_prompt: str = ""              # legacy
    visual_scene: VisualScene = VisualScene.ABSTRACT_WARM   # ✅ NEW
    palette: str = "warm_sunset"          # ✅ NEW
    keywords: List[str] = Field(default_factory=list)        # ✅ NEW
    image_path: Optional[str] = None
    intro_audio: Optional[str] = None
    explain_audio: Optional[str] = None
    ayah_audio: Optional[str] = None

    @field_validator("intro_text", "explain_text", mode="before")
    def process_text(cls, v):
        return humanize_arabic_text(v)


class EpisodeScript(BaseModel):
    model_config = ConfigDict(extra="ignore")
    episode_number: int
    surah_name: str
    title: str
    youtube_title: str
    youtube_description: str
    youtube_tags: List[str] = Field(default_factory=list)

    intro_scene: NarratorScene
    ayah_scenes: List[AyahScene]
    mid_scenes: List[NarratorScene] = Field(default_factory=list)
    outro_scene: NarratorScene

    episode_id: Optional[str] = None
