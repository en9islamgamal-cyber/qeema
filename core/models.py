"""
core/models.py — VALUE / QEEMA v11.0 (Production)
====================================================
Pydantic v2 models for type-safe data flow throughout the pipeline.

[Why Pydantic v2]
- Schema validation: لو LLM رجع JSON غلط، نمسكه قبل ما يدخل في pipeline
- Self-documenting: كل field معروف نوعه ومداه
- IDE support: autocomplete + type checking
- Serialization: model_dump_json() + model_validate_json() لـ persistence

[Domain Vocabulary]
- VerifiedAyah:   آية تم التحقق من نصها من API موثوقة
- NarratorScene:  مشهد سرد عام (intro/outro/mid)
- AyahScene:      مشهد آية محددة (intro + recitation + explain)
- EpisodeScript:  سكريبت كامل للحلقة
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ════════════════════════════════════════════════════════════════
# Enums (closed-set domain types)
# ════════════════════════════════════════════════════════════════
class AudioMood(str, Enum):
    """Mood للموسيقى الخلفية أو لتوجيه TTS."""
    INTRO = "intro"
    CALM = "calm"
    HAPPY = "happy"
    EXCITED = "excited"
    REVERENT = "reverent"
    OUTRO = "outro"


class SceneType(str, Enum):
    """نوع المشهد في الـ pipeline."""
    INTRO = "intro"
    EXPLANATION = "explanation"
    AYAH = "ayah"
    OUTRO = "outro"


class EpisodeStatus(str, Enum):
    """حالة الحلقة في DB. ASCII فقط لأن DB lookups."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_QUALITY = "failed_quality"
    FAILED_PERMANENT = "failed_permanent"


class VisualScene(str, Enum):
    """نوع المشهد البصري للـ procedural rendering."""
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


class PaletteName(str, Enum):
    """ألوان متاحة للمشاهد."""
    WARM_SUNSET = "warm_sunset"
    CALM_BLUE = "calm_blue"
    LUSH_GREEN = "lush_green"
    NIGHT_STARS = "night_stars"
    GOLDEN_HOUR = "golden_hour"


# ════════════════════════════════════════════════════════════════
# Arabic text normalization
# ════════════════════════════════════════════════════════════════
_TASHKEEL_END_RE = re.compile(r"[\u064B-\u0652]+(?=\s|$|[،.؟!])")
_WS_RE = re.compile(r"\s+")
_PAUSE_WORDS = ("الله", "سبحانه", "تعالى", "الرحمن", "الرحيم")


def humanize_arabic_text(text: str) -> str:
    """
    تحضير النص العربي للسرد:
    - إزالة التشكيل في نهايات الكلمات (يقلل artifacts في TTS)
    - تحويل الفواصل العربية لوقفات
    - إضافة وقفات صغيرة بعد أسماء الجلالة (للوقار)
    - تطبيع المسافات
    """
    if not text:
        return ""
    text = _TASHKEEL_END_RE.sub("", text)
    text = text.replace("،", " ... ").replace("؛", " ... ")
    for word in _PAUSE_WORDS:
        text = text.replace(word, f"{word} ..")
    text = _WS_RE.sub(" ", text).strip()
    return text


# ════════════════════════════════════════════════════════════════
# Model: VerifiedAyah
# ════════════════════════════════════════════════════════════════
class VerifiedAyah(BaseModel):
    """آية تم التحقق من نصها من Quran API."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    surah: int = Field(..., ge=1, le=114, description="رقم السورة")
    number: int = Field(..., ge=1, le=286, description="رقم الآية")
    text: str = Field(..., min_length=1, description="نص الآية بالرسم العثماني")
    audio_url: Optional[str] = Field(None, description="URL للتلاوة (اختياري)")


# ════════════════════════════════════════════════════════════════
# Model: NarratorScene
# ════════════════════════════════════════════════════════════════
class NarratorScene(BaseModel):
    """مشهد سرد بدون آية محددة (intro/outro/mid)."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    scene_id: int = Field(..., ge=0)
    scene_type: SceneType
    narrator_text: str = Field(..., description="نص السرد بالعامية المصرية")
    visual_prompt: str = Field("", description="legacy: وصف بصري بالإنجليزية")
    visual_scene: VisualScene = VisualScene.ABSTRACT_WARM
    palette: PaletteName = PaletteName.WARM_SUNSET
    keywords: List[str] = Field(default_factory=list, max_length=10)
    mood: AudioMood = AudioMood.CALM

    # Runtime artifacts (filled during pipeline)
    image_path: Optional[str] = None
    audio_path: Optional[str] = None

    @field_validator("narrator_text", mode="before")
    @classmethod
    def normalize_text(cls, v: Any) -> str:
        if v is None:
            return ""
        return humanize_arabic_text(str(v))


# ════════════════════════════════════════════════════════════════
# Model: AyahScene
# ════════════════════════════════════════════════════════════════
class AyahScene(BaseModel):
    """مشهد آية: intro_text → recitation (audio) → explain_text."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    scene_id: int = Field(..., ge=0)
    ayah: VerifiedAyah
    intro_text: str = Field(..., description="تمهيد بسيط قبل الآية")
    explain_text: str = Field(..., description="شرح الآية بالعامية للأطفال")
    visual_prompt: str = ""
    visual_scene: VisualScene = VisualScene.ABSTRACT_WARM
    palette: PaletteName = PaletteName.WARM_SUNSET
    keywords: List[str] = Field(default_factory=list, max_length=10)

    # Runtime artifacts
    image_path: Optional[str] = None
    intro_audio: Optional[str] = None
    explain_audio: Optional[str] = None
    ayah_audio: Optional[str] = None

    @field_validator("intro_text", "explain_text", mode="before")
    @classmethod
    def normalize_text(cls, v: Any) -> str:
        if v is None:
            return ""
        return humanize_arabic_text(str(v))


# ════════════════════════════════════════════════════════════════
# Model: EpisodeScript (root aggregate)
# ════════════════════════════════════════════════════════════════
class EpisodeScript(BaseModel):
    """السكريبت الكامل لحلقة واحدة. هو الـ source-of-truth للـ pipeline."""
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

    # Pipeline metadata (DB id, etc.)
    episode_id: Optional[str] = None

    @field_validator("youtube_tags")
    @classmethod
    def clean_tags(cls, v: List[str]) -> List[str]:
        # Remove empties, dedupe (preserve order), limit length
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
