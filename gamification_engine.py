"""
gamification_engine.py — VALUE / QEEMA v9.1 (Architectural Bypass + Pipeline-Compat)
====================================================================================
تم اتخاذ قرار معماري (Architectural Decision) بتجاوز هذا المحرك.
السبب: محرك الفيديو السينمائي (video_engine.py) أصبح يتكفل بإضافة
الشعار، شريط التقدم، والنصوص المتوهجة بشكل مدمج عبر (HTML/CSS Render).
تطبيق هذا المحرك مرة أخرى سيؤدي إلى ازدواجية الطبقات وانخفاض جودة الفيديو.

v9.1: تم تعديل الـ signature ليطابق ما يستدعيه الـ orchestrator:
   self.gamify.apply_to_episode(branded_video, script, str(final_path))
"""
import logging
import shutil

logger = logging.getLogger(__name__)


class GamificationEngine:
    def __init__(self, font_path: str = None):
        # لم يعد الخط مطلوباً هنا (الـ video_engine يتكفل بكل التصميم البصري)
        pass

    def apply_to_episode(self, video_path: str, script=None, output_path: str = None) -> str:
        """
        ✅ Signature متوافق مع orchestrator.py الذي يستدعي:
           self.gamify.apply_to_episode(branded_video, script, str(final_path))

        المعاملات:
            video_path:  الفيديو القادم من intro_outro_engine
            script:      الـ EpisodeScript (غير مستخدم حالياً — passthrough)
            output_path: المسار النهائي

        السلوك:
            يمرر الفيديو كما هو دون أي تأثيرات (التلعيب مدمج في video_engine).
        """
        logger.info("ℹ️ Architectural Bypass: تمرير الفيديو دون إضافة طبقات تلعيب (مدمجة مسبقاً).")

        # لو ما تم تمرير output_path، نرجع الفيديو الأصلي
        if not output_path:
            logger.warning("⚠️ output_path غير محدد، إرجاع المسار الأصلي.")
            return video_path

        # التأكد من عدم الكتابة فوق نفس الملف
        if video_path != output_path:
            try:
                shutil.copy(video_path, output_path)
            except Exception as e:
                logger.error(f"❌ فشل نسخ الفيديو في GamificationEngine: {e}")
                return video_path

        return output_path
