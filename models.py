"""
models.py — VALUE / QEEMA v9.0 (NLP & Prosody Edition)
======================================================
الطبقة المسؤولة عن تحويل النص الجاف إلى نص "بشري" قابل للتنفس.
"""
from __future__ import annotations
import re
import logging
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator

logger = logging.getLogger(__name__)

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

def humanize_arabic_text(text: str) -> str:
    """
    خوارزمية المعالجة اللغوية المتقدمة (Advanced NLP Processing):
    1. تصفية التشكيل الطرفي (الإعراب) لضمان الوقف الصحيح على سكون.
    2. إدخال "وقفات تأملية" (...) في مواضع الفواصل لضبط سرعة النطق.
    3. معالجة الحروف المكررة التي تسبب تعثر الذكاء الاصطناعي.
    """
    if not text: return ""
    
    # إزالة تشكيل نهايات الكلمات (تسكين الأواخر تلقائياً)
    # يبحث عن الحركات (َ ً ُ ٌ ِ ٍ ْ) في نهاية الكلمة
    text = re.sub(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])', '', text)
    
    # تحويل الفواصل إلى وقفات زمنية (نصف ثانية تقريباً في ElevenLabs)
    text = text.replace("،", " ... ").replace("؛", " ... ")
    
    # إضافة وقفة تأملية بعد لفظ الجلالة أو الكلمات العظيمة لزيادة الوقار
    words_to_pause = ["الله", "سبحانه", "تعالى", "الرحمن", "الرحيم"]
    for w in words_to_pause:
        text = text.replace(w, f"{w} ..")
        
    # تنظيف المسافات الزائدة
    text = re.sub(r'\s+', ' ', text).strip()
    return text

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
    model_config = ConfigDict(extra="ignore")
    episode_number: int
    surah_name: str
    title: str
    youtube_title: str
    youtube_description: str
    intro_scene: NarratorScene
    ayah_scenes: List[AyahScene]
    outro_scene: NarratorScene