"""
orchestrator.py — VALUE / QEEMA v3.0 (Enterprise Architecture)
═══════════════════════════════════════════════════════
قائد المنظومة الكاملة (The Master Controller)
• استئناف ذكي وموثوق 100% مع حفظ حالة السكريبت (State Persistence)
• تتبع زمني لأداء المحركات (Performance Metrics)
• تنظيف آلي للملفات المؤقتة لتوفير مساحة السيرفر (Garbage Collection)
• معالجة استثنائية شاملة وحفظ الحالة في Supabase
═══════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import logging
import traceback
import time
import shutil
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
    ينفّذ خط الإنتاج الكامل بصلابة المؤسسات الكبرى:
    - حفظ تدريجي للحالة محلياً وسحابياً
    - إدارة الذاكرة والملفات المؤقتة
    """

    def __init__(self):
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    def _init_supabase(self):
        self.db: Client = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
        logger.info("✅ Supabase متصل ومستعد لتسجيل العمليات")

    def _init_engines(self):
        logger.info("🔧 تهيئة وإحماء المحركات (Engines Boot-up)…")
        self.script    = ScriptEngine()
        self.voice     = VoiceEngine()
        self.visual    = VisualEngine()
        self.sfx       = SFXEngine()
        self.gamify    = GamificationEngine()
        self.video     = VideoEngine()
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ جميع المحركات في وضع الاستعداد الأقصى")

    # ──────────────────────────── State Management ─
    def _db_get_next(self) -> Optional[dict]:
        r = (self.db.table(DBConfig.TABLE_EPISODES)
             .select("*")
             .eq("status", "pending")
             .order("episode_number")
             .limit(1)
             .execute())
        return r.data[0] if r.data else None

    def _db_update(self, ep_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()

        if "status" in fields and hasattr(fields["status"], "value"):
            fields["status"] = fields["status"].value.lower()

        try:
            self.db.table(DBConfig.TABLE_EPISODES).update(fields).eq("id", ep_id).execute()
        except Exception as e:
            logger.warning(f"⚠️ تحذير: فشل مزامنة الحالة مع Supabase: {e}")

    def _db_init_episode(self, ep_num: int) -> str:
        r = (self.db.table(DBConfig.TABLE_EPISODES)
             .select("id,status")
             .eq("episode_number", ep_num)
             .execute())
        if r.data:
            return r.data[0]["id"]

        res = self.db.table(DBConfig.TABLE_EPISODES).insert({
            "episode_number": ep_num,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return res.data[0]["id"]

    def _save_script_state(self, script: EpisodeScript) -> None:
        """
        [ترقية حيوية]: يحفظ حالة السكريبت في كل مرحلة 
        ليضمن عدم ضياع مسارات الملفات (الصور والصوت) عند الانقطاع.
        """
        save_path = Paths.SCRIPT_DIR / f"episode_{script.episode_number:03d}.json"
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        logger.debug(f"💾 تم حفظ حالة السكريبت محلياً بنجاح.")

    # ──────────────────────────── Core Stages ──────
    def _stage_script(self, ep_num: int) -> EpisodeScript:
        """مرحلة 1: هندسة السكريبت (Scripting)"""
        logger.info("📝 [المرحلة 1]: توليد السكريبت...")
        cached = self.script.load_from_disk(ep_num)
        if cached:
            logger.info("♻️ استئناف: تم العثور على سكريبت جاهز على القرص.")
            return cached
        return self.script.generate(ep_num)

    def _stage_audio(self, script: EpisodeScript, ep_dir: str) -> dict:
        """مرحلة 2: الإنتاج الصوتي (Voice & SFX)"""
        logger.info("🎙️ [المرحلة 2]: هندسة الصوت والمؤثرات...")
        audio_map_file = Path(ep_dir) / "audio_map.json"
        
        # قراءة آمنة
        if audio_map_file.exists():
            try:
                raw = json.loads(audio_map_file.read_text())
                if isinstance(raw, dict) and all(Path(str(p)).exists() for p in raw.values()):
                    logger.info("♻️ استئناف: تم استرجاع خريطة الصوت من القرص.")
                    self._update_script_audio_paths(script, raw) # تحديث السكريبت في الذاكرة
                    return raw
            except Exception as e:
                logger.warning(f"⚠️ ملف audio_map.json غير صالح، سيتم إعادة التوليد. الخطأ: {e}")

        # التوليد الفعلي
        audio_map = self.voice.generate_episode_audio(script, ep_dir)
        
        if not isinstance(audio_map, dict):
            logger.error(f"❌ خطأ حرج: محرك الصوت أعاد {type(audio_map)} بدلاً من dict.")
            raise ValueError("مخرجات محرك الصوت غير متوافقة برمجياً.")

        audio_map_file.write_text(json.dumps(audio_map, ensure_ascii=False))

        # دمج المؤثرات
        processed = self.sfx.process_all(audio_map, script, ep_dir)
        self._update_script_audio_paths(script, processed)
        
        # حفظ السكريبت المُحدث (تأمين المسارات)
        self._save_script_state(script) 
        return processed

    def _update_script_audio_paths(self, script: EpisodeScript, audio_map: dict) -> None:
        """يحدّث مسارات الصوت في السكريبت"""
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
        """مرحلة 3: الإنتاج البصري (Visuals & Infographics)"""
        logger.info("🎨 [المرحلة 3]: توليد الإنفوجرافيك البصري...")
        vis_map_file = Path(ep_dir) / "visuals_map.json"
        
        if vis_map_file.exists():
            logger.info("♻️ استئناف: تم استرجاع خريطة الصور من القرص.")
            vis_map = json.loads(vis_map_file.read_text())
            
            # ربط المسارات الموجودة
            if vis_map.get("intro") and Path(vis_map["intro"]).exists():
                script.intro_scene.image_path = vis_map["intro"]
            if vis_map.get("outro") and Path(vis_map["outro"]).exists():
                script.outro_scene.image_path = vis_map["outro"]
            for sc in script.ayah_scenes:
                k = f"ayah_{sc.scene_id}"
                if vis_map.get(k) and Path(vis_map[k]).exists():
                    sc.image_path = vis_map[k]
            for sc in script.mid_scenes:
                k = f"mid_{sc.scene_id}"
                if vis_map.get(k) and Path(vis_map[k]).exists():
                    sc.image_path = vis_map[k]
            return

        # توليد الصور (والذي يقوم بتحديث كائن السكريبت داخلياً)
        self.visual.generate_episode_visuals(script, ep_dir)

        # حفظ الخريطة
        vis_map = {"intro": script.intro_scene.image_path, "outro": script.outro_scene.image_path}
        for sc in script.ayah_scenes:
            vis_map[f"ayah_{sc.scene_id}"] = sc.image_path
        for sc in script.mid_scenes:
            vis_map[f"mid_{sc.scene_id}"] = sc.image_path
            
        vis_map_file.write_text(json.dumps(vis_map, ensure_ascii=False))
        
        # حفظ السكريبت المُحدث (تأمين المسارات)
        self._save_script_state(script)

    def _stage_video(self, script: EpisodeScript, ep_dir: str) -> str:
        """مرحلة 4: المونتاج وتجميع الفيديو (Video Assembly)"""
        logger.info("🎬 [المرحلة 4]: تجميع ومونتاج الفيديو...")
        final_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_final.mp4"
        if final_path.exists():
            logger.info("♻️ استئناف: تم العثور على الفيديو النهائي مسبقاً.")
            return str(final_path)
        return self.video.assemble_episode(script, ep_dir)

    def _stage_thumbnail(self, script: EpisodeScript, ep_dir: str) -> str:
        """مرحلة 5: الغلاف المصغر (Thumbnail)"""
        logger.info("🖼️ [المرحلة 5]: تصميم الغلاف المصغر...")
        thumb_path = Paths.THUMBNAILS / f"ep_{script.episode_number:03d}.jpg"
        if thumb_path.exists():
            logger.info("♻️ استئناف: تم العثور على الغلاف مسبقاً.")
            return str(thumb_path)

        scene_img = script.intro_scene.image_path
        return self.thumbnail.create(script, script.episode_number, scene_img)

    def _stage_upload(self, script: EpisodeScript, video_path: str, thumb_path: str) -> str:
        """مرحلة 6: النشر على YouTube"""
        logger.info("📤 [المرحلة 6]: رفع الفيديو على منصة YouTube...")
        dry = __import__("os").environ.get("DRY_RUN", "false").lower() == "true"
        if dry:
            logger.info("🧪 نظام DRY_RUN مُفعل — سيتم تجاوز الرفع الفعلي.")
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
        retries = 0
        max_retries = 5

        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"📤 تقدم الرفع: {int(status.progress()*100)}%")
            except Exception as e:
                retries += 1
                if retries > max_retries:
                    raise e
                logger.warning(f"⚠️ انقطاع اتصال أثناء الرفع، إعادة المحاولة ({retries}/{max_retries})...")
                time.sleep(10 * retries)

        vid_id = response["id"]
        logger.info(f"✅ تم نشر الفيديو بنجاح: https://youtube.com/watch?v={vid_id}")

        if Path(thumb_path).exists():
            try:
                logger.info("🖼️ جاري رفع الغلاف المصغر...")
                youtube.thumbnails().set(
                    videoId=vid_id,
                    media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
                ).execute()
            except Exception as e:
                logger.warning(f"⚠️ فشل رفع الغلاف: {e}")

        return vid_id

    def _cleanup_temp_files(self, ep_dir: str):
        """تنظيف الملفات المؤقتة لتوفير مساحة القرص بعد النشر الناجح"""
        seg_dir = Path(ep_dir) / "segments"
        if seg_dir.exists():
            try:
                shutil.rmtree(seg_dir)
                logger.info("🧹 تم تنظيف المقاطع المؤقتة (Garbage Collection) لتوفير المساحة.")
            except Exception as e:
                logger.warning(f"⚠️ لم يتم تنظيف الملفات المؤقتة: {e}")

    # ──────────────────────────── Main Pipeline ─
    def run(self, episode_number: int) -> bool:
        start_time = time.time()
        logger.info(f"\n{'═'*60}")
        logger.info(f"▶ بدء تشغيل خط الإنتاج للحلقة {episode_number}")
        logger.info(f"{'═'*60}")

        ep_dir = str(Paths.EPISODES / f"ep_{episode_number:03d}")
        Path(ep_dir).mkdir(parents=True, exist_ok=True)

        ep_id = self._db_init_episode(episode_number)

        try:
            # 1. Scripting
            t0 = time.time()
            self._db_update(ep_id, status=EpisodeStatus.SCRIPTING)
            script = self._stage_script(episode_number)
            logger.info(f"⏱️ اكتملت هندسة السكريبت في {time.time()-t0:.1f} ثانية")

            # 2. Audio
            t0 = time.time()
            self._db_update(ep_id, status=EpisodeStatus.AUDIO, surah_name=script.surah_name, title=script.title)
            self._stage_audio(script, ep_dir)
            logger.info(f"⏱️ اكتمل الإنتاج الصوتي في {time.time()-t0:.1f} ثانية")

            # 3. Visuals
            t0 = time.time()
            self._db_update(ep_id, status=EpisodeStatus.VISUAL)
            self._stage_visuals(script, ep_dir)
            logger.info(f"⏱️ اكتمل الإنتاج البصري في {time.time()-t0:.1f} ثانية")

            # 4. Video Assembly
            t0 = time.time()
            self._db_update(ep_id, status=EpisodeStatus.VIDEO)
            video_path = self._stage_video(script, ep_dir)
            self._db_update(ep_id, status=EpisodeStatus.THUMBNAIL, video_path=video_path)
            logger.info(f"⏱️ اكتمل المونتاج في {time.time()-t0:.1f} ثانية")

            # 5. Thumbnail
            t0 = time.time()
            thumb_path = self._stage_thumbnail(script, ep_dir)
            self._db_update(ep_id, status=EpisodeStatus.UPLOADING, thumbnail_path=thumb_path)

            # 6. Upload
            t0 = time.time()
            vid_id  = self._stage_upload(script, video_path, thumb_path)
            logger.info(f"⏱️ اكتمل النشر في {time.time()-t0:.1f} ثانية")
            
            yt_url  = f"https://youtube.com/watch?v={vid_id}"
            self._db_update(
                ep_id,
                status=EpisodeStatus.PUBLISHED,
                youtube_video_id=vid_id,
                youtube_url=yt_url,
                published_at=datetime.now(timezone.utc).isoformat(),
            )

            # 7. Cleanup
            self._cleanup_temp_files(ep_dir)

            total_time = (time.time() - start_time) / 60
            logger.info(f"\n🎉 الحلقة {episode_number} نُشرت بنجاح!")
            logger.info(f"🔗 الرابط: {yt_url}")
            logger.info(f"⏱️ الوقت الإجمالي للإنتاج: {total_time:.1f} دقيقة")
            logger.info(f"{'═'*60}\n")
            return True

        except Exception as exc:
            tb = traceback.format_exc()
            logger.error(f"❌ فشلت المنظومة في معالجة الحلقة {episode_number}:\n{tb}")
            self._db_update(ep_id, status=EpisodeStatus.FAILED,
                            error_message=str(exc), error_traceback=tb[-1500:])
            return False

    def run_next(self) -> bool:
        ep = self._db_get_next()
        if not ep:
            logger.info("✅ لا توجد حلقات معلقة في قاعدة البيانات للإنتاج.")
            return True
        return self.run(ep["episode_number"])

    def seed(self):
        """يُضيف جميع الحلقات بحالة pending"""
        from config import CURRICULUM
        for n in CURRICULUM:
            self._db_init_episode(n)
            logger.info(f"  ✓ تم التسجيل: حلقة {n}: {CURRICULUM[n]['name']}")
        logger.info("✅ تمت تهيئة قاعدة البيانات بجميع الحلقات.")
