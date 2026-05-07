"""
engines/script_engine_v20.py — VALUE / QEEMA v22.5
=========================================================================
Multi-task script prompt builder with production-quality constraints.

[v22.1 quality upgrades]
  ✓ Banned phrases enforcement (no "أحبائي" clichés)
  ✓ Egyptian Arabic dialect with concrete examples
  ✓ Concrete sensory detail requirement
  ✓ Story arc structure (setup → twist → resolution)
  ✓ Show-don't-tell explicit instruction
  ✓ Age-appropriate vocabulary cap (sentence length ≤ 12 words)
  ✓ Hook strategies as concrete examples, not vague hints
  ✓ Self-check checklist before final output
  ✓ Visual prompts get richer scene direction

[Why this prompt is long]
For LLMs, prompt detail directly correlates with output quality. A 200-word
prompt produces generic output; a 1500-word prompt with concrete examples
produces editorial-grade output. Token cost is fixed (input is cheap), but
quality compounds across 7 episodes/month.
"""
from __future__ import annotations

from typing import Any, Dict, List


# ════════════════════════════════════════════════════════════════
# Banned phrases — generic Arabic religious clichés
# ════════════════════════════════════════════════════════════════
BANNED_PHRASES = [
    "أحبائي", "يا أحبائي", "أحبتي", "إخوتي الكرام",
    "أبنائي الأعزاء", "أيها الأطفال الأعزاء",
    "هل تعلم", "هيا نتعلم", "تعالوا نتعلم",
    "في حلقة اليوم سنتحدث",
]


# ════════════════════════════════════════════════════════════════
# Hook strategies — CONCRETE examples (not hints)
# ════════════════════════════════════════════════════════════════
HOOK_EXAMPLES = {
    "amazing scientific fact": (
        'مثل: "في النملة الواحدة عضلات أكتر من جسمك كله. '
        'بس ربنا قال عنها كلمة واحدة في القرآن خلت العلماء '
        'يبصّوا لها بشكل تاني."'
    ),
    "common misconception": (
        'مثل: "كتير بيفتكروا إن الصبر يعني تستحمل وتسكت. '
        'بس القرآن بيقول لنا حاجة مختلفة تماماً."'
    ),
    "vivid metaphor from nature": (
        'مثل: "تخيل البحر لو ابتدى ينشف فجأة من غير ما تحسّ. '
        'القرآن بيوصف حاجة قريبة من ده عن قلب الإنسان."'
    ),
    "rhetorical question that reframes": (
        'مثل: "ليه ربنا قال \'فلينظر الإنسان إلى طعامه\' '
        'وما قالش \'ليأكل\'؟ النظرة دي فيها سرّ."'
    ),
    "contradiction that demands resolution": (
        'مثل: "ربنا في آية بيقول الحياة لعب ولهو، '
        'وفي آية تانية بيقول هي ميدان جدّي. '
        'الاتنين صح ازاي؟"'
    ),
    "personal everyday experience": (
        'مثل: "آخر مرة حسّيت بضيق وكان نفسك حد يفهمك. '
        'ربنا في الآية دي بيتكلم معاك انت بالذات."'
    ),
}


# ════════════════════════════════════════════════════════════════
# Analogy domains — concrete examples for kids
# ════════════════════════════════════════════════════════════════
ANALOGY_GUIDANCE = {
    "nature and animals": (
        "استخدم حيوان أو حشرة أو طائر معروف للأطفال. "
        "مثل: النحلة، الدلفين، النملة، الطائر المهاجر. "
        "اربط سلوكهم الحقيقي بالمعنى."
    ),
    "space and astronomy": (
        "استخدم الكواكب أو النجوم أو القمر. "
        "اربط بحقيقة فلكية حقيقية مذهلة."
    ),
    "human body and biology": (
        "استخدم جزء من الجسم (القلب، العين، المخ). "
        "اذكر إحصائية مذهلة عنه."
    ),
    "plants and seeds": (
        "البذرة تطلع شجرة، الجذور بتشتغل في الخفا، "
        "الورقة بتاكل النور."
    ),
    "water and ocean": (
        "البحر، الموجة، النهر، قطرة المطر، البخار. "
        "ركّز على قوّة الماء وتحوّلاته."
    ),
    "everyday objects": (
        "حاجة في البيت (الساعة، المفتاح، المرآة). "
        "خلّي الطفل يلاقيها في حياته."
    ),
    "weather and seasons": (
        "المطر، الرياح، الفصول. "
        "اربط بشعور الطفل في الفصل ده."
    ),
}


