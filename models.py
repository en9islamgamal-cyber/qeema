"""
models.py — VALUE / QEEMA v2
نماذج البيانات المدققة (Pydantic)
تضمن سلامة البيانات عبر جميع مراحل المنظومة
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


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
# QURAN
# ═══════════════════════════════════════════════════════
class VerifiedAyah(BaseModel):
    """آية قرآنية تم التحقق منها من مصدر موثوق"""
    surah:     int   = Field(..., ge=1, le=114)
    number:    int   = Field(..., ge=1)
    text:      str   = Field(..., min_length=3)
    audio_url: Optional[str] = None
    source:    str   = "quran_api"   # مصدر النص — لا يكون Gemini أبداً

    @field_validator("text")
    @classmethod
    def text_not_generated(cls, v: str) -> str:
        """يمنع أي نص قرآني مولَّد"""
        if "[AYAH" in v or "placeholder" in v.lower():
            raise ValueError("النص القرآني لم يُحقق منه — رفض مقبول")
        return v


# ═══════════════════════════════════════════════════════
# SCRIPT SCENES
# ═══════════════════════════════════════════════════════
class NarratorScene(BaseModel):
    """مشهد سرد عادي بصوت جدو أبو زياد"""
    scene_id:     int
    scene_type:   SceneType
    duration_sec: float           = Field(..., ge=3, le=120)
    narrator_text: str            = Field(..., min_length=5)
    visual_prompt: str            = Field(..., min_length=10)
    on_screen_text: Optional[str] = None
    mood:         AudioMood       = AudioMood.CALM

    # مسارات الملفات — تُملأ أثناء التنفيذ
    audio_path:  Optional[str] = None
    image_path:  Optional[str] = None
    video_path:  Optional[str] = None


class AyahScene(BaseModel):
    """مشهد آية قرآنية مع تلاوة + شرح"""
    scene_id:     int
    ayah:         VerifiedAyah
    intro_text:   str                = Field(..., min_length=5)
    explain_text: str                = Field(..., min_length=10)
    visual_prompt: str               = Field(..., min_length=10)
    repetitions:  int                = Field(default=3, ge=1, le=5)
    duration_sec: float              = Field(..., ge=10, le=180)

    # مسارات
    intro_audio:   Optional[str] = None
    quran_audio:   Optional[str] = None
    explain_audio: Optional[str] = None
    image_path:    Optional[str] = None
    video_path:    Optional[str] = None


# ═══════════════════════════════════════════════════════
# FULL EPISODE SCRIPT
# ═══════════════════════════════════════════════════════
class EpisodeScript(BaseModel):
    """سكريبت حلقة كاملة"""
    episode_number:      int
    surah_name:          str
    surah_number:        int
    title:               str
    youtube_title:       str = Field(..., max_length=100)
    youtube_description: str = Field(..., max_length=5000)
    youtube_tags:        list[str]
    total_duration_sec:  float
    target_age:          str = "5-6 سنوات"

    intro_scene:  NarratorScene
    ayah_scenes:  list[AyahScene]
    mid_scenes:   list[NarratorScene] = []
    outro_scene:  NarratorScene

    @property
    def all_narrator_scenes(self) -> list[NarratorScene]:
        return [self.intro_scene] + self.mid_scenes + [self.outro_scene]

    @property
    def scene_count(self) -> int:
        return 2 + len(self.ayah_scenes) + len(self.mid_scenes)


# ═══════════════════════════════════════════════════════
# PIPELINE STATE
# ═══════════════════════════════════════════════════════
class PipelineState(BaseModel):
    """حالة المنظومة لحلقة واحدة — محفوظة في Supabase"""
    episode_id:     Optional[str]  = None
    episode_number: int
    status:         EpisodeStatus  = EpisodeStatus.PENDING
    surah_name:     Optional[str]  = None

    script_ready:    bool = False
    audio_ready:     bool = False
    visuals_ready:   bool = False
    video_ready:     bool = False
    thumbnail_ready: bool = False
    uploaded:        bool = False

    video_path:      Optional[str] = None
    thumbnail_path:  Optional[str] = None
    youtube_url:     Optional[str] = None
    youtube_id:      Optional[str] = None

    error_message:   Optional[str] = None
    attempt_count:   int = 0