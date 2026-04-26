"""
gamification_engine.py — VALUE / QEEMA v8.0 (Architectural Bypass)
==================================================================
تم اتخاذ قرار معماري (Architectural Decision) بتجاوز هذا المحرك.
السبب: محرك الفيديو السينمائي (video_engine.py) أصبح يتكفل بإضافة 
الشعار، شريط التقدم، والنصوص المتوهجة بشكل مدمج عبر (HTML/CSS Render).
تطبيق هذا المحرك مرة أخرى سيؤدي إلى ازدواجية الطبقات وانخفاض جودة الفيديو.
يعمل هذا الملف حالياً كـ Pass-through لضمان استقرار خط الـ Orchestrator.
"""

import logging
import shutil

logger = logging.getLogger(__name__)

class GamificationEngine:
    def __init__(self, font_path: str = None):
        # لم يعد الخط مطلوباً هنا
        pass

    def apply_to_episode(self, video_path: str, output_path: str) -> str:
        """
        يمرر الفيديو القادم من VideoEngine كما هو دون أي تأثيرات إضافية.
        """
        logger.info("ℹ️ Architectural Bypass: تمرير الفيديو دون إضافة طبقات تلعيب (تم دمجها مسبقاً).")
        
        # التأكد من عدم الكتابة فوق نفس الملف إذا كان المسار مختلفاً
        if video_path != output_path:
            try:
                shutil.copy(video_path, output_path)
            except Exception as e:
                logger.error(f"❌ فشل تمرير الفيديو في GamificationEngine: {e}")
                return video_path
                
        return output_path