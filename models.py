"""
models.py — VALUE / QEEMA v9.1 (NLP & Prosody + Pipeline-Compat)
==================================================================
v9.1 = v9.0 (NLP) + الحقول الناقصة اللي بيستخدمها الـ orchestrator والمحركات:
  - EpisodeStatus enum
  - mid_scenes في EpisodeScript
  - episode_id, youtube_tags
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
    """✅ مرجَّع — كان مفقود وكان السبب في فشل import في orchestrator"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ════════════════════════════════════════════════════════════════
# NLP Helper — تطبيع وتحضير النص العربي للـ TTS
# ════════════════════════════════════════════════════════════════
def humanize_arabic_text(text: str) -> str:
    """
    خوارزمية المعالجة اللغوية المتقدمة (Advanced NLP Processing):
    1. تصفية التشكيل الطرفي (الإعراب) لضمان الوقف الصحيح على سكون.
    2. إدخال "وقفات تأملية" (...) في مواضع الفواصل لضبط سرعة النطق.
    3. إضافة وقفات تأملية بعد لفظ الجلالة لزيادة الوقار.
    """
    if not text:
        return ""

    # إزالة تشكيل نهايات الكلمات (تسكين الأواخر تلقائياً)
    text = re.sub(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])', '', text)

    # تحويل الفواصل إلى وقفات زمنية
    text = text.replace("،", " ... ").replace("؛", " ... ")

    # وقفة تأملية بعد لفظ الجلالة والكلمات العظيمة
    words_to_pause = ["الله", "سبحانه", "تعالى", "الرحمن", "الرحيم"]
    for w in words_to_pause:
        text = text.replace(w, f"{w} ..")

    # تنظيف المسافات الزائدة
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
    visual_prompt: str
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
    visual_prompt: str
    image_path: Optional[str] = None
    intro_audio: Optional[str] = None
    explain_audio: Optional[str] = None
    ayah_audio: Optional[str] = None

    @field_validator("intro_text", "explain_text", mode="before")
    def process_text(cls, v):
        return humanize_arabic_text(v)


class EpisodeScript(BaseModel):
    """
    ✅ v9.1: أُضيفت الحقول التي يستخدمها الـ orchestrator والمحركات:
       - mid_scenes (للمشاهد الوسطية الإضافية)
       - episode_id (لربط Supabase)
       - youtube_tags (للرفع)
    كلها optional/افتراضية لضمان عدم كسر السكريبتات الموجودة.
    """
    model_config = ConfigDict(extra="ignore")

    episode_number: int
    surah_name: str
    title: str
    youtube_title: str
    youtube_description: str
    youtube_tags: List[str] = Field(default_factory=list)   # ✅ جديد

    intro_scene: NarratorScene
    ayah_scenes: List[AyahScene]
    mid_scenes: List[NarratorScene] = Field(default_factory=list)   # ✅ جديد
    outro_scene: NarratorScene

    # حقل تشغيل (يُملأ من الـ orchestrator)
    episode_id: Optional[str] = None   # ✅ جديد
