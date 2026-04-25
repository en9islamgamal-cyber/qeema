"""
video_engine.py — VALUE / QEEMA v5.2
======================================
[CHANGELOG v5.2]
- إصلاح: صوت التلاوة كان مقصوص — تم إضافة tail_pad لكل الأصوات وإزالة -shortest
  من مشاهد التلاوة، والاعتماد على -t (مدة الصوت + 0.3 ثانية buffer)
- إضافة: overlay الشعار (logo.png) على جميع المشاهد عبر ffmpeg
- تحسين: الانترو/الأوترو من BrandEngine (مرة واحدة، لا إعادة توليد)
- تحسين: جميع الـ segments تستخدم نفس الإعدادات لضمان التوافق في concat
"""

import logging, os, shutil, subprocess as sp
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from config import VideoConfig, Paths

if TYPE_CHECKING:
    from models import EpisodeScript

logger = logging.getLogger(__name__)
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════
def _run(cmd: List[str], timeout: int = 600) -> bool:
    try:
        r = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error(f"ffmpeg failed:\n{r.stderr[-600:]}")
            return False
        return True
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        return False


def _probe_duration(path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "error",
               "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        r = sp.run(cmd, capture_output=True, text=True, timeout=15)
        v = r.stdout.strip()
        return float(v) if v and v != "N/A" else 0.0
    except Exception:
        return 0.0


def _escape_dt(s: str) -> str:
    return (s.replace("\\", "\\\\")
             .replace(":", "\\:")
             .replace("'", "\\'")
             .replace(",", "\\,"))


def _find_font() -> str:
    for c in [Paths.FONTS / "Amiri-Bold.ttf",
               Paths.FONTS / "NotoSansArabic-Bold.ttf",
               Path("/usr/share/fonts/truetype/hosny-amiri/Amiri-Regular.ttf"),
               Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]:
        if Path(c).exists():
            return str(c)
    return ""


def _find_logo() -> Optional[str]:
    for c in [Paths.ASSETS / "logo.png", Paths.ASSETS / "logo.jpg",
               Path("logo.png"), Path("logo.jpg")]:
        if Path(c).exists():
            return str(c)
    return None


def _find_bgm() -> Optional[str]:
    for c in [Paths.OVERLAYS / "bgm.mp3", Paths.ASSETS / "bgm.mp3"]:
        if c.exists():
            return str(c)
    return None


# ══════════════════════════════════════════════════════════════════
# VideoEngine
# ══════════════════════════════════════════════════════════════════
class VideoEngine:

    def __init__(self):
        self.W, self.H = VideoConfig.RESOLUTION_WIDTH, VideoConfig.RESOLUTION_HEIGHT
        self.fps       = VideoConfig.FPS
        self.font      = _find_font()
        self.logo      = _find_logo()
        self.bg_music  = _find_bgm()

        if self.font:
            logger.info(f"✅ Font: {self.font}")
        if self.logo:
            logger.info(f"✅ Logo: {self.logo}")
        if not self.bg_music:
            logger.info("ℹ️ لا يوجد ملف bgm.mp3 — بدون موسيقى خلفية")

    # ─────────────────────────────────────────────────────────────
    # Logo Overlay filter (يُضاف في نهاية أي vf chain)
    # ─────────────────────────────────────────────────────────────
    def _logo_overlay_filter(self) -> str:
        """يُعيد filter_complex جاهز لـ overlay الشعار أسفل اليمين."""
        if not self.logo:
            return ""
        # الشعار: 80px عرض، أسفل اليمين، شفافية 70%
        return (
            f";[tmp_video][logo]overlay="
            f"x=W-w-25:y=H-h-25:"
            f"format=auto"
        )

    # ─────────────────────────────────────────────────────────────
    # Router
    # ─────────────────────────────────────────────────────────────
    def _build_segment(self, scene_path: str, audio_path: str,
                       output: str, subtitle: Optional[str] = None,
                       is_recitation: bool = False) -> bool:
        if not Path(scene_path).exists():
            logger.error(f"❌ مشهد غير موجود: {scene_path}")
            return False
        if not Path(audio_path).exists():
            logger.error(f"❌ صوت غير موجود: {audio_path}")
            return False

        if Path(scene_path).suffix.lower() in VIDEO_EXT:
            return self._build_from_video_clip(scene_path, audio_path, output,
                                               subtitle, is_recitation)
        else:
            return self._build_from_image(scene_path, audio_path, output,
                                          subtitle, is_recitation)

    # ─────────────────────────────────────────────────────────────
    # من صورة ثابتة (Ken Burns)
    # ─────────────────────────────────────────────────────────────
    def _build_from_image(self, image_path: str, audio_path: str,
                          output: str, subtitle: Optional[str] = None,
                          is_recitation: bool = False) -> bool:
        """
        Ken Burns على صورة ثابتة.
        is_recitation=True → buffer إضافي لضمان عدم قطع التلاوة.
        """
        audio_dur = _probe_duration(audio_path)
        if audio_dur <= 0:
            logger.error(f"❌ مدة الصوت = 0: {audio_path}")
            return False

        # ─ buffer: +0.5s للتلاوة، +0.1s للباقي
        render_dur = audio_dur + (0.5 if is_recitation else 0.1)
        W, H, fps  = self.W, self.H, self.fps

        # ─ Ken Burns
        zoompan = (
            f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,"
            f"crop={W*2}:{H*2},"
            f"zoompan=z='min(zoom+0.0006,1.06)':d={int(render_dur*fps)}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps},"
            f"setsar=1"
        )

        # ─ subtitle للآيات
        sub = ""
        if subtitle and self.font:
            esc = _escape_dt(subtitle)
            sub = (
                f",drawtext=fontfile='{self.font}':text='{esc}':"
                f"fontsize=42:fontcolor=#FFD700:"
                f"box=1:boxcolor=black@0.6:boxborderw=18:"
                f"x=(w-text_w)/2:y=h-170"
            )

        # ─ fade in/out
        fade = f",fade=t=in:st=0:d=0.3,fade=t=out:st={audio_dur-0.3}:d=0.3"

        vf = zoompan + sub + fade

        # ─ Logo overlay
        if self.logo:
            filter_complex = (
                f"[0:v]{vf}[tmp_video];"
                f"[2:v]scale=80:-1:flags=lanczos,format=rgba,"
                f"colorchannelmixer=aa=0.7[logo]"
                f"{self._logo_overlay_filter()}"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", image_path,
                "-i", audio_path,
                "-i", self.logo,
                "-filter_complex", filter_complex,
                "-map", "[tmp_video]" if not self.logo else
                        filter_complex.split("[")[-1].rstrip("]"),
            ]
            # نبسط — نستخدم -vf بدل filter_complex لو لا يوجد شعار لمنع التعقيد
            # إعادة بناء بدون logo لو filter_complex معقد جداً
            cmd = self._build_image_cmd_with_logo(
                image_path, audio_path, zoompan + sub + fade,
                render_dur, audio_dur
            )
        else:
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
                "-ar", "44100", "-ac", "2",
                "-r", str(fps),
                "-t", f"{render_dur:.3f}",
                output,
            ]

        return _run(cmd, timeout=300)

    def _build_image_cmd_with_logo(self, image_path, audio_path,
                                    vf_chain, render_dur, audio_dur):
        """يبني ffmpeg cmd مع logo overlay."""
        W, H, fps = self.W, self.H, self.fps
        fc = (
            f"[0:v]{vf_chain}[base];"
            f"[2:v]scale=80:-1:flags=lanczos,format=rgba,"
            f"colorchannelmixer=aa=0.7[logo];"
            f"[base][logo]overlay=x=W-w-25:y=H-h-25[out]"
        )
        return [
            "ffmpeg", "-y",
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
            "-i", self.logo,
            "-filter_complex", fc,
            "-map", "[out]", "-map", "1:a",
            "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT,
            "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
            "-ar", "44100", "-ac", "2",
            "-r", str(fps), "-t", f"{render_dur:.3f}",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
        ]

    # ─────────────────────────────────────────────────────────────
    # من مقطع فيديو (Runway .mp4)
    # ─────────────────────────────────────────────────────────────
    def _build_from_video_clip(self, clip_path: str, audio_path: str,
                                output: str, subtitle: Optional[str] = None,
                                is_recitation: bool = False) -> bool:
        audio_dur = _probe_duration(audio_path)
        video_dur = _probe_duration(clip_path)
        if audio_dur <= 0:
            return False

        render_dur = audio_dur + (0.5 if is_recitation else 0.1)
        W, H, fps  = self.W, self.H, self.fps

        scale = (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        sub = ""
        if subtitle and self.font:
            esc = _escape_dt(subtitle)
            sub = (
                f",drawtext=fontfile='{self.font}':text='{esc}':"
                f"fontsize=42:fontcolor=#FFD700:"
                f"box=1:boxcolor=black@0.6:boxborderw=18:"
                f"x=(w-text_w)/2:y=h-170"
            )
        fade = f",fade=t=in:st=0:d=0.3,fade=t=out:st={audio_dur-0.3}:d=0.3"
        vf   = scale + sub + fade

        loop_flag = ["-stream_loop", "-1"] if render_dur > video_dur else []

        if self.logo:
            fc = (
                f"[0:v]{vf}[base];"
                f"[2:v]scale=80:-1:flags=lanczos,format=rgba,"
                f"colorchannelmixer=aa=0.7[logo];"
                f"[base][logo]overlay=x=W-w-25:y=H-h-25[out]"
            )
            cmd = [
                "ffmpeg", "-y",
                *loop_flag, "-i", clip_path,
                "-i", audio_path,
                "-i", self.logo,
                "-filter_complex", fc,
                "-map", "[out]", "-map", "1:a",
                "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
                "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
                "-pix_fmt", VideoConfig.PIX_FMT,
                "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
                "-ar", "44100", "-ac", "2",
                "-r", str(fps), "-t", f"{render_dur:.3f}",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                output,
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                *loop_flag, "-i", clip_path,
                "-i", audio_path,
                "-vf", vf,
                "-map", "0:v", "-map", "1:a",
                "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
                "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
                "-pix_fmt", VideoConfig.PIX_FMT,
                "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
                "-ar", "44100", "-ac", "2",
                "-r", str(fps), "-t", f"{render_dur:.3f}",
                "-avoid_negative_ts", "make_zero",
                output,
            ]
        return _run(cmd, timeout=300)

    # ─────────────────────────────────────────────────────────────
    # Concat
    # ─────────────────────────────────────────────────────────────
    def _concat_segments(self, segments: List[str], output: str) -> bool:
        list_file = Path(output).parent / "concat_list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"file '{Path(seg).absolute()}'\n")
        ok = _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            output,
        ], timeout=300)
        try:
            list_file.unlink()
        except Exception:
            pass
        return ok

    def _add_bgm(self, video_path: str, output: str) -> bool:
        if not self.bg_music:
            shutil.copy(video_path, output)
            return True
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-stream_loop", "-1", "-i", self.bg_music,
            "-filter_complex",
            "[1:a]volume=0.05[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=3[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100",
            "-shortest",
            output,
        ]
        return _run(cmd, timeout=300)

    # ─────────────────────────────────────────────────────────────
    # Main entry
    # ─────────────────────────────────────────────────────────────
    def assemble_episode(self, script: "EpisodeScript", ep_dir: str) -> str:
        logger.info(f"🎬 تجميع الحلقة {script.episode_number}...")
        ep_path  = Path(ep_dir)
        seg_dir  = ep_path / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        segs: List[str] = []
        idx = 0

        # ── Intro
        if script.intro_scene.image_path and script.intro_scene.audio_path:
            seg = str(seg_dir / f"seg_{idx:03d}_intro.mp4")
            if self._build_segment(script.intro_scene.image_path,
                                   script.intro_scene.audio_path, seg):
                segs.append(seg); idx += 1

        # ── Ayah scenes
        for scene in script.ayah_scenes:
            if not scene.image_path:
                logger.warning(f"⚠️ تخطي {scene.scene_id} — لا صورة")
                continue

            # 1) تمهيد الراوي
            if scene.intro_audio:
                seg = str(seg_dir / f"seg_{idx:03d}_s{scene.scene_id}_intro.mp4")
                if self._build_segment(scene.image_path, scene.intro_audio, seg):
                    segs.append(seg); idx += 1

            # 2) تلاوة — is_recitation=True لمنع القطع
            if scene.ayah_audio:
                seg = str(seg_dir / f"seg_{idx:03d}_s{scene.scene_id}_recite.mp4")
                if self._build_segment(scene.image_path, scene.ayah_audio, seg,
                                       subtitle=scene.ayah.text,
                                       is_recitation=True):
                    segs.append(seg); idx += 1

            # 3) شرح
            if scene.explain_audio:
                seg = str(seg_dir / f"seg_{idx:03d}_s{scene.scene_id}_explain.mp4")
                if self._build_segment(scene.image_path, scene.explain_audio, seg):
                    segs.append(seg); idx += 1

        # ── Mid scenes
        for sc in script.mid_scenes:
            if sc.image_path and sc.audio_path:
                seg = str(seg_dir / f"seg_{idx:03d}_mid{sc.scene_id}.mp4")
                if self._build_segment(sc.image_path, sc.audio_path, seg):
                    segs.append(seg); idx += 1

        # ── Outro
        if script.outro_scene.image_path and script.outro_scene.audio_path:
            seg = str(seg_dir / f"seg_{idx:03d}_outro.mp4")
            if self._build_segment(script.outro_scene.image_path,
                                   script.outro_scene.audio_path, seg):
                segs.append(seg); idx += 1

        if not segs:
            logger.error("❌ لا يوجد segments")
            out = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.touch()
            return str(out)

        logger.info(f"📦 {len(segs)} segments — دمج...")
        merged = ep_path / "merged.mp4"
        if not self._concat_segments(segs, str(merged)):
            out = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(segs[0], out)
            return str(out)

        out = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        if not self._add_bgm(str(merged), str(out)):
            shutil.copy(merged, out)
        try:
            merged.unlink()
        except Exception:
            pass

        logger.info(f"✅ {out.name}")
        return str(out)
