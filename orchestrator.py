"""
orchestrator.py — VALUE / QEEMA v5.0 (Enterprise Orchestrator)
================================================================
المايسترو الذي يربط كل المحركات بسلسلة منطقية.

تحديثات v5:
- يستخدم voice_engine الجديد (ElevenLabs primary)
- يضيف Quran audio fetching ضمن stage الصوت
- يضيف intro/outro wrapping كـ stage مستقل بعد التجميع
- gamification أصبح اختياري (بعد intro/outro)
- ينظف import statements
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from supabase import create_client, Client

from config import APIKeys, DBConfig, Paths
from models import EpisodeScript, EpisodeStatus
from script_engine import ScriptEngine
from voice_engine import VoiceEngine                      # ✅ NEW (no _v2)
from visual_engine import VisualEngine
from sfx_engine import SFXEngine
from gamification_engine import GamificationEngine
from video_engine import VideoEngine
from intro_outro_engine import IntroOutroEngine          # ✅ NEW
from thumbnail_engine import ThumbnailEngine
from quality_gate import QualityGate

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    def __init__(self):
        self.db: Optional[Client] = None
        self.quality_gate = QualityGate()
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    # ──────────────────────────────────────────────
    # تهيئة
    # ──────────────────────────────────────────────
    def _init_supabase(self) -> None:
        try:
            self.db = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
            self.db.table(DBConfig.TABLE_EPISODES).select("count", count="exact").limit(0).execute()
            logger.info("✅ Supabase متصل")
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Supabase: {e}")
            raise RuntimeError("لا يمكن الاتصال بقاعدة البيانات") from e

    def _init_engines(self) -> None:
        logger.info("🔧 تهيئة المحركات...")
        self.script = ScriptEngine()
        self.voice = VoiceEngine()
        self.visual = VisualEngine()
        self.sfx = SFXEngine()
        self.gamify = GamificationEngine()
        self.video = VideoEngine()
        self.intro_outro = IntroOutroEngine()           # ✅ NEW
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ كل المحركات جاهزة")

    # ──────────────────────────────────────────────
    # Database helpers
    # ──────────────────────────────────────────────
    def _db_get_episode(self, episode_number: int) -> Optional[Dict[str, Any]]:
        try:
            r = self.db.table(DBConfig.TABLE_EPISODES).select("*").eq("episode_number", episode_number).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"خطأ جلب الحلقة {episode_number}: {e}")
            return None

    def _db_get_pending(self) -> Optional[Dict[str, Any]]:
        try:
            r = (self.db.table(DBConfig.TABLE_EPISODES)
                 .select("*").eq("status", "pending")
                 .order("episode_number").limit(1).execute())
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"خطأ جلب الحلقة المعلقة: {e}")
            return None

    def _db_update_episode(self, ep_id: str, **fields) -> None:
        upd = fields.copy()
        upd["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "status" in upd and hasattr(upd["status"], "value"):
            upd["status"] = upd["status"].value.lower()
        upd.pop("error", None)
        try:
            self.db.table(DBConfig.TABLE_EPISODES).update(upd).eq("id", ep_id).execute()
        except Exception as e:
            logger.warning(f"⚠️ Supabase update failed: {e}")

    def _db_init_episode(self, episode_number: int) -> str:
        existing = self._db_get_episode(episode_number)
        if existing:
            return existing["id"]
        now = datetime.now(timezone.utc).isoformat()
        res = self.db.table(DBConfig.TABLE_EPISODES).insert({
            "episode_number": episode_number,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }).execute()
        if not res.data:
            raise RuntimeError(f"Failed to create episode {episode_number}")
        return res.data[0]["id"]

    def _db_save_state(self, ep_id: str, stage: str, state: Dict[str, Any]) -> None:
        rec = {
            "episode_id": ep_id, "stage": stage,
            "state_data": json.dumps(state, ensure_ascii=False),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.db.table("pipeline_state").upsert(rec, on_conflict="episode_id,stage").execute()
        except Exception as e:
            logger.warning(f"⚠️ فشل حفظ حالة {stage}: {e}")

    def _save_script_state(self, script: EpisodeScript) -> None:
        save_path = Paths.SCRIPT_DIR / f"episode_{script.episode_number:03d}.json"
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")

    # ──────────────────────────────────────────────
    # المراحل الإنتاجية
    # ──────────────────────────────────────────────
    def _stage_script(self, ep_num: int) -> EpisodeScript:
        logger.info("📝 [المرحلة 1] توليد السكريبت...")
        cached = self.script.load_from_disk(ep_num)
        if cached:
            logger.info("♻️ سكريبت موجود، استئناف.")
            return cached
        script = self.script.generate(ep_num)
        self._save_script_state(script)
        return script

    def _stage_audio(self, script: EpisodeScript, ep_dir: str) -> Dict[str, str]:
        logger.info("🎙️ [المرحلة 2] هندسة الصوت (ElevenLabs + Quran)...")
        audio_map_file = Path(ep_dir) / "audio_map.json"

        if audio_map_file.exists():
            try:
                raw = json.loads(audio_map_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and all(Path(p).exists() for p in raw.values()):
                    logger.info("♻️ صوت جاهز، استئناف.")
                    self._update_script_audio_paths(script, raw)
                    return raw
            except Exception as e:
                logger.warning(f"audio_map.json غير صالح: {e}")

        # توليد الصوت (TTS + Quran reciter)
        audio_map = self.voice.generate_episode_audio(script, ep_dir)
        audio_map_file.write_text(json.dumps(audio_map, ensure_ascii=False), encoding="utf-8")

        # معالجة SFX (normalize + fade)
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
            if f"{sid}_intro" in audio_map: sc.intro_audio = audio_map[f"{sid}_intro"]
            if f"{sid}_explain" in audio_map: sc.explain_audio = audio_map[f"{sid}_explain"]
            if f"{sid}_ayah" in audio_map: sc.ayah_audio = audio_map[f"{sid}_ayah"]
        for sc in script.mid_scenes:
            key = f"mid_{sc.scene_id}"
            if key in audio_map: sc.audio_path = audio_map[key]

    def _stage_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        logger.info("🎨 [المرحلة 3] توليد الصور (Leonardo Phoenix)...")
        vis_map_file = Path(ep_dir) / "visuals_map.json"

        if vis_map_file.exists():
            try:
                vis_map = json.loads(vis_map_file.read_text(encoding="utf-8"))
                if all(Path(p).exists() for p in vis_map.values()):
                    logger.info("♻️ صور جاهزة، استئناف.")
                    self._set_scene_images(script, vis_map)
                    self._save_script_state(script)
                    return
            except Exception:
                pass

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

    def _set_scene_images(self, script: EpisodeScript, vis_map: Dict[str, str]) -> None:
        if "intro" in vis_map: script.intro_scene.image_path = vis_map["intro"]
        if "outro" in vis_map: script.outro_scene.image_path = vis_map["outro"]
        for sc in script.ayah_scenes:
            key = f"ayah_{sc.scene_id}"
            if key in vis_map: sc.image_path = vis_map[key]
        for sc in script.mid_scenes:
            key = f"mid_{sc.scene_id}"
            if key in vis_map: sc.image_path = vis_map[key]

    def _stage_video(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info("🎬 [المرحلة 4] تجميع الفيديو الخام...")
        raw_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
        if raw_path.exists() and raw_path.stat().st_size > 100_000:
            logger.info("♻️ فيديو خام موجود، استئناف.")
            return str(raw_path)
        return self.video.assemble_episode(script, ep_dir)

    # ✅ NEW v5: مرحلة الـ branding wrapper
    def _stage_branding(self, script: EpisodeScript, raw_video: str) -> str:
        logger.info("🎭 [المرحلة 4.5] إضافة الانترو والأوترو...")
        branded_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_branded.mp4"
        if branded_path.exists() and branded_path.stat().st_size > 100_000:
            logger.info("♻️ فيديو مع الهوية موجود.")
            return str(branded_path)
        return self.intro_outro.wrap_episode(raw_video, str(branded_path))

    def _stage_gamification(self, script: EpisodeScript, branded_video: str) -> str:
        logger.info("🎮 [المرحلة 5] إضافة التلعيب (شريط تقدم + تشجيع)...")
        final_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_final.mp4"
        if final_path.exists() and final_path.stat().st_size > 100_000:
            return str(final_path)
        result = self.gamify.apply_to_episode(branded_video, script, str(final_path))
        return result

    def _stage_thumbnail(self, script: EpisodeScript, ep_dir: str) -> str:
        logger.info("🖼️ [المرحلة 6] الغلاف المصغر...")
        thumb_path = Paths.THUMBNAILS / f"ep_{script.episode_number:03d}.jpg"
        if thumb_path.exists():
            return str(thumb_path)
        return self.thumbnail.create(script, script.episode_number, script.intro_scene.image_path)

    def _stage_upload(self, script: EpisodeScript, video_path: str, thumb_path: str) -> str:
        logger.info("📤 [المرحلة 7] رفع الفيديو على YouTube...")
        dry = os.environ.get("DRY_RUN", "false").lower() == "true"
        if dry:
            logger.info("🧪 DRY_RUN: تم تخطي الرفع.")
            return "dry_run_video_id"

        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from get_token import YouTubeTokenManager  # ✅ now exists!

        token_manager = YouTubeTokenManager()
        token = token_manager.get_valid_access_token()
        import google.oauth2.credentials
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
                    logger.info(f"📤 تقدم: {status.progress() * 100:.1f}%")
            except Exception as e:
                retries += 1
                if retries > max_retries:
                    raise e
                logger.warning(f"⚠️ انقطاع، إعادة ({retries}/{max_retries})")
                time.sleep(10 * retries)

        video_id = response["id"]
        logger.info(f"✅ تم الرفع: https://youtube.com/watch?v={video_id}")

        if Path(thumb_path).exists():
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
                ).execute()
            except Exception as e:
                logger.warning(f"⚠️ فشل رفع thumbnail: {e}")

        return video_id

    # ──────────────────────────────────────────────
    # تنظيف
    # ──────────────────────────────────────────────
    def _cleanup_temp_files(self, ep_dir: str, raw_video_path: Optional[str] = None,
                             branded_video_path: Optional[str] = None) -> None:
        seg_dir = Path(ep_dir) / "segments"
        if seg_dir.exists():
            shutil.rmtree(seg_dir, ignore_errors=True)

        # نحتفظ بالـ final فقط
        for p in [raw_video_path, branded_video_path]:
            if p and Path(p).exists():
                try: Path(p).unlink()
                except Exception: pass

    # ──────────────────────────────────────────────
    # Main run
    # ──────────────────────────────────────────────
    def run(self, episode_number: int) -> bool:
        ep_id = self._db_init_episode(episode_number)
        self._db_update_episode(ep_id, status="processing")
        ep_dir = Paths.TEMP_EPISODES / f"ep_{episode_number:03d}"
        ep_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 1) Script
            script = self._stage_script(episode_number)
            script.episode_id = ep_id

            # 2) Audio
            self._stage_audio(script, str(ep_dir))

            # 3) Visuals
            self._stage_visuals(script, str(ep_dir))

            # 4) Raw video (segments only — no branding)
            raw_video = self._stage_video(script, str(ep_dir))

            # 4.5) Branding wrap (intro + main + outro)
            branded_video = self._stage_branding(script, raw_video)

            # 5) Gamification overlay
            final_video = self._stage_gamification(script, branded_video)

            # 6) Thumbnail
            thumbnail = self._stage_thumbnail(script, str(ep_dir))

            # 7) Upload
            video_id = self._stage_upload(script, final_video, thumbnail)

            # Cleanup
            self._cleanup_temp_files(str(ep_dir), raw_video, branded_video)

            self._db_update_episode(
                ep_id,
                status="completed",
                youtube_url=f"https://youtube.com/watch?v={video_id}",
            )
            logger.info(f"🎉 اكتملت الحلقة {episode_number}")
            return True

        except Exception as e:
            logger.error(f"❌ فشلت الحلقة {episode_number}: {e}", exc_info=True)
            self._db_update_episode(ep_id, status="failed")
            return False

    def run_next(self) -> bool:
        pending = self._db_get_pending()
        if not pending:
            logger.info("✨ لا توجد حلقات معلقة.")
            return False
        return self.run(pending["episode_number"])

    def seed(self) -> None:
        from config import CURRICULUM
        for ep_num, data in CURRICULUM.items():
            ep_id = self._db_init_episode(ep_num)
            self._db_update_episode(ep_id, surah=data.get("name"))
        logger.info("🌱 تم بذر منهج VALUE في قاعدة البيانات.")
