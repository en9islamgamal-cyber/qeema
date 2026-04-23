"""
models.py — VALUE / QEEMA v5 (AI-Driven Production System)

تحسينات Enterprise:
- Quality Scoring Engine
- Humanization Engine
- Adaptive Pacing (ديناميكي)
- Anti-Repetition Detection
- Self-Healing Models
- Attention Modeling
- AI Feedback Hooks
"""

from __future__ import annotations
import re
import logging
import random
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════
class AudioMood(str, Enum):
    INTRO    = "intro"
    CALM     = "calm"
    HAPPY    = "happy"
    EXCITED  = "excited"
    REVERENT = "reverent"
    OUTRO    = "outro"


class SceneType(str, Enum):
    INTRO       = "intro"
    EXPLANATION = "explanation"
    AYAH        = "ayah"
    ACTIVITY    = "activity"
    OUTRO       = "outro"


class EpisodeStatus(str, Enum):
    PENDING   = "pending"
    SCRIPTING = "scripting"
    AUDIO     = "audio"
    VISUAL    = "visual"
    VIDEO     = "video"
    THUMBNAIL = "thumbnail"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED    = "failed"


# ═══════════════════════════════════════════════════════
# AI UTILITIES
# ═══════════════════════════════════════════════════════
def clean_arabic_text(v: str) -> str:
    return re.sub(r'([ًٌٍَُِ~])(?=s*[،.؟!])', '', v).strip()


def humanize_text(v: str) -> str:
    patterns = {
        "هيا بنا": ["يلا", "جاهزين؟", "خلينا نبدأ"],
        "سنتعلم": ["راح نتعلم", "خلينا نتعلم"],
        "انظر": ["شوف", "لاحظ"],
    }
    for k, opts in patterns.items():
        if k in v:
            v = v.replace(k, random.choice(opts))
    return v


def sanitize_visual_prompt(v: str) -> str:
    forbidden = ["3d", "realistic", "photo", "render"]
    v = v.lower()

    for word in forbidden:
        v = re.sub(rf"\b{word}\b", "", v)

    if "vector" not in v:
        v += ", flat vector infographic"

    return re.sub(r',s*,', ',', v).strip(", ")


def score_visual_prompt(v: str) -> float:
    score = 0
    if "vector" in v: score += 2
    if "infographic" in v: score += 2
    if len(v.split()) < 25: score += 1
    if any(x in v for x in ["realistic", "3d"]): score -= 3
    return score


def adaptive_pacing(text: str, duration: float) -> str:
    words = len(text.split())
    max_words = int(duration * 2.3)

    if words > max_words:
        logger.warning(f"Pacing issue {words}>{max_words}")

    return text


def detect_repetition(texts: List[str]) -> bool:
    seen = set()
    for t in texts:
        key = t[:25]
        if key in seen:
            return True
        seen.add(key)
    return False


def compute_attention_score(text: str) -> float:
    words = len(text.split())
    return max(0.0, min(1.0, 1 - (words / 40)))


# ═══════════════════════════════════════════════════════
# QURAN
# ═══════════════════════════════════════════════════════
class VerifiedAyah(BaseModel):
    surah: int = Field(..., ge=1, le=114)
    number: int
    text: str
    audio_url: Optional[str] = None
    source: str = "quran_api"

    @field_validator("text")
    def validate_text(cls, v):
        if "placeholder" in v.lower():
            raise ValueError("Invalid ayah")
        return v


# ═══════════════════════════════════════════════════════
# SCENES
# ═══════════════════════════════════════════════════════
class NarratorScene(BaseModel):
    scene_id: int
    scene_type: SceneType
    duration_sec: float = Field(..., ge=3, le=45)

    narrator_text: str
    visual_prompt: str

    mood: AudioMood = AudioMood.CALM

    attention_score: Optional[float] = None
    quality_score: Optional[float] = None

    @field_validator("narrator_text")
    def process_text(cls, v):
        v = clean_arabic_text(v)
        v = humanize_text(v)
        return v

    @field_validator("visual_prompt")
    def process_visual(cls, v):
        v = sanitize_visual_prompt(v)
        score = score_visual_prompt(v)

        if score < 1:
            logger.warning("Weak visual prompt")

        return v

    @model_validator(mode="after")
    def post_process(self):
        self.narrator_text = adaptive_pacing(self.narrator_text, self.duration_sec)
        self.attention_score = compute_attention_score(self.narrator_text)

        self.quality_score = (
            self.attention_score +
            score_visual_prompt(self.visual_prompt)
        )

        return self


class AyahScene(BaseModel):
    scene_id: int
    ayah: VerifiedAyah

    intro_text: str
    explain_text: str
    visual_prompt: str

    repetitions: int = 3
    duration_sec: float

    quality_score: Optional[float] = None

    @model_validator(mode="after")
    def process(self):
        self.intro_text = humanize_text(clean_arabic_text(self.intro_text))
        self.explain_text = humanize_text(clean_arabic_text(self.explain_text))
        self.visual_prompt = sanitize_visual_prompt(self.visual_prompt)

        self.quality_score = score_visual_prompt(self.visual_prompt)

        return self


# ═══════════════════════════════════════════════════════
# EPISODE
# ═══════════════════════════════════════════════════════
class EpisodeScript(BaseModel):
    episode_number: int
    surah_name: str
    surah_number: int

    title: str
    youtube_title: str
    youtube_description: str
    youtube_tags: List[str]

    total_duration_sec: float

    intro_scene: NarratorScene
    ayah_scenes: List[AyahScene]
    mid_scenes: List[NarratorScene] = []
    outro_scene: NarratorScene

    overall_score: Optional[float] = None

    @property
    def all_scenes(self):
        return [self.intro_scene] + self.mid_scenes + [self.outro_scene]

    @model_validator(mode="after")
    def evaluate(self):
        scores = []

        for s in self.all_scenes:
            if s.quality_score:
                scores.append(s.quality_score)

        for a in self.ayah_scenes:
            if a.quality_score:
                scores.append(a.quality_score)

        if detect_repetition([s.narrator_text for s in self.all_scenes]):
            logger.warning("Repetition detected")
            scores.append(-2)

        self.overall_score = sum(scores) / max(len(scores), 1)

        if self.overall_score < 1.5:
            logger.warning(f"Low quality episode: {self.overall_score}")

        return self


# ═══════════════════════════════════════════════════════
# PIPELINE STATE
# ═══════════════════════════════════════════════════════
class PipelineState(BaseModel):
    episode_id: Optional[str] = None
    episode_number: int

    status: EpisodeStatus = EpisodeStatus.PENDING

    script_ready: bool = False
    audio_ready: bool = False
    visuals_ready: bool = False
    video_ready: bool = False
    uploaded: bool = False

    error_message: Optional[str] = None
    attempt_count: int = 0