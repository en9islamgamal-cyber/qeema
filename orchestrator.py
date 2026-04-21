"""
orchestrator.py — VALUE / QEEMA v2
═══════════════════════════════════════════════════════
قائد المنظومة الكاملة
• استئناف من أي مرحلة عند الانقطاع
• حفظ الحالة في Supabase بعد كل خطوة
• معالجة استثنائية شاملة
═══════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from supabase import create_client, Client

from config import APIKeys, DBConfig, Paths
from models import EpisodeScript, EpisodeStatus, PipelineState
from script_engine  import ScriptEngine
from voice_engine_v2 import VoiceEngine
from visual_engine  import VisualEngine
from sfx_engine     import SFXEngine
from gamification_engine import GamificationEngine
from video_engine   import VideoEngine
from thumbnail_engine import ThumbnailEngine

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """
    ينفّذ خط الإنتاج الكامل مع:
    - استئناف ذكي من آخر نقطة
    - حفظ تدريجي في Supabase
    - معالجة أخطاء شاملة
    """

    def __init__(self):
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    def _init_supabase(self):
        self.db: Client = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
        logger.info("✅ Supabase متصل")

    def _init_engines(self):
        logger.info("🔧 تهيئة المحركات…")
        self.script    = ScriptEngine()
        self.voice     = VoiceEngine()
        self.visual    = VisualEngine()
        self.sfx       = SFXEngine()
        self.gamify    = GamificationEngine()
        self.video     = VideoEngine()
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ جميع المحركات جاهزة")

    # ──────────────────────────── Supabase ─────
    def _db_get_next(self) -> Optional[dict]:
        r = (self.db.table(DBConfig.TABLE_EPISODES)
             .select("*")
             .eq("status", EpisodeStatus.PENDING)
             .order("episode_number")
             .limit(1)
             .execute())
        return r.data[0] if r.data else None

    def _db_update(self, ep_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self.db.table(DBConfig.TABLE_EPISODES).update(fields).eq("id", ep_id).execute()
        except Exception as e:
            logger.warning(f"⚠️ DB update فشل: {e}")

    def _db_init_episode(self, ep_num: int) -> str:
        r = (self.db.table(DBConfig.TABLE_EPISODES)
             .select("id,status")
             .eq("episode_number", ep_num)
             .execute())
        if r.data:
            return r.data[0]["id"]
        res = self.db.table(DBConfig.TABLE_EPISODES).insert({
            "episode_number": ep_num,
            "status": EpisodeStatus.PENDING,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return res.data[0]["id"]

    # ──────────────────────────── Stages ───────
    def _stage_script(self, ep_num: int) -> EpisodeScript:
        """مرحلة 1: السكريبت"""
        # استئناف إذا كان موجوداً
        cached = self.script.load_from_disk(ep_num)
        if cached:
            logger.info("♻️ استئناف السكريبت من القرص")
            return cached
        return self.script.generate(ep_num)

    def _stage_audio(self, script: EpisodeScript, ep_dir: str) -> dict:
        """مرحلة 2: الصوت"""
        audio_map_file = Path(ep_dir) / "audio_map.json"
        if audio_map_file.exists():
            logger.info("♻️ استئناف الصوت من القرص")
            raw = json.loads(audio_map_file.read_text())
            # تحقق من وجود الملفات فعلاً
            if all(Path(p).exists() for p in raw.values()):
                return raw

        audio_map = self.voice.generate_episode_audio(script, ep_dir)
        audio_map_file.write_text(json.dumps(audio_map, ensure_ascii=False))

        # معالجة الصوت (موسيقى + تطبيع)
        processed = self.sfx.process_all(audio_map, script, ep_dir)

        # تحديث مسارات الصوت في السكريبت
        self._update_script_audio_paths(script, processed)
        return processed

    def _update_script_audio_paths(self, script: EpisodeScript, audio_map: dict) -> None:
        """يحدّث مسارات الصوت في السكريبت بعد المعالجة"""
        if "intro" in audio_map:
            script.intro_scene.audio_path = audio_map["intro"]
        if "outro" in audio_map:
            script.outro_scene.audio_path = audio_map["outro"]
        for scene in script.ayah_scenes:
            sid = scene.scene_id
            if f"ayah_{sid}_intro"   in audio_map: scene.intro_audio   = audio_map[f"ayah_{sid}_intro"]
            if f"ayah_{sid}_quran"   in audio_map: scene.quran_audio   = audio_map[f"ayah_{sid}_quran"]
            if f"ayah_{sid}_explain" in audio_map: scene.explain_audio = audio_map[f"ayah_{sid}_explain"]
        for scene in script.mid_scenes:
            sid = scene.scene_id
            if f"mid_{sid}" in audio_map: scene.audio_path = audio_map[f"mid_{sid}"]

    def _stage_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        """مرحلة 3: الصور"""
        vis_map_file = Path(ep_dir) / "visuals_map.json"
        if vis_map_file.exists():
            logger.info("♻️ استئناف الصور من القرص")
            vis_map = json.loads(vis_map_file.read_text())
            # تطبيق المسارات
            if vis_map.get("intro") and Path(vis_map["intro"]).exists():
                script.intro_scene.image_path = vis_map["intro"]
            if vis_map.get("outro") and Path(vis_map["outro"]).exists():
                script.outro_scene.image_path = vis_map["outro"]
            for sc in script.ayah_scenes:
                k = f"ayah_{sc.scene_id}"
                if vis_map.get(k) and Path(vis_map[k]).exists():
                    sc.image_path = vis_map[k]
            return

        self.visual.generate_episode_visuals(script, ep_dir)

        # حفظ خريطة الصور
        vis_map = {"intro": script.intro_scene.image_path, "outro": script.outro_scene.image_path}
        for sc in script.ayah_scenes:
            vis_map[f"ayah_{sc.scene_id}"] = sc.image_path
        vis_map_file.write_text(json.dumps(vis_map, ensure_ascii=False))

    def _stage_video(self, script: EpisodeScript, ep_dir: str) -> str:
        """مرحلة 4: تجميع الفيديو"""
        final_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_final.mp4"
        if final_path.exists():
            logger.info("♻️ استئناف الفيديو من القرص")
            return str(final_path)
        return self.video.assemble_episode(script, ep_dir)

    def _stage_thumbnail(self, script: EpisodeScript, ep_dir: str) -> str:
        """مرحلة 5: الـ Thumbnail"""
        thumb_path = Paths.THUMBNAILS / f"ep_{script.episode_number:03d}.jpg"
        if thumb_path.exists():
            logger.info("♻️ استئناف Thumbnail من القرص")
            return str(thumb_path)

        scene_img = script.intro_scene.image_path
        return self.thumbnail.create(script, script.episode_number, scene_img)

    def _stage_upload(self, script: EpisodeScript, video_path: str, thumb_path: str) -> str:
        """مرحلة 6: النشر على YouTube"""
        dry = __import__("os").environ.get("DRY_RUN", "false").lower() == "true"
        if dry:
            logger.info("🧪 DRY_RUN — تجاوز الرفع")
            return "dry_run_video_id"

        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        import google.oauth2.credentials
        from get_token import YouTubeTokenManager

        token   = YouTubeTokenManager().get_valid_access_token()
        creds   = google.oauth2.credentials.Credentials(token=token)
        youtube = build("youtube", "v3", credentials=creds)

        body = {
            "snippet": {
                "title":            script.youtube_title,
                "description":      script.youtube_description,
                "tags":             script.youtube_tags[:15],
                "categoryId":       "27",
                "defaultLanguage":  "ar",
            },
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": True},
        }

        media   = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=5*1024*1024)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(f"📤 {int(status.progress()*100)}%")

        vid_id = response["id"]
        logger.info(f"✅ منشور: https://youtube.com/watch?v={vid_id}")

        # Thumbnail
        if Path(thumb_path).exists():
            try:
                youtube.thumbnails().set(
                    videoId=vid_id,
                    media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
                ).execute()
            except Exception as e:
                logger.warning(f"⚠️ Thumbnail فشل: {e}")

        return vid_id

    # ──────────────────────────── Main Pipeline ─
    def run(self, episode_number: int) -> bool:
        logger.info(f"\n{'═'*60}")
        logger.info(f"▶ بدء تنفيذ الحلقة {episode_number}")
        logger.info(f"{'═'*60}")

        ep_dir = str(Paths.EPISODES / f"ep_{episode_number:03d}")
        Path(ep_dir).mkdir(parents=True, exist_ok=True)

        ep_id = self._db_init_episode(episode_number)

        try:
            # 1 — السكريبت
            self._db_update(ep_id, status=EpisodeStatus.SCRIPTING)
            script = self._stage_script(episode_number)
            self._db_update(ep_id, status=EpisodeStatus.AUDIO, surah_name=script.surah_name, title=script.title)

            # 2 — الصوت
            self._stage_audio(script, ep_dir)
            self._db_update(ep_id, status=EpisodeStatus.VISUAL)

            # 3 — الصور
            self._stage_visuals(script, ep_dir)
            self._db_update(ep_id, status=EpisodeStatus.VIDEO)

            # 4 — الفيديو
            video_path = self._stage_video(script, ep_dir)
            self._db_update(ep_id, status=EpisodeStatus.THUMBNAIL, video_path=video_path)

            # 5 — Thumbnail
            thumb_path = self._stage_thumbnail(script, ep_dir)
            self._db_update(ep_id, status=EpisodeStatus.UPLOADING, thumbnail_path=thumb_path)

            # 6 — النشر
            vid_id  = self._stage_upload(script, video_path, thumb_path)
            yt_url  = f"https://youtube.com/watch?v={vid_id}"
            self._db_update(
                ep_id,
                status=EpisodeStatus.PUBLISHED,
                youtube_video_id=vid_id,
                youtube_url=yt_url,
                published_at=datetime.now(timezone.utc).isoformat(),
            )

            logger.info(f"\n🎉 الحلقة {episode_number} نُشرت: {yt_url}")
            return True

        except Exception as exc:
            tb = traceback.format_exc()
            logger.error(f"❌ فشلت الحلقة {episode_number}:\n{tb}")
            self._db_update(ep_id, status=EpisodeStatus.FAILED,
                            error_message=str(exc), error_traceback=tb[-1500:])
            return False

    def run_next(self) -> bool:
        ep = self._db_get_next()
        if not ep:
            logger.info("✅ لا توجد حلقات معلقة")
            return True
        return self.run(ep["episode_number"])

    def seed(self):
        """يُضيف جميع الحلقات بحالة pending"""
        from config import CURRICULUM
        for n in CURRICULUM:
            self._db_init_episode(n)
            logger.info(f"  ✓ حلقة {n}: {CURRICULUM[n]['name']}")
        logger.info("✅ تم البذر")
