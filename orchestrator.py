from __future__ import annotations

import json
import logging
import traceback
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import supabase
from supabase import create_client, Client

from config import APIKeys, DBConfig, Paths
from models import EpisodeScript, EpisodeStatus, PipelineState, PipelineMetrics
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
    قائد المنظومة الكاملة: يتتبع كل مرحلة، ويعيد استئنافها عند الحاجة، ويحافظ على حالة كاملة.
    """

    def __init__(self):
        self.db: Optional[Client] = None
        self.quality_gate = QualityGate()
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    def _init_supabase(self):
        self.db = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
        if self.db.auth.get_user() is None:
            raise RuntimeError("❌ لا يوجد مستخدم موثوق في Supabase — تأكد من المفتاح.")
        logger.info("✅ Supabase متصل ومستعد لتسجيل العمليات")

    def _init_engines(self):
        logger.info("🔧 تهيئة وإحماء المحركات (Engines Boot-up)…")
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
        """جلب حلقة محددة من DB بال episode_number"""
        if not self.db:
            return None
        r = self.db.table(DBConfig.TABLE_EPISODES).select("*").eq("episode_number", episode_number).execute()
        return r.data[0] if r.data else None

    def _db_get_pending(self) -> Optional[Dict[str, Any]]:
        """جلب أول حلقة مُعلّقة (pending) مرتبة حسب episode_number"""
        if not self.db:
            return None
        r = (
            self.db
            .table(DBConfig.TABLE_EPISODES)
            .select("*")
            .eq("status", "pending")
            .order("episode_number")
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None

    def _db_update_episode(self, ep_id: str, **fields) -> None:
        """
        تحديث حقل episode في Supabase، مع تعيين updated_at تلقائيًا.
        """
        upd = fields.copy()
        upd["updated_at"] = datetime.now(timezone.utc).isoformat()

        if "status" in upd and hasattr(upd["status"], "value"):
            upd["status"] = upd["status"].value.lower()

        try:
            self.db.table(DBConfig.TABLE_EPISODES).update(upd).eq("id", ep_id).execute()
        except Exception as e:
            logger.warning("⚠️ Supabase update failed: %s", e)

    def _db_init_episode(self, episode_number: int) -> str:
        """
        إنشاء سجل حلقة في DB إذا لم يوجد.
        """
        r = (
            self.db
            .table(DBConfig.TABLE_EPISODES)
            .select("id,status,episode_number")
            .eq("episode_number", episode_number)
            .execute()
        )
        if r.data:
            logger.debug("Episode %s already exists with status: %s", episode_number, r.data[0]["status"])
            return r.data[0]["id"]

        now = datetime.now(timezone.utc).isoformat()
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

    def _db_save_state(self, ep_id: str, stage: str, state: Dict[str, Any]) -> None:
        """
        حفظ حالة pipeline (مثلاً: السكريبت، مسارات الصوت، المقاطع).
        """
        rec = {
            "episode_id": ep_id,
            "stage": stage,
            "state_data": json.dumps(state, ensure_ascii=False),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.db.table("pipeline_state").upsert(rec).execute()

    def _db_load_state(self, ep_id: str, stage: str) -> Optional[Dict[str, Any]]:
        """
        استرجاع حالة pipeline من DB.
        """
        r = (
            self.db
            .table("pipeline_state")
            .select("state_data")
            .eq("episode_id", ep_id)
            .eq("stage", stage)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if r.data:
            data = json.loads(r.data[0]["state_data"])
            logger.info("♻️ استرجاع حالة %s من Supabase", stage)
            return data
        return None

    def _save_script_state(self, script: EpisodeScript) -> None:
        """
        حفظ حالة السكريبت محليًا، وخلال operations المنفصلة قد يُستخدم لـ recovery.
        """
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
        """
        إعادة تشغيل QualityGate‑Based Script Repair قبل الانتقال للمرحلة التالية.
        """
        logger.info("🔧 [المرحلة 1.5]: إصلاح ذاتي للسكريبت بناءً على Quality Gate…")
        raw = script.model_dump()
        report = self.quality_gate.evaluate(raw)

        if report.passed:
            logger.info("✅ السكريبت ناجح في البوابة دون إصلاح.")
            return script

        logger.warning("⚠️ السكريبت يحتاج إصلاح ذاتي؛ التقييم: %.1f%%", report.overall_score)

        # يمكنك هنا إرسال "Self‑Repair Prompt" إلى script_engine مع الحفاظ على الهيكل
        rep = self.script.generate(ep_num)  # أو إعادة توليد ذاتي محدود
        self._save_script_state(rep)
        return rep

    # ──────────────────────────── AUDIO STAGE ────────────────────────────

    def _stage_audio(self, script: EpisodeScript, ep_dir: str) -> Dict[str, str]:
        """
        توليد وتنقية وتعديل مسار الصوت والتوفر.
        """
        logger.info("🎙️ [المرحلة 2]: هندسة الصوت والمؤثرات…")
        audio_map_file = Path(ep_dir) / "audio_map.json"

        # 1. استئناف من القرص إن وُجد
        if audio_map_file.exists():
            try:
                raw = json.loads(audio_map_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    # التأكد من أن جميع الملفات موجودة
                    miss = [k for k, p in raw.items() if not Path(p).exists()]
                    if not miss:
                        logger.info("♻️ استئناف: خريطة الصوت مستعادة من القرص.")
                        self._update_script_audio_paths(script, raw)
                        return raw
                    else:
                        logger.warning("❌ ملفات صوت مفقودة: %s — إعادة التوليد", miss)
            except Exception as e:
                logger.warning("❌ ملف audio_map.json غير صالح، سيتم إعادة التوليد: %s", e)

        # 2. توليد الصوت الأساسي
        audio_map = self.voice.generate_episode_audio(script, ep_dir)
        if not isinstance(audio_map, dict) or not audio_map:
            logger.error("❌ محرك الصوت لم يُعدّ خريطة صوت صحيحة.")
            raise ValueError("مخرجات محرك الصوت غير متوافقة")

        # 3. حفظ الخريطة أولًا
        audio_map_file.write_text(json.dumps(audio_map, ensure_ascii=False), encoding="utf-8")

        # 4. معالجة المؤثرات
        processed = self.sfx.process_all(audio_map, script, ep_dir)
        self._update_script_audio_paths(script, processed)

        # 5. حفظ الحالة في DB أيضًا
        self._save_script_state(script)
        self._db_save_state(script.episode_id, "audio", processed)

        return processed

    def _update_script_audio_paths(self, script: EpisodeScript, audio_map: Dict[str, str]):
        """
        مزامنة audio_map مع EpisodeScript.
        """
        if ep := script.intro_scene:
            if "intro" in audio_map:
                ep.audio_path = audio_map["intro"]
        if ep := script.outro_scene:
            if "outro" in audio_map:
                ep.audio_path = audio_map["outro"]

        for sc in script.ayah_scenes:
            sid = f"ayah_{sc.scene_id}"
            if f"{sid}_intro" in audio_map:
                sc.intro_audio = audio_map[f"{sid}_intro"]
            if f"{sid}_quran" in audio_map:
                sc.quran_audio = audio_map[f"{sid}_quran"]
            if f"{sid}_explain" in audio_map:
                sc.explain_audio = audio_map[f"{sid}_explain"]

        for sc in script.mid_scenes:
            k = f"mid_{sc.scene_id}"
            if k in audio_map:
                sc.audio_path = audio_map[k]

    # ──────────────────────────── VISUAL STAGE ────────────────────────────

    def _stage_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        """
        توليد وتحديث visual map للمشهد.
        """
        logger.info("🎨 [المرحلة 3]: توليد الإنفوجرافيك البصري…")
        vis_map_file = Path(ep_dir) / "visuals_map.json"

        if vis_map_file.exists():
            logger.info("♻️ استئناف: خريطة الصور مُسترجعة من القرص.")
            vis_map = json.loads(vis_map_file.read_text(encoding="utf-8"))

            # التحقق من وجود الملفات
            for k, p in vis_map.items():
                if Path(p).exists():
                    self._set_scene_image(script, k, p)

            vis_map_file.write_text(json.dumps(vis_map, ensure_ascii=False), encoding="utf-8")
            self._save_script_state(script)
            self._db_save_state(script.episode_id, "visuals", vis_map)
            return

        self.visual.generate_episode_visuals(script, ep_dir)

        vis_map = {
            "intro": script.intro_scene.image_path,
            "outro": script.outro_scene.image_path,
        }
        for sc in script.ayah_scenes:
            vis_map[f"ayah_{sc.scene_id}"] = sc.image_path
        for sc in script.mid_scenes:
            vis_map[f"mid_{sc.scene_id}"] = sc.image_path

        vis_map_file.write_text(json.dumps(vis_map, ensure_ascii=False), encoding="utf-8")
        self._save_script_state(script)
        self._db_save_state(script.episode_id, "visuals", vis_map)

    def _set_scene_image(self, script: EpisodeScript, key: str, path: str):
        if key == "intro":
            script.intro_scene.image_path = path
        elif key == "outro":
            script.outro_scene.image_path = path
        else:
            for sc in script.ayah_scenes:
                if key == f"ayah_{sc.scene_id}":
                    sc.image_path = path
            for sc in script.mid_scenes:
                if key == f"mid_{sc.scene_id}" and Path(path).exists():
                    sc.image_path = path

    # ──────────────────────────── VIDEO ASSEMBLY ────────────────────────────

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
        """
        تطبيق التأثيرات البصرية والتفاعلية (Progress bar, Encouragements).
        """
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
        """
        رفع الفيديو على YouTube مع الغلاف.
        """
        logger.info("📤 [المرحلة 6]: رفع الفيديو على YouTube…")

        # 1. Dry‑run mode
        dry = __import__("os").environ.get("DRY_RUN", "false").lower() == "true"
        if dry:
            logger.info("🧪 DRY_RUN مُفعّل — سيتم تجاهل الرفع الفعلي.")
            return "dry_run_video_id"

        # 2. إعداد YouTube client
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        import google.oauth2.credentials
        from get_token import YouTubeTokenManager

        token = YouTubeTokenManager().get_valid_access_token()
        creds = google.oauth2.credentials.Credentials(token=token)
        youtube = build("youtube", "v3", credentials=creds)

        # 3. إعداد بيانات الفيديو
        body = {
            "snippet": {
                "title": script.youtube_title,
                "description": script.youtube_description,
                "tags": script.youtube_tags[:15],
                "categoryId": "27",
                "defaultLanguage": "ar",
            },
            "status": {"privacyStatus