# ════════════════════════════════════════════════════════════════
# The actual prompt builder
# ════════════════════════════════════════════════════════════════
def build_full_episode_prompt(
    surah_name: str,
    surah_number: int,
    ayahs: List[Dict[str, Any]],
    hook_strategy_hint: str = "amazing scientific fact",
    analogy_domain_hint: str = "nature and animals",
) -> str:
    """Build a single multi-task prompt — production quality.

    Returns ~1400 word prompt that produces editorial-grade output.
    Single Gemini call replaces 6 legacy calls (83% token reduction).
    """
    ayah_block = "\n".join([
        f"  آية {a['number']}: {a['text']}"
        for a in ayahs
    ])

    hook_example = HOOK_EXAMPLES.get(
        hook_strategy_hint,
        HOOK_EXAMPLES["amazing scientific fact"],
    )

    analogy_guidance = ANALOGY_GUIDANCE.get(
        analogy_domain_hint,
        ANALOGY_GUIDANCE["nature and animals"],
    )

    banned_list = "، ".join(BANNED_PHRASES[:8])

    return f"""اكتب حلقة تعليمية كاملة عن سورة {surah_name} ({len(ayahs)} آيات).

═══════════════════════════════════════════════════
🎯 الجمهور: أطفال 6–12 سنة
🎬 الأسلوب: TED-Ed insight-first، بدون شخصيات وهمية
🗣️ اللهجة: عامية مصرية حديثة (ليست فصحى ولا جامدة)
═══════════════════════════════════════════════════

[الآيات]:
{ayah_block}

═══════════════════════════════════════════════════
📋 قواعد إلزامية — اللي يخالفها = إخفاق الحلقة:
═══════════════════════════════════════════════════

1. **اللغة** — عامية مصرية أصيلة:
   ✓ صح: "ربنا قال لينا في الآية دي حاجة جميلة"
   ✓ صح: "تخيل لو..."، "يعني ايه؟"، "ايه اللي بيخلّي..."
   ✗ غلط: "إن الله تعالى يخبرنا"، "أيها الإخوة"

2. **عبارات ممنوعة تماماً** (clichés تمسح أصالة المحتوى):
   {banned_list}
   ↳ ما تستخدمش أيًا منها في أي مكان.

3. **طول الجملة** — حد أقصى 12 كلمة لكل جملة.
   جملة طويلة = طفل بيفقد التركيز.
   ✗ غلط: "الإنسان مخلوق ضعيف يحتاج إلى ربه في كل لحظة من حياته اليومية..."
   ✓ صح: "الإنسان ضعيف. محتاج ربنا في كل لحظة. ده مش عيب فيه."

4. **Show, Don't Tell** — وصف ملموس بدلاً من تجريد:
   ✗ غلط: "الصبر مهم وفيه أجر كبير"
   ✓ صح: "تخيل بذرة في الأرض ساكتة 30 يوم من غير ما تتحرك.
            بس فيها بتحضّر نفسها لتطلع شجرة عمرها 100 سنة.
            ده الصبر."

5. **Concrete Sensory Detail** — حواس + أرقام + أمثلة محددة:
   ✗ غلط: "السماء جميلة"
   ✓ صح: "السماء فيها 100 مليار نجم، كل واحد فيهم زي شمسنا أو أكبر"

6. **Story Arc** — كل scene لازم يكون فيه:
   - Setup: حقيقة مدهشة أو سؤال
   - Twist: المفاجأة أو إعادة التأطير
   - Resolution: الربط بالآية وتطبيق عملي

7. **عدم الهلوسة في الحقائق العلمية**:
   لو هتقول رقم أو حقيقة، خليها معروفة شعبياً.
   ✗ غلط: "البطيخة فيها 87 نوع فيتامين"
   ✓ صح: "النحلة تطير من 200 وردة في اليوم"

═══════════════════════════════════════════════════
🎤 استراتيجية الـ Hook المطلوبة:
═══════════════════════════════════════════════════
{hook_strategy_hint}

{hook_example}

═══════════════════════════════════════════════════
🌿 مجال الأمثلة (analogies):
═══════════════════════════════════════════════════
{analogy_domain_hint}

{analogy_guidance}

═══════════════════════════════════════════════════
🎬 توجيهات الـ visual prompts (English, للذكاء الاصطناعي):
═══════════════════════════════════════════════════
- Use 3 components: subject + action + environment
- Be specific: "ant carrying leaf 100x its weight on bark texture"
                ليس "an ant"
- Atmospheric: include time-of-day, weather, mood
- No human faces, no famous characters, no text in image
- NotebookLM-style watercolor + ink illustration on paper (auto-applied later)

═══════════════════════════════════════════════════
✅ Self-Check قبل الإجابة (افحصهم في عقلك):
═══════════════════════════════════════════════════
[ ] هل اللغة عامية مصرية حقيقية؟
[ ] هل تجنّبت كل العبارات الممنوعة فعلاً؟
[ ] هل كل جملة ≤ 12 كلمة؟
[ ] هل الـ hook فيه عنصر مفاجأة أو سؤال يستحق الإجابة؟
[ ] هل كل analogy ملموس وله تفصيلة حسية؟
[ ] هل الـ moral قابل للحفظ والتطبيق فوراً؟
[ ] هل الـ visual prompts فيها 3 components محددين؟

═══════════════════════════════════════════════════

أجب بـ JSON واحد فقط — بدون أي نص قبله أو بعده — بهذا الـ schema:

{{
  "title": "عنوان عربي يثير الفضول، max 50 حرف",
  "youtube_title": "عنوان يوتيوب SEO، max 60 حرف",
  "youtube_description": "وصف 200-300 كلمة يبدأ بـ hook + ملخص + #هاشتاجات",
  "youtube_tags": ["وسم1", "وسم2", "تفسير قرآن", "{surah_name}"],
  "intro_text": "افتتاحية max 30 كلمة، تبدأ بـ hook قوي (سؤال/حقيقة)",
  "cta_text": "تذكير بالاشتراك max 18 كلمة، طبيعي مش متكرّر",
  "outro_text": "خاتمة فيها takeaway في جملتين + دعاء قصير، max 35 كلمة",
  "intro_visual": "Wide cinematic establishing shot in English, 3 components",
  "outro_visual": "Peaceful contemplative scene in English, 3 components",

  "ayah_scenes": [
    {{
      "ayah_number": 1,
      "hook_text": "خطاف max 25 كلمة، curiosity gap واضح",
      "intro_text": "جسر للآية في جملتين max 25 كلمة",
      "analogy_text": "مثال ملموس max 60 كلمة، حسّي ومحدد",
      "explain_text": "الشرح في جملتين max 40 كلمة",
      "moral_text": "Takeaway قابل للحفظ والتطبيق max 20 كلمة",
      "scene_emotion": "warm",
      "visual_subject": "Specific subject in English",
      "visual_action": "Specific action in English",
      "visual_environment": "Specific environment in English",
      "visual_scene_hint": "golden_field"
    }}
  ]
}}

scene_emotion يكون واحد من: warm, reverent, playful, peaceful, excited
visual_scene_hint يكون واحد من: golden_field, garden, sky, mosque, ocean, starry_night, abstract_warm, abstract_cool

ابدأ بـ {{ مباشرة. لا مقدمات. لا markdown."""


