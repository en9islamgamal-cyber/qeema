"""
orchestrator.py — QEEMA v3.0 (Enterprise Architecture)
قائد المنظومة الكاملة (The Master Controller)
• دمج تام لمحرك التلعيب (Gamification Integration)
• استئناف ذكي وموثوق 100% مع حفظ حالة السكريبت
• تتبع زمني لأداء المحركات (Performance Metrics)
• تنظيف آلي صارم للملفات المؤقتة لتوفير مساحة السيرفر
"""
from __future__ import annotations

import json
import logging
import shutil
import traceback
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

from supabase import create_client, Client

from config import APIKeys, DBConfig, Paths
from models import EpisodeScript, EpisodeStatus
from script_engine import ScriptEngine
from voice_engine_v2 import VoiceEngine
from visual_engine import VisualEngine
from sfx_engine import SFXEngine
from gamification_engine import GamificationEngine
from video_engine import VideoEngine
from thumbnail_engine import ThumbnailEngine
from quality_gate import QualityGate

logger = logging.getLogger(__name__)


@dataclass
class PipelineMetrics:
    """مقاييس أداء خط الإنتاج"""
    episode_number: int
    script_time: float = 0.0
    audio_time: float = 0.0
    visual_time: float = 0.0
    video_time: float = 0.0
    gamification_time: float = 0.0
    thumbnail_time: float = 0.0
    upload_time: float = 0.0
    total_time: float = 0.0
    failure: bool = False


