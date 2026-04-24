"""
orchestrator.py — QEEMA v5.2 (Optimized for Free Tiers & High Fidelity)
Refactor focus: Quota Protection, Cache Validation, and Human-like Sequencing.
"""

import time
import random
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any

from models import EpisodeScript, EpisodeStatus
from config import DBConfig, Paths

# إعداد الـ Logger ليكون أكثر وضوحاً في مراقبة الميزانية
import logging
logger = logging.getLogger("QEEMA.Orchestrator")

class PipelineOrchestrator:
    def __init__(self):
        # ... (نفس التعريفات السابقة للمحركات و Supabase) ...
        self.MIN_API_GAP = 3.0  # الحد الأدنى للثواني بين طلبات الـ API
        self.MAX_API_GAP = 7.0  # الحد الأقصى لمحاكاة التفكير البشري

    def _human_delay(self):
        """تأخير عشوائي لحماية الكوتة ومحاكاة النشاط البشري"""
        delay = random.uniform(self.MIN_API_GAP, self.MAX_API_GAP)
        logger.info(f"⏳ الانتظار لمدة {delay:.2f} ثانية (حماية الكوتة)...")
        time.sleep(delay)

    def _get_content_hash(self, content: str) -> str:
        """إنشاء بصمة فريدة للمحتوى لتجنب إعادة توليد نفس الشيء"""
        return hashlib.md5(content.encode()).hexdigest()

    # ──────────────────────────── SCRIPTING (The Brain) ────────────────────────────

    def _stage_script(self, ep_num: int) -> EpisodeScript:
        logger.info("📝 [المرحلة 1]: توليد السكريبت بذكاء...")
        
        # محاولة الاسترجاع من القرص أولاً
        cached = self.script.load_from_disk(ep_num)
        if cached:
            logger.info("♻️ تم العثور على سكريبت جاهز. توفير كوتة الـ LLM!")
            return cached

        script = self.script.generate(ep_num)
        
        # لمسة بشرية: مراجعة السكريبت تلقائياً (Self-Refinement)
        # نقوم بذلك فقط إذا كان السكريبت يحتاج فعلياً لتحسين (توفير للطلبات)
        script = self._stage_script_repair(script, ep_num)
        
        self._save_script_state(script)
        return script

    # ──────────────────────────── AUDIO (Quota-Safe) ────────────────────────────

    def _stage_audio(self, script: EpisodeScript, ep_dir: str) -> Dict[str, str]:
        logger.info("🎙️ [المرحلة 2]: توليد الصوت (نظام التوفير)...")
        audio_map_file = Path(ep_dir) / "audio_map.json"
        
        # استئناف ذكي جداً
        if audio_map_file.exists():
            audio_map = json.loads(audio_map_file.read_text(encoding="utf-8"))
            # نتحقق أن كل ملف موجود فعلياً وحجمه أكبر من 0
            if all(Path(p).exists() and Path(p).stat().st_size > 0 for p in audio_map.values()):
                logger.info("✅ جميع ملفات الصوت موجودة. لن يتم استهلاك أي طلبات API.")
                self._update_script_audio_paths(script, audio_map)
                return audio_map

        # التوليد المتسلسل مع فواصل (تجنب الـ Rate Limit)
        # يتم استدعاء المحرك ليقوم بالتوليد جملة بجملة مع انتظار بين كل جملة
        audio_map = self.voice.generate_episode_audio_sequential(script, ep_dir, delay_fn=self._human_delay)
        
        # معالجة المؤثرات (تتم محلياً، لا تستهلك كوتة)
        processed = self.sfx.process_all(audio_map, script, ep_dir)
        self._update_script_audio_paths(script, processed)
        
        # حفظ الحالة
        audio_map_file.write_text(json.dumps(processed, ensure_ascii=False), encoding="utf-8")
        self._db_save_state(script.episode_id, "audio", processed)
        
        return processed

    # ──────────────────────────── VISUALS (Strategic Rendering) ────────────────────

    def _stage_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        logger.info("🎨 [المرحلة 3]: توليد الصور (انتقائي)...")
        
        # التحقق من وجود "Visual State" في قاعدة البيانات لتجنب إعادة طلب الصور المكلفة
        state = self._db_load_state(script.episode_id, "visuals")
        if state:
            # التأكد من صحة المسارات محلياً
            if all(Path(p).exists() for p in state.values()):
                logger.info("♻️ استعادة الصور من الحالة السابقة. توفير كبير في الميزانية!")
                for k, p in state.items():
                    self._set_scene_image(script, k, p)
                return

        # توليد متسلسل مع "Human Delay"
        # محرك الصور الآن سيقوم بطلب صورة والانتظار قبل طلب التالية
        self.visual.generate_episode_visuals_sequential(script, ep_dir, delay_fn=self._human_delay)
        
        # حفظ الحالة فوراً بعد الانتهاء
        vis_map = self._extract_vis_map(script)
        self._db_save_state(script.episode_id, "visuals", vis_map)

    # ──────────────────────────── MAIN RUN ──────────────────────────────────────────

    def run(self, episode_number: Optional[int] = None):
        """
        تشغيل المنظومة بأسلوب "السلحفاة الذكية": بطيء لكنه آمن وموفر.
        """
        try:
            # 1. تحديد الحلقة
            target = self._db_get_pending() if episode_number is None else {"episode_number": episode_number}
            if not target:
                logger.info("📭 لا توجد حلقات في قائمة الانتظار.")
                return

            ep_num = target["episode_number"]
            ep_id = target.get("id") or self._db_init_episode(ep_num)

            # تحديث الحالة إلى Processing
            self._db_update_episode(ep_id, status=EpisodeStatus.PROCESSING)
            
            ep_dir = Paths.TEMP_EPISODES / f"ep_{ep_num:03d}"
            ep_dir.mkdir(parents=True, exist_ok=True)

            # 2. تسلسل المراحل (Strict Sequential)
            script = self._stage_script(ep_num)
            script.episode_id = ep_id
            
            self._stage_audio(script, str(ep_dir))
            self._stage_visuals(script, str(ep_dir))
            
            raw_video = self._stage_video(script, str(ep_dir))
            final_video = self._stage_gamification(script, raw_video)
            
            thumb = self._stage_thumbnail(script, str(ep_dir))
            
            # 3. الرفع (اختياري وبحذر)
            video_id = self._stage_upload(script, final_video, thumb)

            # 4. النجاح النهائي
            self._db_update_episode(
                ep_id, 
                status=EpisodeStatus.COMPLETED, 
                video_id=video_id,
                video_path=final_video
            )
            logger.info(f"🎉 تم إنتاج الحلقة {ep_num} بنجاح وبأقل تكلفة!")

        except Exception as e:
            logger.error(f"🚨 فشل في خط الإنتاج: {traceback.format_exc()}")
            if 'ep_id' in locals():
                self._db_update_episode(ep_id, status=EpisodeStatus.FAILED, error=str(e))
