"""
models.py — VALUE / QEEMA v3 (Enterprise Architecture)
نماذج البيانات المدققة (Pydantic v2)
تعمل كـ Guardrails (حواجز أمان) لضمان:
1. الإيقاع السريع لمنع الملل البصري (Micro-segmentation).
2. الفلترة الإجبارية لستايل الإنفوجرافيك.
3. حماية النصوص من أخطاء التشكيل.
"""

from __future__ import annotations
import re
import logging
from enum import Enum
from typing import Optional
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
# COMMON VALIDATORS (Guardrails)
# ═══════════════════════════════════════════════════════
def sanitize_visual_prompt(v: str) -> str:
    """يحذف الكلمات الممنوعة (3D, Realistic) ويجبر النظام على الإنفوجرافيك"""
    forbidden = ["3d", "pixar", "realistic", "photo", "photography", "render", "octane"]
    v_lower = v.lower()
    for word in forbidden:
        v_lower = re.sub(rf'\b{word}\b', '', v_lower)
    
    # إذا نسي الموديل وضع الستايل، نحقنه نحن بالقوة
    if "flat" not in v_lower and "vector" not in v_lower:
        v_lower += ", flat vector graphic, minimal infographic"
        
    # تنظيف الفواصل الزائدة
    return re.sub(r',\s*,', ',', v_lower).strip().strip(',')

def clean_arabic_text(v: str) -> str:
    """حماية ضد التشكيل الآلي لتجنب نطق الروبوتات"""
    # يزيل التنوين والتشكيل من نهاية الكلمات
    cleaned = re.sub(r'([ًٌٍَُِ~])(?=\s*[،.؟!])', '', v)
    return cleaned.strip()

def check_pacing(v: str, max_words: int = 30) -> str:
    """يراقب طول النص لمنع الملل البصري (Scene Fatigue)"""
    words = len(v.split())
    if words > max_words:
        logger.warning(f"⚠️ [Pacing Alert] نص طويل جداً ({words} كلمة). قد يسبب جموداً بصرياً في الفيديو! النص: {v[:30]}...")
    return v


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
    duration_sec: float           = Field(..., ge=3, le=45) # تم تقليل الحد الأقصى لدعم الإيقاع السريع
    narrator_text: str            = Field(..., min_length=5)
    visual_prompt: str            = Field(..., min_length=10)
    on_screen_text: Optional[str] = None
    mood:         AudioMood       = AudioMood.CALM

    audio_path:  Optional[str] = None
    image_path:  Optional[str] = None
    video_path:  Optional[str] = None

    @field_validator("visual_prompt")
    @classmethod
    def enforce_infographic_style(cls, v: str) -> str:
        return sanitize_visual_prompt(v)

    @field_validator("narrator_text")
    @classmethod
    def enforce_short_text(cls, v: str) -> str:
        v = clean_arabic_text(v)
        return check_pacing(v, max_words=25)


class AyahScene(BaseModel):
    """مشهد آية قرآنية مع تلاوة + شرح"""
    scene_id:     int
    ayah:         VerifiedAyah
    intro_text:   str                = Field(..., min_length=5)
    explain_text: str                = Field(..., min_length=10)
    visual_prompt: str               = Field(..., min_length=10)
    repetitions:  int                = Field(default=3, ge=1, le=5)
    duration_sec: float              = Field(..., ge=10, le=120)

    intro_audio:   Optional[str] = None
    quran_audio:   Optional[str] = None
    explain_audio: Optional[str] = None
    image_path:    Optional[str] = None
    video_path:    Optional[str] = None

    @field_validator("visual_prompt")
    @classmethod
    def enforce_infographic_style(cls, v: str) -> str:
        return sanitize_visual_prompt(v)

    @field_validator("intro_text", "explain_text")
    @classmethod
    def enforce_short_text(cls, v: str) -> str:
        v = clean_arabic_text(v)
        return check_pacing(v, max_words=35) # الشرح قد يكون أطول قليلاً، لكن مراقب!


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

    @model_validator(mode="after")
    def validate_episode_pacing(self) -> EpisodeScript:
        """يضمن أن الحلقة تحتوي على مشاهد كافية (Mid Scenes) لتفادي الملل البصري"""
        # إذا كان عدد الآيات قليلاً ولا يوجد Mid Scenes، نسجل تحذير (يستخدم لتحسين Prompts مستقبلاً)
        if len(self.ayah_scenes) < 3 and len(self.mid_scenes) == 0:
            logger.warning("⚠️ [Episode Architecture] الحلقة تفتقر إلى Mid Scenes! قد يؤثر ذلك على إيقاع الفيديو.")
        return self


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
