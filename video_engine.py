"""
video_engine.py — VALUE / QEEMA v4.0
محرك تجميع الفيديو: يقوم بدمج الصور والصوت والمقاطع في فيديو نهائي.
"""

import logging
import subprocess as sp
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class VideoEngine:
    """
    يقوم بتجميع الفيديو النهائي من:
    - الصور الثابتة (الإنفوجرافيك)
    - المقاطع الصوتية
    - خلفية الفيديو (اختياري)
    """

    def __init__(self):
        self.ffmpeg_path = "ffmpeg"
        self.ffprobe_path = "ffprobe"

    def assemble_episode(self, script, ep_dir: str) -> str:
        """
        تجميع الفيديو النهائي للحلقة.
        """
        logger.info(f"🎬 Assembling video for episode {script.episode_number}")
        
        # تحديد مسار الإخراج
        output_path = Path(ep_dir).parent / "videos" / f"ep_{script.episode_number:03d}_raw.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # بناء قائمة المشاهد
        scenes = self._build_scene_list(script, ep_dir)
        
        # تجميع الفيديو
        success = self._concat_videos(scenes, str(output_path))
        
        if success:
            logger.info(f"✅ Video assembled successfully: {output_path}")
            return str(output_path)
        else:
            logger.error("❌ Video assembly failed")
            raise RuntimeError("Video assembly failed")

    def _build_scene_list(self, script, ep_dir: str) -> List[Dict[str, str]]:
        """
        بناء قائمة المشاهد مع مسارات الصور والصوت.
        """
        scenes = []
        
        # Intro scene
        intro_img = script.intro_scene.image_path
        intro_audio = script.intro_scene.audio_path
        if intro_img and intro_audio:
            scenes.append({
                "image": intro_img,
                "audio": intro_audio,
                "duration": self._get_audio_duration(intro_audio)
            })
        
        # Ayah scenes
        for scene in script.ayah_scenes:
            img = scene.image_path
            intro_audio = scene.intro_audio
            explain_audio = scene.explain_audio
            
            if img and intro_audio:
                scenes.append({
                    "image": img,
                    "audio": intro_audio,
                    "duration": self._get_audio_duration(intro_audio)
                })
            
            if img and explain_audio:
                scenes.append({
                    "image": img,
                    "audio": explain_audio,
                    "duration": self._get_audio_duration(explain_audio)
                })
        
        # Outro scene
        outro_img = script.outro_scene.image_path
        outro_audio = script.outro_scene.audio_path
        if outro_img and outro_audio:
            scenes.append({
                "image": outro_img,
                "audio": outro_audio,
                "duration": self._get_audio_duration(outro_audio)
            })
        
        return scenes

    def _get_audio_duration(self, audio_path: str) -> float:
        """
        استخراج مدة المقطع الصوتي باستخدام ffprobe.
        """
        try:
            cmd = [
                self.ffprobe_path, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path
            ]
            result = sp.run(cmd, capture_output=True, text=True, timeout=10)
            duration = float(result.stdout.strip())
            return duration
        except Exception as e:
            logger.warning(f"⚠️ Failed to get duration for {audio_path}: {e}")
            return 5.0  # قيمة افتراضية

    def _concat_videos(self, scenes: List[Dict[str, str]], output_path: str) -> bool:
        """
        دمج المشاهد باستخدام ffmpeg complex filter.
        """
        if not scenes:
            logger.error("❌ No scenes to assemble")
            return False
        
        # بناء سلسلة الفلاتر
        filter_parts = []
        inputs = []
        
        for i, scene in enumerate(scenes):
            img_path = scene["image"]
            audio_path = scene["audio"]
            duration = scene["duration"]
            
            # إضافة المدخلات
            inputs.extend(["-loop", "1", "-t", str(duration), "-i", img_path])
            inputs.extend(["-i", audio_path])
            
            # بناء الفلتر لكل مشهد
            video_label = f"v{i}"
            audio_label = f"a{i}"
            
            filter_parts.append(
                f"[{i*2}:v]scale=1920:1080:force_original_aspect_ratio=1,"
                f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
                f"setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.5,fade=t=out:st={duration-0.5}:d=0.5[{video_label}]"
            )
            
            filter_parts.append(
                f"[{i*2+1}:a]adelay=0,aresample=async=1[{audio_label}]"
            )
        
        # دمج جميع المشاهد
        video_streams = "".join([f"[v{i}]" for i in range(len(scenes))])
        audio_streams = "".join([f"[a{i}]" for i in range(len(scenes))])
        
        filter_parts.append(
            f"{video_streams}concat=n={len(scenes)}:v=1:a=0[outv]"
        )
        filter_parts.append(
            f"{audio_streams}concat=n={len(scenes)}:v=0:a=1[outa]"
        )
        
        filter_complex = ";".join(filter_parts)
        
        # أمر ffmpeg
        cmd = [
            self.ffmpeg_path, "-y"
        ] + inputs + [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            output_path
        ]
        
        try:
            logger.info(f"Running ffmpeg command (length: {len(cmd)} args)")
            result = sp.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                # ✅ تم إصلاح الخطأ: السلسلة النصية أصبحت في سطر واحد
                logger.error(f"❌ ffmpeg failed with code {result.returncode}: {result.stderr[:500]}")
                return False
            return True
        except sp.TimeoutExpired:
            logger.error("❌ ffmpeg timeout after 300 seconds")
            return False
        except Exception as e:
            logger.error(f"❌ ffmpeg exception: {e}")
            return False

    def create_video_from_image_audio(self, image_path: str, audio_path: str, output_path: str) -> bool:
        """
        إنشاء فيديو بسيط من صورة واحدة ومقطع صوتي.
        """
        if not Path(image_path).exists():
            logger.error(f"❌ Image not found: {image_path}")
            return False
        
        if not Path(audio_path).exists():
            logger.error(f"❌ Audio not found: {audio_path}")
            return False
        
        duration = self._get_audio_duration(audio_path)
        
        cmd = [
            self.ffmpeg_path, "-y",
            "-loop", "1",
            "-i", image_path,
            "-i", audio_path,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=1,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "-t", str(duration),
            output_path
        ]
        
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                # ✅ تم إصلاح الخطأ هنا أيضاً
                logger.error(f"❌ ffmpeg failed: {result.stderr[:300]}")
                return False
            return True
        except Exception as e:
            logger.error(f"❌ Exception: {e}")
            return False