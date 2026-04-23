"""
quality_gate.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
بوابة الجودة الشاملة (Quality Assurance Director)
تراقب: الإيقاع البصري، الهوية الصوتية (المصرية)، والأسلوب الإنفوجرافيك.
"""

import logging
import re
from typing import List

logger = logging.getLogger(__name__)

class QualityReport:
    def __init__(self, passed: bool, score: float, critiques: List[str]):
        self.passed = passed
        self.score = score
        self.critiques = critiques # رسائل النقد التي تُرسل للموديل ليتعلم ويصلح نفسه

class QualityGate:
    """بوابة جودة لغوية، بصرية، وهيكلية صارمة جداً"""
    def __init__(self, min_words=5, max_words=25):
        # الاعتماد على عدد الكلمات لضبط الإيقاع (Pacing)
        self.min_words = min_words
        self.max_words = max_words

        # 1. فخاخ الذكاء الاصطناعي (AI Hallucinations)
        self.forbidden_llm_phrases = [
            "أنا نموذج لغوي", "كذكاء اصطناعي", "بِسْمِ اللَّهِ", "قُلْ هُوَ",
            "آسف", "لا يمكنني", "بالتأكيد", "إليك الـ JSON"
        ]

        # 2. فلتر اللهجة (الحفاظ على هوية الجد المصري ومنع اللهجات الدخيلة)
        self.forbidden_dialects = [
            "هيك", "بدي", "شلون", "شو", "زلمة", "وايد", "أبي", "بركي", "هلا والله"
        ]

        # 3. فلتر الستايل البصري (منع الـ 3D وإجبار الإنفوجرافيك)
        self.forbidden_visuals = [
            "3d", "pixar", "realistic", "photo", "cinema", "render", "octane", "photography"
        ]

    def _count_words(self, text: str) -> int:
        return len(str(text).split())

    def _check_text_segment(self, text: str, segment_name: str, critiques: List[str]) -> float:
        """يفحص مقطع نصي ويعيد قيمة الخصم من التقييم"""
        penalty = 0.0
        words = self._count_words(text)

        # فحص الملل البصري (الإيقاع)
        if words < self.min_words:
            critiques.append(f"[{segment_name}]: قصير جداً ({words} كلمات). يجب أن يكون أعمق قصصياً.")
            penalty += 15
        elif words > self.max_words + 10: # سماحية 10 كلمات إضافية كحد أقصى
            critiques.append(f"[{segment_name}]: طويل جداً ({words} كلمات). هذا يسبب مللاً بصرياً! قسّمه أو اختصره ليكون أقل من {self.max_words} كلمة.")
            penalty += 30

        # فحص الهوية والذكاء الاصطناعي
        for phrase in self.forbidden_llm_phrases:
            if phrase in text:
                critiques.append(f"[{segment_name}]: يحتوي على لغة روبوتية أو غير مقبولة ('{phrase}').")
                penalty += 50
                
        for word in self.forbidden_dialects:
            if f" {word} " in f" {text} ":
                critiques.append(f"[{segment_name}]: انحراف عن اللهجة المصرية المطلوبة. احذف كلمة ('{word}').")
                penalty += 40

        # فحص التشكيل المفرط (حماية الـ TTS)
        diacritics_count = len(re.findall(r'[\u064B-\u0652]', text))
        if diacritics_count > words * 1.5:
            critiques.append(f"[{segment_name}]: يحتوي على تشكيل مبالغ فيه سيفسد النطق الآلي. قم بإزالة التشكيل قدر الإمكان.")
            penalty += 20

        return penalty

    def _check_visual_segment(self, visual_prompt: str, segment_name: str, critiques: List[str]) -> float:
        """يفحص الوصف البصري لضمان جودة ليوناردو"""
        penalty = 0.0
        v_lower = str(visual_prompt).lower()
        
        for word in self.forbidden_visuals:
            if word in v_lower:
                critiques.append(f"[{segment_name} Visual]: استخدمت ستايل ممنوع ('{word}'). استبدله بـ 'flat vector graphic, infographic'.")
                penalty += 40
                
        if "flat" not in v_lower and "vector" not in v_lower:
            critiques.append(f"[{segment_name} Visual]: الوصف البصري يفتقر إلى كلمات التوجيه الأساسية. أضف 'flat vector graphic'.")
            penalty += 25
            
        return penalty

    def evaluate(self, script_data: dict) -> QualityReport:
        critiques = []
        score = 100.0

        if not isinstance(script_data, dict):
            return QualityReport(False, 0.0, ["المخرج ليس بصيغة JSON صحيحة (يجب أن يكون Object)."])

        # 1. فحص المشاهد الأساسية (الراوي والآيات)
        if "ayah_scenes" not in script_data or not script_data["ayah_scenes"]:
            critiques.append("الهيكل مفقود: لا يوجد ayah_scenes.")
            score -= 100

        # فحص مقدمة وخاتمة الحلقة
        for scene_key in ["intro_scene", "outro_scene"]:
            scene = script_data.get(scene_key, {})
            score -= self._check_text_segment(scene.get("narrator_text", ""), scene_key, critiques)
            score -= self._check_visual_segment(scene.get("visual_prompt", ""), scene_key, critiques)

        # 2. فحص مشاهد الآيات بدقة
        for i, scene in enumerate(script_data.get("ayah_scenes", [])):
            name_intro = f"مشهد الآية {i+1} (التمهيد)"
            name_explain = f"مشهد الآية {i+1} (الشرح)"
            name_visual = f"مشهد الآية {i+1}"

            score -= self._check_text_segment(scene.get("intro_text", ""), name_intro, critiques)
            score -= self._check_text_segment(scene.get("explain_text", ""), name_explain, critiques)
            score -= self._check_visual_segment(scene.get("visual_prompt", ""), name_visual, critiques)

        # درجة النجاح أصبحت 85% كمعيار Enterprise
        passed = score >= 85.0 and len(critiques) <= 1

        if not passed:
            logger.warning(f"❌ فشل في بوابة الجودة. التقييم: {score}%. يتم توجيه الموديل للإصلاح...")
        else:
            logger.info(f"✅ اجتاز بوابة الجودة بتفوق (التقييم: {score}%)")

        return QualityReport(passed, max(0.0, score), critiques)
