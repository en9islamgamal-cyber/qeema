"""
core/models.py
====================================================================
Pydantic schemas for QEEMA v2 — the 2-prompt simplified pipeline.

Architecture flow
-----------------
    Prompt 1 (Tafsir / Sheikh)  →  EpisodeNarration
    Prompt 2 (Hook + Visuals)   →  EpisodeHookAndVisuals

Both are combined into an `EpisodeBundle`, which is persisted to disk
and contains everything Phase 2 (assets) and Phase 3 (render) need.

Video timeline that this schema supports
----------------------------------------
   [0:00]  Hook narration + hook_visual
   [0:20]  Intro narration + intro_visual
   [0:35]  First recitation (full tilawah from CDN) + intro_visual
   [Y:YY]  Full explanation (all ayah narrations concatenated)
           Images: ayah_1_visual → ayah_2_visual → ... (crossfade)
   [Z:ZZ]  Transition narration ("تعالوا نسمعها تاني...")
           Second recitation (same tilawah) + ayah_visuals loop
   [W:WW]  Outro narration + outro_visual

Schema design notes
-------------------
1.  Gemini-friendly types only — no tuple (uses prefixItems which some
    Gemini versions handle poorly). Use explicit start/end fields.

2.  `extra="ignore"` on all Gemini-output models — accept extra fields
    silently instead of crashing the pipeline.

3.  All Arabic in narration/title/intro/outro/transition/youtube_*.
    All English in visual_*, full_prompt, hook visual concepts.

4.  Field descriptions are written for the LLM (they appear in the
    generated JSON Schema and steer Gemini's output).
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ════════════════════════════════════════════════════════════════════
# Re-usable Literal types
# ════════════════════════════════════════════════════════════════════

Reciter = Literal["husary", "alafasy"]
VisualStyle = Literal["watercolor", "soft_cartoon", "realistic"]
VisualPurpose = Literal["hook", "intro", "ayah", "outro", "thumbnail"]


# ════════════════════════════════════════════════════════════════════
# STAGE 1 OUTPUT  —  produced by Prompt 1 (Sheikh / Tafsir)
# ════════════════════════════════════════════════════════════════════

class AyahNarration(BaseModel):
    """
    The Egyptian-Arabic narration for a single ayah.

    Important: `narration` does NOT contain the Arabic text of the ayah
    itself — the Quranic verse is delivered separately as authentic
    recitation (الحصري) from `everyayah.com`. The narration is the
    Sheikh's *explanation* spoken by ElevenLabs.

    Per the agreed timeline, this narration plays in the middle of the
    video, between the two recitations.
    """
    model_config = ConfigDict(extra="ignore")

    ayah_number: int = Field(
        ge=1, le=286,
        description="رقم الآية في السورة",
    )
    ayah_text: str = Field(
        min_length=1,
        description=(
            "نص الآية الكامل من المصحف. للمرجع فقط — لن يُقرأ بـ AI، "
            "تُلاوة الآية ستُجلب من قارئ معتمد (الحصري). "
            "قد تكون من حرف واحد (مثل 'ن') إلى عدة أسطر."
        ),
    )
    narration: str = Field(
        min_length=200,
        max_length=2200,
        description=(
            "شرح الآية بالعامية المصرية البسيطة للأطفال 6-10 سنين. "
            "150-250 كلمة. "
            "اكتب الشرح كحكواتي محبوب يتكلم: جمل قصيرة (max 12 كلمة)، "
            "نبرة دافئة، أمثلة محسوسة من الطبيعة أو الأسرة أو الحياة اليومية. "
            "لا تُدرج نص الآية نفسه (تُتلى منفصلة من قارئ معتمد). "
            "ابدأ بشكل طبيعي يكمل من شرح الآية السابقة، "
            "وانتهِ بشكل يمهّد للآية التالية (للحفاظ على تدفق الشرح)."
        ),
    )


class EpisodeNarration(BaseModel):
    """
    Output of Prompt 1: full episode script + YouTube metadata.
    Generated in one Gemini call.
    """
    model_config = ConfigDict(extra="ignore")

    # ── Core script ──────────────────────────────────────────────
    title: str = Field(
        min_length=8, max_length=80,
        description=(
            "عنوان جذّاب للحلقة بالعربية. max 60 حرف. "
            "يفضّل سؤال أو حقيقة لافتة، مش 'تفسير سورة كذا'."
        ),
    )
    intro: str = Field(
        min_length=60, max_length=400,
        description=(
            "مقدمة 25-40 كلمة بالعامية المصرية. "
            "تُقرأ بعد الـ hook في الفيديو، تمهّد للموضوع، "
            "وتقول للطفل: 'تعالى نسمع الآيات الأول، وبعدين نفهمها مع بعض'."
        ),
    )
    ayahs: List[AyahNarration] = Field(
        min_length=1,
        max_length=15,
        description="شرح كل آية على حدة، بترتيب الآيات",
    )
    transition_to_second_recitation: str = Field(
        min_length=20, max_length=300,
        description=(
            "جملة قصيرة بالعامية المصرية تربط بين نهاية الشرح "
            "وبداية التلاوة الثانية. تذكّر الطفل بأن الآيات ستُتلى "
            "مرة أخرى ليحفظها. "
            "مثال: 'فاكر الآيات اللي سمعناها في الأول؟ "
            "تعالوا نسمعها تاني بعد ما فهمناها كويس عشان ما ننساهاش'."
        ),
    )
    outro: str = Field(
        min_length=60, max_length=400,
        description=(
            "خاتمة 25-40 كلمة بالعامية المصرية. "
            "تأتي بعد التلاوة الثانية. "
            "فيها takeaway قصير + دعاء قصير + توديع."
        ),
    )

    # ── YouTube SEO ──────────────────────────────────────────────
    youtube_title: str = Field(
        min_length=8, max_length=100,
        description=(
            "عنوان يوتيوب SEO max 70 حرف. "
            "يحتوي اسم السورة + سؤال أو فضول. "
            "مثال: 'سورة الفلق - السر اللي بنطلبه قبل النوم 🌙'."
        ),
    )
    youtube_description: str = Field(
        min_length=100, max_length=2500,
        description=(
            "وصف يوتيوب 150-250 كلمة. "
            "أول سطرين: hook + اسم السورة. "
            "آخر سطرين: hashtags."
        ),
    )
    youtube_tags: List[str] = Field(
        default_factory=list,
        max_length=15,
        description="هاشتاجات يوتيوب، 5-10 وسوم بالعربي",
    )

    # ── Internal cross-check ─────────────────────────────────────
    @model_validator(mode="after")
    def _check_ayah_numbers_unique_and_ordered(self) -> "EpisodeNarration":
        seen = set()
        prev = 0
        for ay in self.ayahs:
            if ay.ayah_number in seen:
                raise ValueError(
                    f"Duplicate ayah_number {ay.ayah_number} in episode"
                )
            seen.add(ay.ayah_number)
            if ay.ayah_number <= prev:
                raise ValueError(
                    f"Ayahs must be in ascending order; "
                    f"saw {ay.ayah_number} after {prev}"
                )
            prev = ay.ayah_number
        return self


# ════════════════════════════════════════════════════════════════════
# STAGE 2 OUTPUT  —  produced by Prompt 2 (Hook + ALL Visuals)
# ════════════════════════════════════════════════════════════════════

class VisualPrompt(BaseModel):
    """One Leonardo prompt for one image."""
    model_config = ConfigDict(extra="ignore")

    purpose: VisualPurpose = Field(
        description="الغرض من الصورة"
    )
    ayah_number: Optional[int] = Field(
        default=None,
        ge=1, le=286,
        description=(
            "رقم الآية لو purpose='ayah'، خلاف ذلك يُترك null. "
            "إلزامي مع purpose='ayah'، ممنوع مع الأغراض الأخرى."
        ),
    )
    subject: str = Field(
        min_length=4, max_length=200,
        description=(
            "الموضوع الرئيسي للصورة بالإنجليزية. 8-12 كلمة. "
            "محدد وملموس، لا تجريد. "
            "أمثلة: 'ancient olive tree with golden leaves', "
            "'silver fish swimming in clear mountain stream'."
        ),
    )
    action: str = Field(
        min_length=3, max_length=150,
        description=(
            "الفعل أو الحركة بالإنجليزية. 5-8 كلمات. "
            "حركة هادئة: leaves swaying, water flowing, light shifting."
        ),
    )
    environment: str = Field(
        min_length=4, max_length=200,
        description=(
            "البيئة بالإنجليزية. 6-10 كلمات. "
            "وقت اليوم + الطقس + المزاج."
        ),
    )
    color_palette: str = Field(
        min_length=4, max_length=150,
        description=(
            "Color palette description (English). 4-8 words. "
            "Examples: 'warm golden hour tones', "
            "'soft pastel blues and creams', 'deep twilight purples and embers'."
        ),
    )
    full_prompt: str = Field(
        min_length=40, max_length=1500,
        description=(
            "The complete Leonardo prompt: subject + action + environment "
            "+ color palette + style template (watercolor + ink on textured paper, "
            "warm earthy tones, dreamy atmosphere, child-friendly, no human faces, "
            "no text in image). "
            "Make it cinematic, atmospheric, suitable for Islamic children's content."
        ),
    )

    @model_validator(mode="after")
    def _check_ayah_number_consistency(self) -> "VisualPrompt":
        if self.purpose == "ayah" and self.ayah_number is None:
            raise ValueError(
                "purpose='ayah' requires ayah_number to be set"
            )
        if self.purpose != "ayah" and self.ayah_number is not None:
            raise ValueError(
                f"ayah_number must be null when purpose='{self.purpose}'"
            )
        return self


class EpisodeHookAndVisuals(BaseModel):
    """
    Output of Prompt 2: hook text + all visual prompts.

    This single Gemini call produces:
      - hook_text (Egyptian Arabic, ~20 seconds)
      - hook_visual (Leonardo prompt for the hook image)
      - intro_visual (Leonardo prompt for the intro/recitation image)
      - ayah_visuals (one per ayah)
      - outro_visual
      - thumbnail_visuals (3 variants for A/B testing)
    """
    model_config = ConfigDict(extra="ignore")

    hook_text: str = Field(
        min_length=60, max_length=600,
        description=(
            "نص الـ hook بالعامية المصرية. 40-80 كلمة. "
            "يلفت انتباه الطفل في أول 15-20 ثانية. "
            "يطرح سؤال مذهل أو حقيقة لافتة عن موضوع الآيات. "
            "ممنوع تماماً: 'يا أحبائي'، 'هل تعلم'، 'النهارده هنتكلم عن'. "
            "يُقرأ بصوت ElevenLabs بنبرة 'fascinated storyteller'."
        ),
    )

    hook_visual: VisualPrompt = Field(
        description="صورة الـ Hook — تظهر مع نص الـ Hook في بداية الفيديو"
    )

    intro_visual: VisualPrompt = Field(
        description=(
            "صورة الـ Intro — تظهر مع المقدمة والتلاوة الأولى. "
            "هادئة، تأملية، تدع الطفل يتركز على صوت التلاوة."
        )
    )

    ayah_visuals: List[VisualPrompt] = Field(
        min_length=1, max_length=15,
        description=(
            "صورة لكل آية، بترتيب الآيات. "
            "كل صورة تعبّر عن معنى الآية بصرياً."
        ),
    )

    outro_visual: VisualPrompt = Field(
        description="صورة الـ Outro — للخاتمة والدعاء، مزاج هادئ ودافئ"
    )

    thumbnail_visuals: List[VisualPrompt] = Field(
        min_length=3, max_length=3,
        description=(
            "Exactly 3 thumbnail variants for YouTube A/B testing. "
            "Each should have higher contrast and more dramatic composition "
            "than the in-video images. Still: no faces, no text in image."
        ),
    )

    @model_validator(mode="after")
    def _check_purposes(self) -> "EpisodeHookAndVisuals":
        if self.hook_visual.purpose != "hook":
            raise ValueError("hook_visual.purpose must be 'hook'")
        if self.intro_visual.purpose != "intro":
            raise ValueError("intro_visual.purpose must be 'intro'")
        if self.outro_visual.purpose != "outro":
            raise ValueError("outro_visual.purpose must be 'outro'")
        for i, v in enumerate(self.ayah_visuals):
            if v.purpose != "ayah":
                raise ValueError(
                    f"ayah_visuals[{i}].purpose must be 'ayah', got '{v.purpose}'"
                )
        for i, v in enumerate(self.thumbnail_visuals):
            if v.purpose != "thumbnail":
                raise ValueError(
                    f"thumbnail_visuals[{i}].purpose must be 'thumbnail', "
                    f"got '{v.purpose}'"
                )
        return self


# ════════════════════════════════════════════════════════════════════
# FINAL BUNDLE  —  persisted to disk, consumed by render stages
# ════════════════════════════════════════════════════════════════════

class EpisodeBundle(BaseModel):
    """
    The complete output of the script-generation pipeline.

    Persisted to: state/episodes/episode_NNN/bundle.json
    Consumed by:  asset generation (Leonardo, ElevenLabs) + video assembly
    """
    model_config = ConfigDict(extra="ignore")

    # ── Identity ─────────────────────────────────────────────────
    episode_number: int = Field(ge=1)
    surah_number: int = Field(ge=1, le=114)
    surah_name: str
    start_ayah: int = Field(ge=1)
    end_ayah: int = Field(ge=1)

    # ── Content (from the 2 Gemini calls) ────────────────────────
    narration: EpisodeNarration
    hook_and_visuals: EpisodeHookAndVisuals

    # ── Production parameters ────────────────────────────────────
    reciter: Reciter = "husary"
    visual_style: VisualStyle = "watercolor"

    # ── Provenance / observability ───────────────────────────────
    pipeline_version: str = "qeema_v2.0"
    generated_at_utc: str = Field(
        description="ISO-8601 UTC timestamp of generation"
    )
    gemini_calls_used: int = Field(
        ge=1,
        description="Number of successful Gemini calls (typically 2)"
    )
    gemini_keys_used: List[str] = Field(
        default_factory=list,
        description="Which keys served each prompt"
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> "EpisodeBundle":
        # ayah range sanity
        if self.end_ayah < self.start_ayah:
            raise ValueError(
                f"end_ayah ({self.end_ayah}) must be ≥ start_ayah "
                f"({self.start_ayah})"
            )
        expected_count = self.end_ayah - self.start_ayah + 1

        # narration ayahs match range
        narr_count = len(self.narration.ayahs)
        if narr_count != expected_count:
            raise ValueError(
                f"Expected {expected_count} ayahs in narration "
                f"({self.start_ayah}-{self.end_ayah}), got {narr_count}"
            )

        # visuals ayahs match
        vis_count = len(self.hook_and_visuals.ayah_visuals)
        if vis_count != expected_count:
            raise ValueError(
                f"Expected {expected_count} ayah_visuals, got {vis_count}"
            )

        # ayah numbers in narration match those in visuals
        narr_nums = [a.ayah_number for a in self.narration.ayahs]
        vis_nums = [
            v.ayah_number for v in self.hook_and_visuals.ayah_visuals
        ]
        if narr_nums != vis_nums:
            raise ValueError(
                f"Ayah numbers in narration {narr_nums} don't match "
                f"visuals {vis_nums}"
            )

        # the ayah numbers should match the requested range
        expected_nums = list(range(self.start_ayah, self.end_ayah + 1))
        if narr_nums != expected_nums:
            raise ValueError(
                f"Ayah numbers in narration {narr_nums} don't match "
                f"requested range {expected_nums}"
            )

        return self

    def ayah_count(self) -> int:
        return self.end_ayah - self.start_ayah + 1


# ════════════════════════════════════════════════════════════════════
# REQUEST  —  the orchestrator's input to the pipeline
# ════════════════════════════════════════════════════════════════════

class EpisodeRequest(BaseModel):
    """Input to ScriptOrchestrator.generate()."""
    model_config = ConfigDict(extra="forbid")

    episode_number: int = Field(ge=1, le=999)
    surah_number: int = Field(ge=1, le=114)
    surah_name: str = Field(min_length=2, max_length=30)
    start_ayah: int = Field(ge=1)
    end_ayah: int = Field(ge=1)

    reciter: Reciter = "husary"
    visual_style: VisualStyle = "watercolor"

    @model_validator(mode="after")
    def _check_range(self) -> "EpisodeRequest":
        if self.end_ayah < self.start_ayah:
            raise ValueError(
                f"end_ayah ({self.end_ayah}) must be ≥ start_ayah "
                f"({self.start_ayah})"
            )
        return self

    def ayah_count(self) -> int:
        return self.end_ayah - self.start_ayah + 1


# ════════════════════════════════════════════════════════════════════
# Verified ayah text — fetched from Quran.com, NEVER AI-generated
# ════════════════════════════════════════════════════════════════════

class VerifiedAyah(BaseModel):
    """A single ayah's verified Arabic text from a trusted source."""
    surah: int = Field(ge=1, le=114)
    number: int = Field(ge=1)
    text: str = Field(min_length=1)
    audio_url: Optional[str] = None  # everyayah.com URL
