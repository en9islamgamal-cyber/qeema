"""
models.py — VALUE / QEEMA v6.0 (World-Class Edition)
تمت إضافة طبقات التحليل المتقدمة للنصوص وتوحيد النمط البصري.
"""
from __future__ import annotations
import re
import logging
import random
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

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
    ACTIVITY = "activity"
    OUTRO = "outro"

def clean_arabic_text(v: str) -> str:
    """تنظيف دقيق يضمن نطقاً بشرياً طبيعياً لتجنب اللكنة الآلية."""
    if not v: return v
    # إزالة التشكيل الزائد في نهايات الكلمات الذي يسبب وقفات آلية
    v = re.sub(r"[\u064B-\u0650\u0652]+(?=\s|$|[،.؟!])", "", v)
    v = re.sub(r"\s+", " ", v)
    # استبدال الفواصل التقليدية بوقفات تنفسية طبيعية (SSML Break markers style)
    v = v.replace("،", "، ")
    return v.strip()

def sanitize_visual_prompt(v: str) -> str:
    """
    هندسة الأوامر البصرية (Prompt Engineering):
    ضمان نمط بصري (Art Style) ثابت وعالمي يشبه إنتاجات ديزني/بيكسار ثنائية الأبعاد.
    """
    forbidden = ["3d", "realistic", "photo", "render", "photograph", "text", "words", "letters"]
    v = v.lower()
    for word in forbidden:
        v = re.sub(rf"\b{word}\b", "", v)
    
    # إضافة الأسلوب الفني العالمي الموحد لكل الصور
    master_style = (
        ", high quality 2d flat vector illustration, studio ghibli style background, "
        "soft pastel colors, highly detailed, children's book illustration, cinematic lighting, 8k resolution"
    )
    if "vector" not in v:
        v += master_style
    
    return re.sub(r",\s*,", ",", v).strip(", ")

class NarratorScene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scene_id: int
    scene_type: SceneType
    duration_sec: float = Field(..., ge=3, le=45)
    narrator_text: str
    visual_prompt: str
    mood: AudioMood = AudioMood.CALM
    image_path: Optional[str] = None
    audio_path: Optional[str] = None

    @field_validator("narrator_text")
    def process_text(cls, v):
        return clean_arabic_text(v)

    @field_validator("visual_prompt")
    def process_visual(cls, v):
        return sanitize_visual_prompt(v)

# ... (باقي الكود يظل كما هو مع التأكد من استخدام هذه الدوال)