# ════════════════════════════════════════════════════════════════
# Response parser — validates schema + applies post-checks
# ════════════════════════════════════════════════════════════════
def parse_full_episode_response(
    data: Dict[str, Any],
    expected_ayahs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate JSON response shape + run quality checks."""
    required = [
        "title", "youtube_title", "youtube_description",
        "intro_text", "outro_text", "ayah_scenes",
    ]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    scenes = data["ayah_scenes"]
    if not isinstance(scenes, list):
        raise ValueError("ayah_scenes must be a list")

    if len(scenes) != len(expected_ayahs):
        raise ValueError(
            f"Expected {len(expected_ayahs)} ayah scenes, got {len(scenes)}"
        )

    scene_required = [
        "hook_text", "explain_text", "moral_text", "scene_emotion",
    ]
    valid_emotions = {"warm", "reverent", "playful", "peaceful", "excited"}

    for i, scene in enumerate(scenes):
        scene_missing = [k for k in scene_required if k not in scene]
        if scene_missing:
            raise ValueError(
                f"Scene {i+1} missing fields: {scene_missing}"
            )

        emotion = scene.get("scene_emotion", "warm").lower().strip()
        if emotion not in valid_emotions:
            scene["scene_emotion"] = "warm"
        else:
            scene["scene_emotion"] = emotion

    _quality_check(data)
    return data


def _quality_check(data: Dict[str, Any]) -> None:
    """Run soft quality checks. Logs warnings but doesn't fail."""
    import logging
    logger = logging.getLogger(__name__)

    text_fields = [
        data.get("intro_text", ""),
        data.get("outro_text", ""),
        data.get("cta_text", ""),
    ]
    for scene in data.get("ayah_scenes", []):
        text_fields.extend([
            scene.get("hook_text", ""),
            scene.get("intro_text", ""),
            scene.get("analogy_text", ""),
            scene.get("explain_text", ""),
            scene.get("moral_text", ""),
        ])

    all_text = " ".join(text_fields)
    for phrase in BANNED_PHRASES[:8]:
        if phrase in all_text:
            logger.warning(
                f"⚠️ Quality: banned phrase '{phrase}' found in script"
            )

    long_sentences = 0
    for text in text_fields:
        for sentence in text.split("."):
            sentence = sentence.strip()
            if len(sentence.split()) > 14:
                long_sentences += 1
    if long_sentences > 3:
        logger.warning(
            f"⚠️ Quality: {long_sentences} sentences exceed 12-word limit"
        )


def build_visual_prompt_from_scene(
    scene_data: Dict[str, Any],
    *,
    use_depth_of_field: bool = True,
) -> str:
    """Build a rich Leonardo prompt from scene dict's visual_* fields."""
    subject = scene_data.get("visual_subject", "abstract symbolic scene")
    action = scene_data.get("visual_action", "")
    environment = scene_data.get("visual_environment", "")

    parts = [subject]
    if action:
        parts.append(action)
    if environment:
        parts.append(f"in {environment}")

    if use_depth_of_field:
        parts.extend([
            "shallow depth of field",
            "atmospheric perspective",
            "layered foreground midground background composition",
        ])

    return ", ".join(parts)
