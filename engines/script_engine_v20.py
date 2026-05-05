"""
engines/script_engine_v20.py — Multi-task script generator (NEW v20)
=========================================================================
Single-call episode generation. v17/v18/v19 used 6 calls per episode:
  1× meta (title/intro/outro/CTA)
  5× per-ayah (hook/intro/analogy/explain/moral/visual)

v20 collapses this to 1-2 calls using JSON-structured multi-task prompts.

[Cost reduction]
  Old: 6 × ~3000 tokens = 18,000 tokens/episode
  New: 1 × ~6000 tokens = 6,000 tokens/episode
  → 67% reduction in Gemini token usage

[Why a single call works]
  Gemini 2.5 Flash handles 1M context. Sending all 5 ayahs + meta in
  one JSON prompt produces consistent output AND lets the LLM reference
  earlier ayahs when generating later ones (better narrative cohesion).

[Fallback]
  If single-call fails (timeout/parse error), falls back to per-ayah calls
  via the original ScriptEngine.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Single-call multi-task prompt
# ════════════════════════════════════════════════════════════════
def build_full_episode_prompt(
    surah_name: str,
    surah_number: int,
    ayahs: List[Dict[str, Any]],  # [{"number": 1, "text": "..."}]
    hook_strategy_hint: str = "",
    analogy_domain_hint: str = "",
) -> str:
    """Build a single multi-task prompt that generates the full episode."""
    ayah_block = "\n".join([
        f"  آية {a['number']}: {a['text']}"
        for a in ayahs
    ])

    return f"""اكتب حلقة تعليمية كاملة عن سورة {surah_name} ({len(ayahs)} آيات).
أسلوب: TED-Ed للأطفال 6-12 سنة، insight-first، بدون شخصية وهمية.

[الآيات]:
{ayah_block}

[توجيهات]:
- استراتيجية الـ Hook: {hook_strategy_hint or 'سؤال علمي مذهل'}
- مجال المثال (analogy): {analogy_domain_hint or 'الطبيعة'}

أجب بـ JSON واحد كامل بهذا الشكل بالضبط:

{{
  "title": "عنوان عربي يثير الفضول (max 50 حرف)",
  "youtube_title": "عنوان يوتيوب SEO max 60 حرف",
  "youtube_description": "وصف 200-300 كلمة يبدأ بـhook + ملخص + #هاشتاجات",
  "youtube_tags": ["وسم1", "وسم2", "تفسير قرآن", "{surah_name}"],
  "intro_text": "افتتاحية max 30 كلمة، تبدأ بـhook قوي (سؤال/حقيقة)",
  "cta_text": "تذكير ودي بالاشتراك max 18 كلمة",
  "outro_text": "خاتمة فيها takeaway في جملتين + دعاء قصير، max 35 كلمة",
  "intro_visual": "Wide cinematic establishing shot in English",
  "outro_visual": "Peaceful contemplative scene in English",

  "ayah_scenes": [
    {{
      "ayah_number": 1,
      "hook_text": "خطاف max 25 كلمة، curiosity gap",
      "intro_text": "جسر للآية في جملتين max 25 كلمة",
      "analogy_text": "مثال من الواقع max 60 كلمة، بدون شخصيات وهمية",
      "explain_text": "الشرح في جملتين max 40 كلمة",
      "moral_text": "Takeaway قابل للحفظ max 20 كلمة",
      "scene_emotion": "warm/reverent/playful/peaceful/excited",
      "visual_subject": "Subject in English (e.g., 'galaxy with stars')",
      "visual_action": "Action in English (e.g., 'stars rotating slowly')",
      "visual_environment": "Environment in English (e.g., 'deep space')",
      "visual_scene_hint": "golden_field/garden/sky/mosque/ocean/starry_night/etc"
    }}
    // ... كرر لكل آية بنفس البنية
  ]
}}

[قواعد صارمة]:
- مصري معاصر بسيط: إيه، إزاي، عايز، فين، كده
- ممنوع: شخصيات وهمية، 'يا أحبائي'، 'كان يا ما كان'، 'جدو'
- visual_* بالإنجليزي فقط، بدون شخصيات بشرية واضحة
- النصوص العربية بدون تشكيل
- JSON صالح فقط، مفيش markdown، مفيش شرح خارج JSON

[تحقق قبل ما تجاوب]:
✓ 5 ayah_scenes (واحد لكل آية)
✓ كل scene فيه كل الحقول المطلوبة
✓ مفيش scenes مكررة في الـ analogies
"""


def parse_full_episode_response(
    data: Dict[str, Any],
    ayahs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Parse single-call response into the structure orchestrator expects.

    Returns dict matching v18 EpisodeScript shape with:
        - title, youtube_title, youtube_description, youtube_tags
        - cta_text, intro_text, outro_text, intro_visual, outro_visual
        - ayah_scenes: list of dicts (one per ayah)

    Raises ValueError if response missing required fields.
    """
    required_meta = [
        "title", "youtube_title", "youtube_description",
        "intro_text", "outro_text", "intro_visual", "outro_visual",
    ]
    for field in required_meta:
        if not data.get(field):
            raise ValueError(f"Missing required meta field: {field}")

    if "ayah_scenes" not in data or not isinstance(data["ayah_scenes"], list):
        raise ValueError("Missing or invalid ayah_scenes array")

    if len(data["ayah_scenes"]) != len(ayahs):
        raise ValueError(
            f"Expected {len(ayahs)} ayah_scenes, got {len(data['ayah_scenes'])}"
        )

    # Validate each scene
    required_scene_fields = [
        "hook_text", "intro_text", "analogy_text",
        "explain_text", "moral_text",
    ]
    for i, scene in enumerate(data["ayah_scenes"]):
        for f in required_scene_fields:
            if not scene.get(f):
                raise ValueError(f"Ayah {i+1} missing field: {f}")

    return data


def build_visual_prompt_from_scene(scene_data: Dict[str, Any]) -> str:
    """Combine subject + action + environment into a single prompt string.

    Returns a text the orchestrator can pass to LeonardoImageEngine.generate()
    (which then runs it through VisualPromptEngineer for locked style).
    """
    subject = scene_data.get("visual_subject", "abstract symbolic scene")
    action = scene_data.get("visual_action", "")
    environment = scene_data.get("visual_environment", "")

    parts = [subject]
    if action:
        parts.append(action)
    if environment:
        parts.append(f"in {environment}")
    return ", ".join(parts)
