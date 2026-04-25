"""
video_engine.py — VALUE / QEEMA v5.0
======================================
محرك تجميع الفيديو الاحترافي:
  - يبني segment لكل scene (image + audio + Ken Burns zoom + subtitle)
  - يدمج كل الـ segments بـ concat
  - يضيف موسيقى خلفية هادئة (-30dB)
  - يكتب الآيات كـ subtitle محترم على الشاشة
  - يستخدم خط Amiri-Bold للعربية
"""

import logging
import os
import shutil
import subprocess as sp
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from config import VideoConfig, Paths

if TYPE_CHECKING:
    from models import EpisodeScript, AyahScene, NarratorScene

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════
def _run(cmd: List[str], timeout: int = 600) -> bool:
    """تشغيل ffmpeg بأمان."""
    try:
        result = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.error(f"ffmpeg failed: {result.stderr[-500:]}")
            return False
        return True
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        return False


def _probe_duration(path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        r = sp.run(cmd, capture_output=True, text=True, timeout=15)
        return float(r.stdout.strip()) if r.stdout.strip() else 0.0
    except Exception:
        return 0.0


def _escape_drawtext(s: str) -> str:
    """Escape special chars for ffmpeg drawtext."""
    return (s.replace("\\", "\\\\")
             .replace(":", "\\:")
             .replace("'", "\\'")
             .replace(",", "\\,"))


def _font_path() -> str:
    """العثور على الخط العربي."""
    candidates = [
        Paths.FONTS / "Amiri-Bold.ttf",
        Paths.FONTS / "NotoSansArabic-Bold.ttf",
        Path("/usr/share/fonts/truetype/hosny-amiri/Amiri-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for c in candidates:
        if Path(c).exists():
            return str(c)
    return ""


# ════════════════════════════════════════════════════════════════
# VideoEngine
# ════════════════════════════════════════════════════════════════
class VideoEngine:
    """محرك تجميع الفيديو الكامل."""

    def __init__(self):
        self.font = _font_path()
        if self.font:
            logger.info(f"✅ Font: {self.font}")
        else:
            logger.warning("⚠️ No Arabic font found — subtitles will be plain")
        self.bg_music = self._find_bg_music()

    def _find_bg_music(self) -> Optional[str]:
        candidates = [Paths.OVERLAYS / "bgm.mp3", Paths.ASSETS / "bgm.mp3"]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    # ─────────────────────────────────────────────────────────────
    # Segment builders
    # ─────────────────────────────────────────────────────────────
    def _build_segment(
        self,
        image_path: str,
        audio_path: str,
        output: str,
        subtitle: Optional[str] = None,
        ken_burns: bool = True,
    ) -> bool:
        """
        بناء segment واحد:
          - صورة ثابتة (مع Ken Burns zoom-in بسيط) لمدة الصوت
          - صوت مدمج
          - subtitle اختياري في الأسفل (للآيات)
        """
        if not Path(image_path).exists():
            logger.error(f"❌ Image missing: {image_path}")
            return False
        if not Path(audio_path).exists():
            logger.error(f"❌ Audio missing: {audio_path}")
            return False

        duration = _probe_duration(audio_path)
        if duration <= 0:
            logger.error(f"❌ Audio has no duration: {audio_path}")
            return False

        W, H = VideoConfig.RESOLUTION_WIDTH, VideoConfig.RESOLUTION_HEIGHT
        fps = VideoConfig.FPS

        # Ken Burns: zoom from 1.0 to 1.08 over duration
        zoompan = (
            f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,"
            f"crop={W*2}:{H*2},"
            f"zoompan=z='min(zoom+0.0008,1.08)':d={int(duration*fps)}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}"
        )
        if not ken_burns:
            zoompan = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"

        # Subtitle filter (for Quran ayahs)
        sub_filter = ""
        if subtitle and self.font:
            esc = _escape_drawtext(subtitle)
            # box في الأسفل، خط ذهبي على خلفية شبه شفافة
            sub_filter = (
                f",drawtext=fontfile='{self.font}':text='{esc}':"
                f"fontsize=44:fontcolor=#FFD700:"
                f"box=1:boxcolor=black@0.55:boxborderw=20:"
                f"x=(w-text_w)/2:y=h-180"
            )

        vf = zoompan + sub_filter

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
            "-vf", vf,
            "-c:v", VideoConfig.CODEC,
            "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF),
            "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT,
            "-c:a", VideoConfig.AUDIO_CODEC,
            "-b:a", VideoConfig.AUDIO_BITRATE,
            "-r", str(fps),
            "-t", f"{duration:.2f}",
            "-shortest",
            output,
        ]
        return _run(cmd, timeout=300)

    def _build_silence(self, duration: float, output: str) -> bool:
        """بناء صوت صمت لـ padding بين الـ segments."""
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
            "-t", f"{duration:.2f}",
            "-c:a", "aac", "-b:a", "128k",
            output,
        ]
        return _run(cmd, timeout=30)

    # ─────────────────────────────────────────────────────────────
    # Concat
    # ─────────────────────────────────────────────────────────────
    def _concat_segments(self, segments: List[str], output: str) -> bool:
        """ربط كل الـ segments باستخدام concat demuxer."""
        list_file = Path(output).parent / "concat_list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"file '{Path(seg).absolute()}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            output,
        ]
        ok = _run(cmd, timeout=300)

        # حذف list file
        try:
            list_file.unlink()
        except Exception:
            pass

        return ok

    def _add_bgm(self, video_path: str, output: str) -> bool:
        """إضافة موسيقى خلفية هادئة."""
        if not self.bg_music:
            logger.info("ℹ️ No BGM file — skipping")
            shutil.copy(video_path, output)
            return True

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-stream_loop", "-1", "-i", self.bg_music,
            "-filter_complex",
            "[1:a]volume=0.06[bgm];"  # -24 dB roughly
            "[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output,
        ]
        return _run(cmd, timeout=300)

    # ─────────────────────────────────────────────────────────────
    # Main entry
    # ─────────────────────────────────────────────────────────────
    def assemble_episode(self, script: "EpisodeScript", ep_dir: str) -> str:
        logger.info(f"🎬 Assembling video for episode {script.episode_number}")

        ep_path = Path(ep_dir)
        seg_dir = ep_path / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)

        segments: List[str] = []
        seg_idx = 0

        # ── Intro segment
        if script.intro_scene.image_path and script.intro_scene.audio_path:
            seg = str(seg_dir / f"seg_{seg_idx:03d}_intro.mp4")
            if self._build_segment(
                script.intro_scene.image_path,
                script.intro_scene.audio_path,
                seg,
            ):
                segments.append(seg)
                seg_idx += 1

        # ── Ayah scenes (each: intro narration → recitation with subtitle → explanation)
        for scene in script.ayah_scenes:
            if not scene.image_path:
                logger.warning(f"⚠️ Skipping ayah scene {scene.scene_id} — no image")
                continue

            # 1) Intro narration
            if scene.intro_audio:
                seg = str(seg_dir / f"seg_{seg_idx:03d}_ayah{scene.scene_id}_intro.mp4")
                if self._build_segment(scene.image_path, scene.intro_audio, seg):
                    segments.append(seg)
                    seg_idx += 1

            # 2) Quran recitation WITH ayah text as subtitle
            if scene.ayah_audio:
                seg = str(seg_dir / f"seg_{seg_idx:03d}_ayah{scene.scene_id}_recite.mp4")
                if self._build_segment(
                    scene.image_path,
                    scene.ayah_audio,
                    seg,
                    subtitle=scene.ayah.text,
                ):
                    segments.append(seg)
                    seg_idx += 1

            # 3) Explanation
            if scene.explain_audio:
                seg = str(seg_dir / f"seg_{seg_idx:03d}_ayah{scene.scene_id}_explain.mp4")
                if self._build_segment(scene.image_path, scene.explain_audio, seg):
                    segments.append(seg)
                    seg_idx += 1

        # ── Mid scenes
        for sc in script.mid_scenes:
            if sc.image_path and sc.audio_path:
                seg = str(seg_dir / f"seg_{seg_idx:03d}_mid_{sc.scene_id}.mp4")
                if self._build_segment(sc.image_path, sc.audio_path, seg):
                    segments.append(seg)
                    seg_idx += 1

        # ── Outro segment
        if script.outro_scene.image_path and script.outro_scene.audio_path:
            seg = str(seg_dir / f"seg_{seg_idx:03d}_outro.mp4")
            if self._build_segment(
                script.outro_scene.image_path,
                script.outro_scene.audio_path,
                seg,
            ):
                segments.append(seg)
                seg_idx += 1

        if not segments:
            logger.error("❌ No segments produced — aborting video assembly")
            output_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.touch()
            return str(output_path)

        logger.info(f"📦 Built {len(segments)} segments — concatenating...")

        # ── Concat
        merged = ep_path / "merged.mp4"
        if not self._concat_segments(segments, str(merged)):
            logger.error("❌ Concat failed")
            output_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(segments[0], output_path)
            return str(output_path)

        # ── Add BGM
        output_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not self._add_bgm(str(merged), str(output_path)):
            logger.warning("⚠️ BGM step failed, using merged video as-is")
            shutil.copy(merged, output_path)

        # cleanup merged
        try:
            merged.unlink()
        except Exception:
            pass

        logger.info(f"✅ Episode video ready: {output_path}")
        return str(output_path)
