"""
orchestrator.py — QEEMA v4.0 (Enterprise Orchestrator)
مدير خط الإنتاج الرئيسي.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import traceback
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

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


class PipelineOrchestrator:
    """
    قائد المنظومة: يدير مراحل الإنتاج ويتابع الحالة في Supabase.
    """

    def __init__(self):
        self.db: Optional[Client] = None
        self.quality_gate = QualityGate()
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    # --------------------------------------------------------------
    # تهيئة قواعد البيانات والمحركات
    # --------------------------------------------------------------
    def _init_supabase(self) -> None:
        """الاتصال بـ Supabase (بدون الحاجة إلى أمان المستخدم)"""
        try:
            self.db = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
            # اختبار الاتصال عبر قراءة جدول (بدون مصادقة مسبقة)
            self.db.table(DBConfig.TABLE_EPISODES).select("count", count="exact").limit(0).execute()
            logger.info("✅ Supabase متصل ومستعد لتسجيل العمليات")
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Supabase: {e}")
            raise RuntimeError("لا يمكن الاتصال بقاعدة البيانات") from e

    def _init_engines(self) -> None:
        logger.info("🔧 تهيئة وإحماء المحركات (Engines Boot-up)…")
        self.script = ScriptEngine()
        self.voice = VoiceEngine()
        self.visual = VisualEngine()
        self.sfx = SFXEngine()
        self.gamify = GamificationEngine()
        self.video = VideoEngine()
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ جميع المحركات في وضع الاستعداد الأقصى")

    # --------------------------------------------------------------
    # دوال إدارة قاعدة البيانات (CRUD)
    # --------------------------------------------------------------
    def _db_get_episode(self, episode_number: int) -> Optional[Dict[str, Any]]:
        try:
            r = self.db.table(DBConfig.TABLE_EPISODES).select("*").eq("episode_number", episode_number).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"خطأ في جلب الحلقة {episode_number}: {e}")
            return None

    def _db_get_pending(self) -> Optional[Dict[str, Any]]:
        try:
            r = (self.db.table(DBConfig.TABLE_EPISODES)
                 .select("*")
                 .eq("status", "pending")
                 .order("episode_number")
                 .limit(1)
                 .execute())
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"خطأ في جلب الحلقة المعلقة: {e}")
            return None

    def _db_update_episode(self, ep_id: str, **fields) -> None:
        upd = fields.copy()
        upd["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "status" in upd and hasattr(upd["status"], "value"):
            upd["status"] = upd["status"].value.lower()
        try:
            self.db.table(DBConfig.TABLE_EPISODES).update(upd).eq("id", ep_id).execute()
        except Exception as e:
            logger.warning(f"⚠️ Supabase update failed: {e}")

    def _db_init_episode(self, episode_number: int) -> str:
        existing = self._db_get_episode(episode_number)
        if existing:
            logger.debug("Episode %s already exists with status: %s", episode_number, existing["status"])
            return existing["id"]
        now = datetime.now(timezone.utc).isoformat()
        try:
            res = self.db.table(DBConfig.TABLE_EPISODES).insert({
                "episode_number": episode_number,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            }).execute()
            if not res.data:
                raise RuntimeError(f"Failed to create episode {episode_number}")
            return res.data[0]["id"]
        except Exception as e:
            logger.error(f"فشل إنشاء الحلقة {episode_number}: {e}")
            raise

    def _db_save_state(self, ep_id: str, stage: str, state: Dict[str, Any]) -> None:
        rec = {
            "episode_id": ep_id,
            "stage": stage,
            "state_data": json.dumps(state, ensure_ascii=False),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.db.table("pipeline_state").upsert(rec, on_conflict="episode_id,stage").execute()
        except Exception as e:
            logger.warning(f"⚠️ فشل حفظ حالة المرحلة {stage}: {e}")

    def _db_load_state(self, ep_id: str, stage: str) -> Optional[Dict[str, Any]]:
        try:
            r = (self.db.table("pipeline_state")
                 .select("state_data")
                 .eq("episode_id", ep_id)
                 .eq("stage", stage)
                 .order("updated_at", desc=True)
                 .limit(1)
                 .execute())
            if r.data:
                logger.info("♻️ استرجاع حالة %s من Supabase", stage)
                return json.loads(r.data[0]["state_data"])
        except Exception as e:
            logger.warning(f"⚠️ فشل استرجاع حالة {stage}: {e}")
        return None

    def _save_script_state(self, script: EpisodeScript) -> None:
        save_path = Paths.SCRIPT_DIR / f"episode_{script.episode_number:03d}.json"
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        logger.debug("💾 السكريبت محليًا: %s", save_path)

    # --------------------------------------------------------------
    # المراحل الإنتاجية
    # --------------------------------------------------------------
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
            logger.info("✅ السكريبت ناجح دون إصلاح.")
            return script
        logger.warning("⚠️ السكريبت يحتاج إصلاح، التقييم: %.1f%%", report.overall_score)
        repaired = self.script.generate(ep_num)
        self._save_script_state(repaired)
        return repaired

    def _stage_audio(self, script: EpisodeScript, ep_dir: str) -> Dict[str, str]:
        logger.info("🎙️ [المرحلة 2]: هندسة الصوت والمؤثرات…")
        audio_map_file = Path(ep_dir) / "audio_map.json"
        if audio_map_file.exists():
            try:
                raw = json.loads(audio_map_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and all(Path(p).exists() for p in raw.values()):
                    logger.info("♻️ استئناف: خريطة الصوت موجودة.")
                    self._update_script_audio_paths(script, raw)
                    return raw
                else:
                    logger.warning("بعض الملفات الصوتية مفقودة، إعادة التوليد")
            except Exception as e:
                logger.warning("audio_map.json غير صالح: %s", e)
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
        if "intro" in audio_map:
            script.intro_scene.audio_path = audio_map["intro"]
        if "outro" in audio_map:
            script.outro_scene.audio_path = audio_map["outro"]
        for sc in script.ayah_scenes:
            sid = f"ayah_{sc.scene_id}"
            if f"{sid}_intro" in audio_map:
                sc.intro_audio = audio_map[f"{sid}_intro"]
            if f"{sid}_explain" in audio_map:
                sc.explain_audio = audio_map[f"{sid}_explain"]
        for sc in script.mid_scenes:
            key = f"mid_{sc.scene_id}"
            if key in audio_map:
                sc.audio_path = audio_map[key]

    def _stage_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        logger.info("🎨 [المرحلة 3]: توليد الإنفوجرافيك البصري…")
        vis_map_file = Path(ep_dir) / "visuals_map.json"
        if vis_map_file.exists():
            try:
                vis_map = json.loads(vis_map_file.read_text(encoding="utf-8"))
                if all(Path(p).exists() for p in vis_map.values()):
                    logger.info("♻️ استئناف: خريطة الصور صالحة.")
                    self._set_scene_images(script, vis_map)
                    self._save_script_state(script)
                    self._db_save_state(script.episode_id, "visuals", vis_map)
                    return
            except Exception as e:
                logger.warning("visuals_map.json غير صالح: %s", e)
        self.visual.generate_episode_visuals(script, ep_dir)
        vis_map = {"intro": script.intro_scene.image_path, "outro": script.outro_scene.image_path}
        for sc in script.ayah_scenes:
            vis_map[f"ayah_{sc.scene_id}"] = sc.image_path
        for sc in script.mid_scenes:
            vis_map[f"mid_{sc.scene_id}"] = sc.image_path
        vis_map_file.write_text(json.dumps(vis_map, ensure_ascii=False), encoding="utf-8")
        self._save_script_state(script)
        self._db_save_state(script.episode_id, "visuals", vis_map)

    def _set_scene_images(self, script: EpisodeScript, vis_map: Dict[str, str]) -> None:
        if "intro" in vis_map:
            script.intro_scene.image_path = vis_map["intro"]
        if "outro" in vis_map:
            script.outro_scene.image_path = vis_map["outro"]
        for sc in script.ayah_scenes:
            key = f"ayah_{sc.scene_id}"
            if key in vis_map:
                sc.image_path = vis_map[key]
        for sc in script.mid_scenes:
            key = f"mid_{sc.scene_id}"
            if key in vis_map:
                sc.image_path = vis_map[key]

    def _stage_video(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info("🎬 [المرحلة 4]: تجميع الفيديو الخام…")
        raw_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
        if raw_path.exists():
            logger.info("♻️ الفيديو الخام موجود.")
            return str(raw_path)
        raw_video = self.video.assemble_episode(script, ep_dir)
        self._db_save_state(script.episode_id, "video", {"raw_path": raw_video})
        return raw_video

    def _stage_gamification(self, script: EpisodeScript, raw_video_path: str) -> str:
        logger.info("🎮 [المرحلة 4.5]: إضافة التلعيب (Gamification)…")
        final_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_final.mp4"
        if final_path.exists():
            logger.info("♻️ الفيديو النهائي موجود.")
            return str(final_path)
        result = self.gamify.apply_to_episode(raw_video_path, script, str(final_path))
        self._db_save_state(script.episode_id, "gamification", {"final_path": result})
        return result

    def _stage_thumbnail(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info("🖼️ [المرحلة 5]: تصميم الغلاف المصغر…")
        thumb_path = Paths.THUMBNAILS / f"ep_{script.episode_number:03d}.jpg"
        if thumb_path.exists():
            logger.info("♻️ الغلاف موجود.")
            return str(thumb_path)
        generated = self.thumbnail.create(script, script.episode_number, script.intro_scene.image_path)
        self._db_save_state(script.episode_id, "thumbnail", {"thumb_path": generated})
        return generated

    def _stage_upload(self, script: EpisodeScript, video_path: str, thumb_path: str) -> str:
        logger.info("📤 [المرحلة 6]: رفع الفيديو إلى YouTube…")
        dry = os.environ.get("DRY_RUN", "false").lower() == "true"
        if dry:
            logger.info("🧪 DRY_RUN مفعّل، لن يتم الرفع الفعلي.")
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
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": True,
                }
            }

            media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=5*1024*1024)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

            response = None
            retries = 0
            max_retries = 5
            while response is None:
                try:
                    status, response = request.next_chunk()
                    if status:
                        logger.info(f"📤 تقدم الرفع: {status.progress() * 100:.1f}%")
                except Exception as e:
                    retries += 1
                    if retries > max_retries:
                        raise e
                    logger.warning(f"⚠️ انقطاع أثناء الرفع، إعادة المحاولة ({retries}/{max_retries}): {e}")
                    time.sleep(10 * retries)

            video_id = response["id"]
            logger.info(f"✅ تم الرفع: https://youtube.com/watch?v={video_id}")

            if Path(thumb_path).exists():
                try:
                    youtube.thumbnails().set(
                        videoId=video_id,
                        media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
                    ).execute()
                    logger.info("🖼️ رفع الغلاف بنجاح.")
                except Exception as e:
                    logger.warning(f"⚠️ فشل رفع الغلاف: {e}")

            return video_id
        except Exception as e:
            logger.error(f"❌ فشل رفع الفيديو: {e}")
            raise

    # --------------------------------------------------------------
    # تنظيف الملفات المؤقتة
    # --------------------------------------------------------------
    def _cleanup_temp_files(self, ep_dir: str, raw_video_path: Optional[str] = None) -> None:
        seg_dir = Path(ep_dir) / "segments"
        if seg_dir.exists():
            try:
                shutil.rmtree(seg_dir)
                logger.info("🧹 تم تنظيف مقاطع الفيديو المؤقتة.")
            except Exception as e:
                logger.warning(f"⚠️ فشل تنظيف المقاطع: {e}")
        if raw_video_path and Path(raw_video_path).exists():
            try:
                Path(raw_video_path).unlink()
                logger.info("🧹 تم حذف الفيديو الخام الأصلي.")
            except Exception as e:
                logger.warning(f"⚠️ فشل حذف الفيديو الخام: {e}")

    # --------------------------------------------------------------
    # دوال التشغيل الرئيسية (API العامة)
    # --------------------------------------------------------------
    def run(self, episode_number: int) -> bool:
        """تنتج حلقة محددة بكل مراحلها."""
        ep_id = self._db_init_episode(episode_number)
        self._db_update_episode(ep_id, status="processing")
        ep_dir = Paths.TEMP_EPISODES / f"ep_{episode_number:03d}"
        ep_dir.mkdir(parents=True, exist_ok=True)

        try:
            script = self._stage_script(episode_number)
            script.episode_id = ep_id
            script = self._stage_script_repair(script, episode_number)

            audio_map = self._stage_audio(script, str(ep_dir))
            self._stage_visuals(script, str(ep_dir))
            raw_video = self._stage_video(script, str(ep_dir))
            final_video = self._stage_gamification(script, raw_video)
            thumbnail = self._stage_thumbnail(script, str(ep_dir))
            video_id = self._stage_upload(script, final_video, thumbnail)

            self._cleanup_temp_files(str(ep_dir), raw_video)

            self._db_update_episode(ep_id, status="completed", youtube_url=f"https://youtube.com/watch?v={video_id}")
            logger.info(f"🎉 اكتملت الحلقة {episode_number} بنجاح.")
            return True
        except Exception as e:
            logger.error(f"❌ فشلت الحلقة {episode_number}: {e}")
            self._db_update_episode(ep_id, status="failed", error=str(e))
            return False

    def run_next(self) -> bool:
        """تنتج أول حلقة معلقة."""
        pending = self._db_get_pending()
        if not pending:
            logger.info("✨ لا توجد حلقات معلقة.")
            return False
        ep_num = pending["episode_number"]
        return self.run(ep_num)

    def seed(self) -> None:
        """يولد جدول المنهج في قاعدة البيانات (اختياري)."""
        from config import CURRICULUM
        for ep_num, data in CURRICULUM.items():
            self._db_init_episode(ep_num)
            self._db_update_episode(ep_id=self._db_get_episode(ep_num)["id"], surah=data.get("name"))
        logger.info("🌱 تم بذر منهج VALUE في قاعدة البيانات.")