```python
"""
models.py — VALUE / QEEMA v8.0 (Enterprise Data & NLP Engine)
================================================================
يحتوي على النماذج الأساسية للبيانات (Pydantic Models) وخوارزميات
هندسة النصوص المتقدمة لتوليد صوت بشري طبيعي خالي تماماً من الروبوتية.
"""

from __future__ import annotations
import re
import logging
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 1. ENUMS (حالات النظام والمشاهد)
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
# 2. NLP & TTS Tuning (هندسة النطق المتقدمة)
# ════════════════════════════════════════════════════════════════
def tune_text_for_human_tts(text: str) -> str:
    """
    خوارزمية معالجة النصوص للحصول على أداء صوتي بشري (ElevenLabs/Google):
    - تزيل التشكيل الطرفي (التنوين، الضم، الكسر) من نهايات الكلمات قبل علامات الترقيم.
      هذا يجبر الذكاء الاصطناعي على "الوقوف على سكون" بدلاً من النطق النحوي الآلي.
    - تستبدل علامات الترقيم الحادة بوقفات تنفس طبيعية (SSML-like delays).
    """
    if not text: 
        return text
    
    # 1. إزالة التشكيل من نهاية الكلمات التي يتبعها مسافة أو علامة ترقيم
    # \u064B-\u0652 هي حركات التشكيل العربية
    text = re.sub(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])', '', text)
    
    # 2. استبدال علامات الترقيم بوقفات تنفس طبيعية (توسيع المدة الزمنية للسكوت)
    text = text.replace("،", " ... ")
    text = text.replace("!", "! ... ")
    text = text.replace("؟", "؟ ... ")
    
    # 3. إزالة أي مسافات مزدوجة نتجت عن الاستبدال لتنظيف النص
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ════════════════════════════════════════════════════════════════
# 3. Pydantic Models (هياكل البيانات الصارمة)
# ════════════════════════════════════════════════════════════════
class VerifiedAyah(BaseModel):
    surah: int = Field(..., ge=1, le=114)
    number: int
    text: str
    audio_url: Optional[str] = None
    source: str = "quran_api"

class NarratorScene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    scene_id: int
    scene_type: SceneType
    duration_sec: float = Field(..., ge=3, le=60)
    narrator_text: str
    visual_prompt: str
    mood: AudioMood = AudioMood.CALM
    
    image_path: Optional[str] = None
    audio_path: Optional[str] = None
    quality_score: Optional[float] = None

    @field_validator("narrator_text", mode="before")
    def process_text(cls, v):
        return tune_text_for_human_tts(v)

class AyahScene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    scene_id: int
    ayah: VerifiedAyah
    
    intro_text: str
    explain_text: str
    visual_prompt: str
    
    repetitions: int = 3
    duration_sec: float
    
    image_path: Optional[str] = None
    intro_audio: Optional[str] = None
    explain_audio: Optional[str] = None
    ayah_audio: Optional[str] = None
    quality_score: Optional[float] = None

    @field_validator("intro_text", "explain_text", mode="before")
    def process_text(cls, v):
        return tune_text_for_human_tts(v)

class EpisodeScript(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    episode_id: Optional[str] = None
    episode_number: int
    surah_name: str
    surah_number: int
    
    title: str
    youtube_title: str
    youtube_description: str
    youtube_tags: List[str] = []
    
    total_duration_sec: float
    
    intro_scene: NarratorScene
    ayah_scenes: List[AyahScene]
    mid_scenes: List[NarratorScene] = []
    outro_scene: NarratorScene
    
    overall_score: Optional[float] = None

    @property
    def all_narrator_scenes(self) -> List[NarratorScene]:
        return [self.intro_scene] + self.mid_scenes + [self.outro_scene]
        
    @property
    def all_scenes(self) -> List[NarratorScene]:
        # Alias for backward compatibility with orchestrator
        return self.all_narrator_scenes

class PipelineState(BaseModel):
    episode_id: Optional[str] = None
    episode_number: int
    status: EpisodeStatus = EpisodeStatus.PENDING


```
