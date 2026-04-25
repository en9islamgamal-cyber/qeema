"""
models.py — VALUE / QEEMA v5.0 (FIXED)
========================================
الإصلاح الجوهري: إضافة الحقول الناقصة (image_path, audio_path, audio_url, episode_id)
اللي كانت السبب الرئيسي لانهيار الـ Pipeline.
"""
from __future__ import annotations
import re
import logging
import random
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# ENUMS
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
    ACTIVITY = "activity"
    OUTRO = "outro"


class EpisodeStatus(str, Enum):
    PENDING = "pending"
    SCRIPTING = "scripting"
    AUDIO = "audio"
    VISUAL = "visual"
    VIDEO = "video"
    THUMBNAIL = "thumbnail"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"


# ════════════════════════════════════════════════════════════════
# AI UTILITIES
# ════════════════════════════════════════════════════════════════
# Tashkeel ranges (used aggressively to remove robotic-sounding diacritics)
_TASHKEEL_END = re.compile(r"[\u064B-\u0650\u0652]+(?=\s|$|[،.؟!])")
_MULTI_SPACE = re.compile(r"\s+")


def clean_arabic_text(v: str) -> str:
    """تنظيف التشكيل من نهايات الكلمات + توحيد المسافات."""
    if not v:
        return v
    v = _TASHKEEL_END.sub("", v)
    v = _MULTI_SPACE.sub(" ", v)
    return v.strip()


def humanize_text(v: str) -> str:
    """تحويل الجمل الفصحى الجامدة إلى لهجة دافئة للأطفال."""
    patterns = {
        "هيا بنا": ["يلا بينا", "خلونا", "تعالوا"],
        "سنتعلم": ["هنتعلم", "هنعرف", "خلينا نعرف"],
        "انظر": ["شوف", "بصّ", "تأمل"],
        "أحبائي": ["يا حبايبي", "يا أبطال", "يا نجوم"],
    }
    for k, opts in patterns.items():
        if k in v:
            v = v.replace(k, random.choice(opts))
    return v


def sanitize_visual_prompt(v: str) -> str:
    """ضمان نمط الإنفوجرافيك للأطفال + إزالة الكلمات المحظورة."""
    forbidden = ["3d", "realistic", "photo", "render", "photograph"]
    v = v.lower()
    for word in forbidden:
        v = re.sub(rf"\b{word}\b", "", v)
    if "vector" not in v and "illustration" not in v:
        v += ", flat 2d vector illustration, children's book style, pastel colors"
    return re.sub(r",\s*,", ",", v).strip(", ")


def score_visual_prompt(v: str) -> float:
    score = 0.0
    if "vector" in v or "illustration" in v: score += 2
    if "infographic" in v or "children" in v: score += 2
    if "pastel" in v or "warm" in v: score += 1
    if len(v.split()) < 35: score += 1
    if any(x in v for x in ["realistic", "3d", "photo"]): score -= 3
    return score


def adaptive_pacing(text: str, duration: float) -> str:
    """تنبيه إذا كان النص أطول من المدة."""
    words = len(text.split())
    max_words = int(duration * 2.3)  # ~2.3 wpm Arabic narration
    if words > max_words:
        logger.warning(f"⚠️ Pacing: {words} words for {duration}s (max {max_words})")
    return text


def detect_repetition(texts: List[str]) -> bool:
    seen = set()
    for t in texts:
        key = t[:25] if t else ""
        if key and key in seen:
            return True
        seen.add(key)
    return False


def compute_attention_score(text: str) -> float:
    words = len(text.split()) if text else 0
    return max(0.0, min(1.0, 1 - (words / 40)))


# ════════════════════════════════════════════════════════════════
# QURAN
# ════════════════════════════════════════════════════════════════
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


# ════════════════════════════════════════════════════════════════
# SCENES — مع إضافة الحقول الناقصة (image_path, audio_path)
# ════════════════════════════════════════════════════════════════
class NarratorScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene_id: int
    scene_type: SceneType
    duration_sec: float = Field(..., ge=3, le=45)

    narrator_text: str
    visual_prompt: str

    mood: AudioMood = AudioMood.CALM

    # ✅ FIX: Runtime artifact paths (filled by engines)
    image_path: Optional[str] = None
    audio_path: Optional[str] = None

    attention_score: Optional[float] = None
    quality_score: Optional[float] = None

    @field_validator("narrator_text")
    def process_text(cls, v):
        return humanize_text(clean_arabic_text(v))

    @field_validator("visual_prompt")
    def process_visual(cls, v):
        return sanitize_visual_prompt(v)

    @model_validator(mode="after")
    def post_process(self):
        self.narrator_text = adaptive_pacing(self.narrator_text, self.duration_sec)
        self.attention_score = compute_attention_score(self.narrator_text)
        self.quality_score = (self.attention_score + score_visual_prompt(self.visual_prompt))
        return self


class AyahScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene_id: int
    ayah: VerifiedAyah

    intro_text: str
    explain_text: str
    visual_prompt: str

    repetitions: int = 3
    duration_sec: float

    # ✅ FIX: Runtime artifact paths
    image_path: Optional[str] = None
    intro_audio: Optional[str] = None
    explain_audio: Optional[str] = None
    ayah_audio: Optional[str] = None  # The actual Quran recitation MP3

    quality_score: Optional[float] = None

    @model_validator(mode="after")
    def process(self):
        self.intro_text = humanize_text(clean_arabic_text(self.intro_text))
        self.explain_text = humanize_text(clean_arabic_text(self.explain_text))
        self.visual_prompt = sanitize_visual_prompt(self.visual_prompt)
        self.quality_score = score_visual_prompt(self.visual_prompt)
        return self


# ════════════════════════════════════════════════════════════════
# EPISODE
# ════════════════════════════════════════════════════════════════
class EpisodeScript(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # ✅ FIX: episode_id was being assigned by orchestrator but didn't exist
    episode_id: Optional[str] = None
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
    def all_narrator_scenes(self) -> List[NarratorScene]:
        return [self.intro_scene] + self.mid_scenes + [self.outro_scene]

    # Backwards-compat alias used in older code
    @property
    def all_scenes(self) -> List[NarratorScene]:
        return self.all_narrator_scenes

    @model_validator(mode="after")
    def evaluate(self):
        scores = []
        for s in self.all_narrator_scenes:
            if s.quality_score is not None:
                scores.append(s.quality_score)
        for a in self.ayah_scenes:
            if a.quality_score is not None:
                scores.append(a.quality_score)

        if detect_repetition([s.narrator_text for s in self.all_narrator_scenes]):
            logger.warning("⚠️ Repetition detected across narrator scenes")
            scores.append(-2)

        self.overall_score = sum(scores) / max(len(scores), 1)
        if self.overall_score < 1.5:
            logger.warning(f"⚠️ Low quality episode: {self.overall_score:.2f}")
        return self


# ════════════════════════════════════════════════════════════════
# PIPELINE STATE
# ════════════════════════════════════════════════════════════════
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