class PipelineOrchestrator:
    """
    قائد المنظومة الكاملة: يتتبع كل مرحلة، ويعيد استئنافها عند الحاجة، ويحافظ على حالة كاملة.
    """

    def __init__(self):
        self.db: Optional[Client] = None
        self.quality_gate = QualityGate()
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    def _init_supabase(self) -> None:
        """الاتصال بـ Supabase والتحقق من صحته دون الحاجة إلى مصادقة مستخدم"""
        try:
            self.db = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
            # اختبار بسيط: قراءة جدول episodes (بدون مصادقة مسبقة)
            test = self.db.table(DBConfig.TABLE_EPISODES).select("count", count="exact").limit(0).execute()
            logger.info("✅ Supabase متصل ومستعد لتسجيل العمليات")
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Supabase: {e}")
            raise RuntimeError("لا يمكن الاتصال بقاعدة البيانات") from e

    def _init_engines(self) -> None:
        """تهيئة جميع المحركات الفرعية"""
        logger.info("🔧 تهيئة وإحماء المحركات (Engines Boot‑up)…")
        self.script = ScriptEngine()
        self.voice = VoiceEngine()
        self.visual = VisualEngine()
        self.sfx = SFXEngine()
        self.gamify = GamificationEngine()
        self.video = VideoEngine()
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ جميع المحركات في وضع الاستعداد الأقصى")

    # ──────────────────────────── SUPABASE / STATE MANAGEMENT ────────────────────────────

    def _db_get_episode(self, episode_number: int) -> Optional[Dict[str, Any]]:
        """جلب بيانات حلقة من قاعدة البيانات"""
        try:
            r = self.db.table(DBConfig.TABLE_EPISODES).select("*").eq("episode_number", episode_number).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"خطأ في جلب الحلقة {episode_number}: {e}")
            return None

    def _db_get_pending(self) -> Optional[Dict[str, Any]]:
        """جلب أول حلقة معلقة"""
        try:
            r = (
                self.db.table(DBConfig.TABLE_EPISODES)
                .select("*")
                .eq("status", "pending")
                .order("episode_number")
                .limit(1)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"خطأ في جلب الحلقة المعلقة: {e}")
            return None

    def _db_update_episode(self, ep_id: str, **fields) -> None:
        """تحديث بيانات حلقة"""
        upd = fields.copy()
        upd["updated_at"] = datetime.now(timezone.utc).isoformat()
        # تحويل القيم النصية إذا كانت من Enum
        if "status" in upd and hasattr(upd["status"], "value"):
            upd["status"] = upd["status"].value.lower()
        try:
            self.db.table(DBConfig.TABLE_EPISODES).update(upd).eq("id", ep_id).execute()
        except Exception as e:
            logger.warning(f"⚠️ Supabase update failed: {e}")

    def _db_init_episode(self, episode_number: int) -> str:
        """إنشاء سجل حلقة جديد إذا لم يوجد، وإرجاع معرفها"""
        existing = self._db_get_episode(episode_number)
        if existing:
            logger.debug("Episode %s already exists with status: %s", episode_number, existing["status"])
            return existing["id"]
        now = datetime.now(timezone.utc).isoformat()
        try:
            res = self.db.table(DBConfig.TABLE_EPISODES).insert(
                {
                    "episode_number": episode_number,
                    "status": "pending",
                    "created_at": now,
                    "updated_at": now,
                }
            ).execute()
            if not res.data:
                raise RuntimeError(f"Failed to create episode {episode_number} in DB")
            return res.data[0]["id"]
        except Exception as e:
            logger.error(f"فشل إنشاء سجل الحلقة {episode_number}: {e}")
            raise

    def _db_save_state(self, ep_id: str, stage: str, state: Dict[str, Any]) -> None:
        """حفظ حالة مرحلة معينة باستخدام upsert"""
        rec = {
            "episode_id": ep_id,
            "stage": stage,
            "state_data": json.dumps(state, ensure_ascii=False),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            # استخدام upsert مع تحديث إذا وجدت نفس (episode_id, stage)
            self.db.table("pipeline_state").upsert(rec, on_conflict="episode_id,stage").execute()
        except Exception as e:
            logger.warning(f"⚠️ فشل حفظ حالة المرحلة {stage}: {e}")

    def _db_load_state(self, ep_id: str, stage: str) -> Optional[Dict[str, Any]]:
        """استرجاع حالة مرحلة مخزنة"""
        try:
            r = (
                self.db.table("pipeline_state")
                .select("state_data")
                .eq("episode_id", ep_id)
                .eq("stage", stage)
                .order("updated_at", desc=True)
                .limit(1)
                .execute()
            )
            if r.data:
                logger.info("♻️ استرجاع حالة %s من Supabase", stage)
                return json.loads(r.data[0]["state_data"])
        except Exception as e:
            logger.warning(f"⚠️ فشل استرجاع حالة المرحلة {stage}: {e}")
        return None

    def _save_script_state(self, script: EpisodeScript) -> None:
        """حفظ السكريبت محلياً كملف JSON"""
        save_path = Paths.SCRIPT_DIR / f"episode_{script.episode_number:03d}.json"
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        logger.debug("💾 السكريبت محليًا: %s", save_path)

    # ──────────────────────────── SCRIPTING STAGE ────────────────────────────

    def _stage_script(self, ep_num: int) -> EpisodeScript:
        logger.info("📝 [المرحلة 1]: توليد السكريبت…")
        cached = self.script.load_from_disk(ep_num)
        if cached:
            logger.info("♻️ استئناف: تم العثور على سكريبت جاهز على القرص.")
            return cached
        script = self.script.generate(ep_num)
        self._save_script_state(script)
        return script

    def _stage_script_repair(self, script: EpisodeScript, ep_num: int) -> EpisodeScript:
        logger.info("🔧 [المرحلة 1.5]: إصلاح ذاتي للسكريبت بناءً على Quality Gate…")
        raw = script.model_dump()
        report = self.quality_gate.evaluate(raw)
        if report.passed:
            logger.info("✅ السكريبت ناجح في البوابة دون إصلاح.")
            return script
        logger.warning("⚠️ السكريبت يحتاج إصلاح ذاتي، التقييم: %.1f%%", report.overall_score)
        repaired = self.script.generate(ep_num)  # إعادة توليد
        self._save_script_state(repaired)
        return repaired

    # ──────────────────────────── AUDIO STAGE ────────────────────────────

    def _stage_audio(self, script: EpisodeScript, ep_dir: str) -> Dict[str, str]:
        logger.info("🎙️ [المرحلة 2]: هندسة الصوت والمؤثرات…")
        audio_map_file = Path(ep_dir) / "audio_map.json"
        if audio_map_file.exists():
            try:
                raw = json.loads(audio_map_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and all(Path(p).exists() for p in raw.values()):
                    logger.info("♻️ استئناف: خريطة الصوت مستعادة من القرص.")
                    self._update_script_audio_paths(script, raw)
                    return raw
                else:
                    logger.warning("بعض الملفات الصوتية مفقودة، إعادة التوليد")
            except Exception as e:
                logger.warning("audio_map.json غير صالح، إعادة التوليد: %s", e)
        audio_map = self.voice.generate_episode_audio(script, ep_dir)
        if not isinstance(audio_map, dict) or not audio_map:
            raise ValueError("مخرجات محرك الصوت غير متوافقة")
        audio_map_file.write_text(json.dumps(audio_map, ensure_ascii=False), encoding="utf-8")
        processed = self.sfx.process_all(audio_map, script, ep_dir)
        self._update_script_audio_paths(script, processed)
        self._save_script_state(script)
        self._db_save_state(script.episode_id, "audio", processed)
        return processed

    def _update_script_audio_paths(self, script: EpisodeScript, audio_map: Dict[str, str]) -> None:
        """تحديث مسارات الصوت في كائن السكريبت بناءً على الخريطة"""
        if "intro" in audio_map:
            script.intro_scene.audio_path = audio_map["intro"]
        if "outro" in audio_map:
            script.outro_scene.audio_path = audio_map["outro"]
        for sc in script.ayah_scenes:
            sid = f"ayah_{sc.scene_id}"
            if f"{sid}_intro" in audio_map:
                sc.intro_audio = audio_map[f"{sid}_intro"]
            if f"{sid}_explain" in audio_map:
                sc.explain_audio = audio_map[f"{sid}_explain"]   # تم تصحيح: explain_audio بدلاً من quran_audio
        for sc in script.mid_scenes:
            k = f"mid_{sc.scene_id}"
            if k in audio_map:
                sc.audio_path = audio_map[k]

    # ──────────────────────────── VISUAL STAGE ────────────────────────────

    def _stage_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        logger.info("🎨 [المرحلة 3]: توليد الإنفوجرافيك البصري…")
        vis_map_file = Path(ep_dir) / "visuals_map.json"
        if vis_map_file.exists():
            try:
                vis_map = json.loads(vis_map_file.read_text(encoding="utf-8"))
                # التحقق من وجود جميع الملفات
                if all(Path(p).exists() for p in vis_map.values()):
                    logger.info("♻️ استئناف: خريطة الصور موجودة وصالحة.")
                    self._set_scene_images(script, vis_map)
                    self._save_script_state(script)
                    self._db_save_state(script.episode_id, "visuals", vis_map)
                    return
                else:
                    logger.warning("بعض ملفات الصور مفقودة، سيتم إعادة توليدها.")
            except Exception as e:
                logger.warning("visuals_map.json غير صالح، إعادة توليد: %s", e)
        # توليد جديد
        self.visual.generate_episode_visuals(script, ep_dir)
        # بناء خريطة جديدة
        vis_map = {"intro": script.intro_scene.image_path, "outro": script.outro_scene.image_path}
        for sc in script.ayah_scenes:
            vis_map[f"ayah_{sc.scene_id}"] = sc.image_path
        for sc in script.mid_scenes:
            vis_map[f"mid_{sc.scene_id}"] = sc.image_path
        vis_map_file.write_text(json.dumps(vis_map, ensure_ascii=False), encoding="utf-8")
        self._save_script_state(script)
        self._db_save_state(script.episode_id, "visuals", vis_map)

    def _set_scene_images(self, script: EpisodeScript, vis_map: Dict[str, str]) -> None:
        """تطبيق مسارات الصور على المشاهد باستخدام خريطة واحدة"""
        if "intro" in vis_map:
            script.intro_scene.image_path = vis_map["intro"]
        if "outro" in vis_map:
            script.outro_scene.image_path = vis_map["outro"]
        # معالجة مشاهد الآيات
        for sc in script.ayah_scenes:
            key = f"ayah_{sc.scene_id}"
            if key in vis_map:
                sc.image_path = vis_map[key]
        # معالجة المشاهد الوسطية
        for sc in script.mid_scenes:
            key = f"mid_{sc.scene_id}"
            if key in vis_map:
                sc.image_path = vis_map[key]

    # ──────────────────────────── VIDEO STAGE ────────────────────────────

    def _stage_video(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info("🎬 [المرحلة 4]: تجميع ومونتاج الفيديو الخام…")
        raw_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
        if raw_path.exists():
            logger.info("♻️ استئناف: الفيديو الخام موجود مسبقًا.")
            return str(raw_path)
        raw_video_path = self.video.assemble_episode(script, ep_dir)
        self._db_save_state(script.episode_id, "video", {"raw_path": raw_video_path})
        return raw_video_path

    # ──────────────────────────── GAMIFICATION STAGE (4.5) ────────────────────────────

    def _stage_gamification(self, script: EpisodeScript, raw_video_path: str) -> str:
        logger.info("🎮 [المرحلة 4.5]: إضافة التلعيب (Gamification)…")
        gamified_path = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_final.mp4")
        if Path(gamified_path).exists():
            logger.info("♻️ استئناف: الفيديو النهائي (مُلعب) موجود مسبقًا.")
            return gamified_path
        final_path = self.gamify.apply_to_episode(raw_video_path, script, gamified_path)
        self._db_save_state(script.episode_id, "gamification", {"final_path": final_path})
        return final_path

    # ──────────────────────────── THUMBNAIL STAGE ────────────────────────────

    def _stage_thumbnail(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info("🖼️ [المرحلة 5]: تصميم الغلاف المصغر…")
        thumb_path = Paths.THUMBNAILS / f"ep_{script.episode_number:03d}.jpg"
        if thumb_path.exists():
            logger.info("♻️ استئناف: الغلاف موجود مسبقًا.")
            return str(thumb_path)
        scene_img = script.intro_scene.image_path
        generated = self.thumbnail.create(script, script.episode_number, scene_img)
        self._db_save_state(script.episode_id, "thumbnail", {"thumb_path": generated})
        return generated

    # ──────────────────────────── YOUTUBE UPLOAD STAGE ────────────────────────────

    def _stage_upload(self, script: EpisodeScript, video_path: str, thumb_path: str) -> str:
        logger.info("📤 [المرحلة 6]: رفع الفيديو إلى YouTube…")
        dry = os.environ.get("DRY_RUN", "false").lower() == "true"
        if dry:
            logger.info("🧪 DRY_RUN مُفعّل — سيتم تجاهل الرفع الفعلي.")
            return "dry_run_video_id"
        try:
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
            import google.oauth2.credentials
            from get_token import YouTubeTokenManager

            token_manager = YouTubeTokenManager()
            token = token_manager.get_valid_access_token()
            creds = google.oauth2.credentials.Credentials(token=token)
            youtube = build("youtube", "v3", credentials=creds)

            body = {
                "snippet": {
                    "title": script.youtube_title,
                    "description": script.youtube_description,
                    "tags": script.youtube_tags[:15],
                    "categoryId": "27",
                    "defaultLanguage": "ar",
                },
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": True},
            }
            media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=5 * 1024 * 1024)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            retries = 0
            max_retries = 5
            while response is None:
                try:
                    status, response = request.next_chunk()
                    if status:
                        logger.info("📤 تقدم الرفع: %.1f%%", status.progress() * 100)
                except Exception as e:
                    retries += 1
                    if retries > max_retries:
                        raise e
                    logger.warning("⚠️ انقطاع أثناء الرفع، إعادة المحاولة (%d/%d): %s", retries, max_retries, e)
                    time.sleep(10 * retries)
            vid_id = response["id"]
            logger.info("✅ تم نشر الفيديو: https://youtube.com/watch?v=%s", vid_id)
            # رفع الغلاف
            if Path(thumb_path).exists():
                try:
                    logger.info("🖼️ رفع الغلاف المصغر…")
                    youtube.thumbnails().set(
                        videoId=vid_id,
                        media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
                    ).execute()
                except Exception as e:
                    logger.warning("⚠️ فشل رفع الغلاف: %s", e)
            return vid_id
        except Exception as e:
            logger.error(f"❌ فشل رفع الفيديو إلى YouTube: {e}")
            raise

    # ──────────────────────────── CLEANUP STAGE ────────────────────────────

    def _cleanup_temp_files(self, ep_dir: str, raw_video_path: Optional[str] = None) -> None:
        """تنظيف الملفات المؤقتة لتوفير المساحة"""
        seg_dir = Path(ep_dir) / "segments"
        if seg_dir.exists():
            try:
                shutil.rmtree(seg_dir)
                logger.info("🧹 تم تنظيف المقاطع المؤقتة (Segments) لتوفير المساحة.")
            except Exception as e:
                logger.warning(f"⚠️ لم يتم تنظيف الملفات المؤقتة: {e}")
        if raw_video_path and Path(raw_video_path).exists():
            try:
                Path(raw_video_path).unlink()
                logger.info(f"🧹 تم حذف الفيديو الخام: {raw_video_path}")
            except Exception as e:
                logger.warning(f"⚠️ لم يتم حذف الفيديو الخام: {e}")

    # ──────────────────────────── MAIN RUN LOOP ────────────────────────────

    def run(self, episode_number: Optional[int] = None) -> None:
        """
        التشغيل الرئيسي لخط الإنتاج.
        إذا تم تحديد episode_number، تعالج حلقة معينة، وإلا تبدأ بأول حلقة معلقة.
        """
        metrics = PipelineMetrics(episode_number=episode_number or 0)
        full_start = time.time()
        try:
            # تحديد الحلقة
            if episode_number is None:
                pending = self._db_get_pending()
                if not pending:
                    logger.info("✨ لا توجد حلقات معلقة. انتظار الجدولة التالية.")
                    return
                episode_number = pending["episode_number"]
                ep_id = pending["id"]
            else:
                ep_id = self._db_init_episode(episode_number)
            metrics.episode_number = episode_number
            logger.info("\n" + "═" * 60)
            logger.info(f"▶ بدء تشغيل خط الإنتاج للحلقة {episode_number}")
            logger.info("═" * 60)

            # تحديث الحالة إلى processing
            self._db_update_episode(ep_id, status=EpisodeStatus.PROCESSING)

            # إنشاء مجلد مؤقت للحلقة
            ep_dir = Paths.TEMP_EPISODES / f"ep_{episode_number:03d}"
            ep_dir.mkdir(parents=True, exist_ok=True)

            # 1. السكريبت
            start = time.time()
            script = self._stage_script(episode_number)
            metrics.script_time = time.time() - start
            script.episode_id = ep_id

            # 1.5 إصلاح السكريبت إذا لزم الأمر
            script = self._stage_script_repair(script, episode_number)

            # 2. الصوت
            start = time.time()
            audio_map = self._stage_audio(script, str(ep_dir))
            metrics.audio_time = time.time() - start

            # 3. البصريات
            start = time.time()
            self._stage_visuals(script, str(ep_dir))
            metrics.visual_time = time.time() - start

            # 4. الفيديو الخام
            start = time.time()
            raw_video = self._stage_video(script, str(ep_dir))
            metrics.video_time = time.time() - start

            # 4.5 التلعيب
            start = time.time()
            final_video = self._stage_gamification(script, raw_video)
            metrics.gamification_time = time.time() - start

            # 5. الغلاف
            start = time.time()
            thumb = self._stage_thumbnail(script, str(ep_dir))
            metrics.thumbnail_time = time.time() - start

            # 6. الرفع إلى يوتيوب
            start = time.time()
            video_id = self._stage_upload(script, final_video, thumb)
            metrics.upload_time = time.time() - start

            # تنظيف
            self._cleanup_temp_files(str(ep_dir), raw_video)

            # تحديث حالة النجاح
            self._db_update_episode(ep_id, status=EpisodeStatus.COMPLETED, youtube_url=f"https://youtube.com/watch?v={video_id}")
            metrics.total_time = time.time() - full_start
            metrics.failure = False
            logger.info(f"🎉 اكتملت الحلقة {episode_number} بنجاح في {metrics.total_time:.2f} ثانية")
            # يمكن حفظ metrics في قاعدة البيانات أو ملف لاحقاً

        except Exception as e:
            metrics.failure = True
            metrics.total_time = time.time() - full_start
            logger.error(f"❌ فشلت المنظومة في معالجة الحلقة {episode_number}:")
            logger.error(traceback.format_exc())
            if 'ep_id' in locals():
                self._db_update_episode(ep_id, status=EpisodeStatus.FAILED, error=str(e))
            raise