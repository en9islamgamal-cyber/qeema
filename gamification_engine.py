"""
gamification_engine.py — VALUE / QEEMA v10.0 (Pass-through Bypass)
====================================================================
محرك التلعيب الآن مدمج بالكامل في video_engine عبر:
  - Logo ظاهر دائم (CSS overlay)
  - Progress bar في كل مشهد
  - Particles محبّبة للأطفال
  - Word-level animations
  
هذا الملف يتم تجاوزه (pass-through) لأن إضافة طبقة تلعيب إضافية
كانت تسبب الاحتشاد البصري والتشتت اللي شكوت منه.

⚠️ ملحوظة: الـ orchestrator يستدعي:
   self.gamify.apply_to_episode(branded_video, script, str(final_path))
"""
import logging
import shutil

logger = logging.getLogger(__name__)


class GamificationEngine:
    def __init__(self, font_path: str = None):
        pass

    def apply_to_episode(self, video_path: str, script=None, output_path: str = None) -> str:
        """Pass-through: التلعيب مدمج في video_engine."""
        logger.info("ℹ️ Pass-through: التلعيب مدمج بالفعل في video_engine")

        if not output_path:
            return video_path

        if video_path != output_path:
            try:
                shutil.copy(video_path, output_path)
            except Exception as e:
                logger.error(f"❌ نسخ فشل: {e}")
                return video_path

        return output_path
