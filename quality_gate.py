import logging
from typing import Tuple, List

logger = logging.getLogger(__name__)

class QualityReport:
    def __init__(self, passed: bool, score: float, critiques: List[str]):
        self.passed = passed
        self.score = score
        self.critiques = critiques # رسائل النقد لإصلاح المخرج

class QualityGate:
    """بوابة جودة لغوية وهيكلية صارمة"""
    def __init__(self, min_intro_len=20, min_explain_len=30):
        self.min_intro_len = min_intro_len
        self.min_explain_len = min_explain_len
        
        # كلمات محظورة (تدل على أن الذكاء الاصطناعي يتحدث عن نفسه أو يضع نصوصاً غير مرغوبة)
        self.forbidden_phrases = [
            "أنا نموذج لغوي", "كذكاء اصطناعي", "بِسْمِ اللَّهِ", "قُلْ هُوَ"
        ]

    def evaluate(self, script_data: dict) -> QualityReport:
        critiques = []
        score = 100.0
        
        # 1. فحص الهيكل الأساسي
        if "ayah_scenes" not in script_data or not script_data["ayah_scenes"]:
            return QualityReport(False, 0.0, ["الهيكل مفقود: لا يوجد ayah_scenes."])

        # 2. فحص جودة المشاهد
        for i, scene in enumerate(script_data.get("ayah_scenes", [])):
            intro = str(scene.get("intro_text", "")).strip()
            explain = str(scene.get("explain_text", "")).strip()
            
            # فحص الطول (لمنع الإجابات الكسولة)
            if len(intro) < self.min_intro_len:
                critiques.append(f"مشهد الآية {i+1}: intro_text قصير جداً ({len(intro)} حرف).")
                score -= 20
                
            if len(explain) < self.min_explain_len:
                critiques.append(f"مشهد الآية {i+1}: explain_text قصير جداً ({len(explain)} حرف).")
                score -= 20
                
            # فحص الكلمات المحظورة
            combined_text = intro + " " + explain
            for phrase in self.forbidden_phrases:
                if phrase in combined_text:
                    critiques.append(f"مشهد الآية {i+1}: يحتوي على عبارة محظورة ('{phrase}').")
                    score -= 50

        # يعتبر التقييم ناجحاً إذا تجاوز 80% ولم تكن هناك أخطاء هيكلية قاتلة
        passed = score >= 80.0 and len(critiques) <= 1
        
        if not passed:
            logger.warning(f"❌ فشل في بوابة الجودة. التقييم: {score}%. النقد: {critiques}")
        else:
            logger.info(f"✅ اجتاز بوابة الجودة بتقييم {score}%")
            
        return QualityReport(passed, max(0.0, score), critiques)