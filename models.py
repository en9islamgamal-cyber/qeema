"""
models.py — VALUE / QEEMA v6.0 (World-Class Edition)
========================================
تمت إضافة طبقات التحليل المتقدمة للنصوص وتوحيد النمط البصري،
مع الحفاظ على كافة الحقول المطلوبة لضمان استقرار الـ Pipeline.
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
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

# ════════════════════════════════════════════════════════════════
# AI UTILITIES & SMART LAYERS
# ════════════════════════════════════════════════════════════════
def clean_arabic_text(v: str) -> str:
    """تنظيف دقيق يضمن نطقاً بشرياً طبيعياً لتجنب اللكنة الآلية للذكاء الاصطناعي."""
    if not v:
        return v
    # إزالة التشكيل الزائد في نهايات الكلمات الذي يسبب وقفات آلية مزعجة
    v = re.sub(r"[\u064B-\u0650\u0652]+(?=\s|$|[،.؟!])", "", v)
    v = re.sub(r"\s+", " ", v)
    # استبدال الفواصل التقليدية بوقفات تنفسية طبيعية
    v = v.replace("،", "، ")
    return v.strip()

def humanize_text(v: str) -> str:
    """تحويل الجمل الفصحى الجامدة إلى لهجة دافئة وجذابة للأطفال."""
    patterns = {
        "هيا بنا": ["يلا بينا", "خلونا", "تعالوا"],
        "سنتعلم": ["هنتعلم", "هنعرف", "خلينا نعرف مع بعض"],
        "انظر": ["شوف", "بصّ كده", "تأمل الجمال ده"],
        "أحبائي": ["يا حبايبي", "يا أبطال", "يا نجوم قِيمة"],
    }
    for k, opts in patterns.items():
        if k in v:
            v = v.replace(k, random.choice(opts))
    return v

def sanitize_visual_prompt(v: str) -> str:
    """
    هندسة الأوامر البصرية (Prompt Engineering):
    ضمان نمط بصري (Art Style) ثابت وعالمي يشبه إنتاجات ديزني/استوديو جيبلي.
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
    if "vector" not in v and "illustration" not in v:
        v += master_style
    
    return re.sub(r",\s*,", ",", v).strip(", ")

def score_visual_prompt(v: str) -> float:
    score = 0.0
    if "vector" in v or "illustration" in v: score += 2
    if "ghibli" in v or "children" in v: score += 2
    if "pastel" in v or "warm" in v: score += 1
    if len(v.split()) < 35: score += 1
    if any(x in v for x in ["realistic", "3d", "photo"]): score -= 3
    return score

def adaptive_pacing(text: str, duration: float) -> str:
    """تنبيه إذا كان النص أطول من المدة لضمان عدم استعجال القارئ (ElevenLabs)."""
    words = len(text.split())
    max_words = int(duration * 2.3)  # ~2.3 wpm Arabic narration
    if words > max_words:
        logger.warning(f"⚠️ Pacing Alert: {words} words for {duration}s (max recommended {max_words})")
    return text

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
# SCENES
# ════════════════════════════════════════════════════════════════
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

    image_path: Optional[str] = None
    intro_audio: Optional[str] = None
    explain_audio: Optional[str] = None
    ayah_audio: Optional[str] = None

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

        self.overall_score = sum(scores) / max(len(scores), 1)
        return self

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
