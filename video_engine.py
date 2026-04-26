"""
video_engine.py — VALUE / QEEMA v10.0 (Procedural Renderer)
=============================================================
يستخدم visual_engine.render_scene_html() لكل مشهد ثم يرندر بـ Playwright.

التغييرات الكبيرة عن v9:
- مفيش صور خارجية (Leonardo). كل شيء procedural (Three.js).
- المشاهد بتختار visual_scene تلقائياً (garden/sky/mosque/...)
- Word-level animations (الكلمات تظهر مع التوقيت).
- Logo دائم في كل مشهد.
- Particles + Ken Burns + glow.
- مدة كل مشهد محسوبة من ffprobe.
- اسم الإخراج _raw.mp4 (متوافق مع orchestrator).
"""
import logging
import os
import shutil
import subprocess as sp
import time
import uuid
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

from config import VideoConfig, Paths, BrandingConfig
from visual_engine import VisualEngine

logger = logging.getLogger(__name__)


class VideoEngine:
    def __init__(self):
        self.W = VideoConfig.RESOLUTION_WIDTH
        self.H = VideoConfig.RESOLUTION_HEIGHT
        self.fps = VideoConfig.FPS

        # Logo URI for HTML
        logo_path = Paths.LOGO_PRIMARY.absolute()
        self.logo_uri = f"file://{logo_path}" if logo_path.exists() else ""
        if not self.logo_uri:
            logger.warning("⚠️ assets/logo.png مش موجود — مش هيظهر اللوجو في الفيديو")

        Paths.WEB_RENDERS.mkdir(parents=True, exist_ok=True)
        Paths.TEMP_HTML.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────
    # Audio duration helper
    # ──────────────────────────────────────────────────────────────
    def _get_audio_duration(self, audio_path: str) -> float:
        try:
            r = sp.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of",
                 "default=noprint_wrappers=1:nokey=1", audio_path],
                capture_output=True, text=True, timeout=30
            )
            d = float(r.stdout.strip())
            return d if d > 0.1 else 5.0
        except Exception as e:
            logger.warning(f"⚠️ ffprobe فشل لـ {audio_path}: {e}")
            return 5.0

    # ──────────────────────────────────────────────────────────────
    # Scene rendering
    # ──────────────────────────────────────────────────────────────
    def _render_scene(
        self,
        visual_scene: str,
        palette: str,
        narrator_text: str,
        audio_path: str,
        output_path: str,
        is_ayah: bool = False,
        keywords: Optional[list] = None,
    ) -> bool:
        """يبني HTML للمشهد، يرندره بـ Playwright، ثم يدمجه بالصوت."""
        if not audio_path or not Path(audio_path).exists():
            logger.warning(f"⚠️ صوت مش موجود: {audio_path}")
            return False

        duration = self._get_audio_duration(audio_path) + (1.0 if is_ayah else 0.4)

        # Build HTML
        html_content = VisualEngine.render_scene_html(
            scene_type=visual_scene,
            palette_name=palette,
            text=narrator_text,
            duration=duration,
            is_ayah=is_ayah,
            logo_uri=self.logo_uri,
            keywords=keywords or [],
        )

        # Save HTML
        unique_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        html_file = Paths.TEMP_HTML / f"scene_{unique_id}.html"
        html_file.write_text(html_content, encoding="utf-8")

        raw_video = Paths.WEB_RENDERS / f"v_{unique_id}.webm"

        try:
            # Render with Playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-web-security",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--use-gl=swiftshader",          # GPU emulation للـ WebGL
                        "--enable-webgl",
                        "--ignore-gpu-blocklist",
                    ]
                )
                context = browser.new_context(
                    viewport={"width": self.W, "height": self.H},
                    record_video_dir=str(Paths.WEB_RENDERS),
                    record_video_size={"width": self.W, "height": self.H}
                )
                page = context.new_page()
                page.goto(f"file://{html_file.absolute()}", wait_until="load")

                # انتظر تحميل Three.js (بحد أقصى 5 ثواني)
                page.wait_for_timeout(2000)

                # سجّل المدة الفعلية + هامش
                wait_ms = int(duration * 1000) + 200
                page.wait_for_timeout(wait_ms)

                video_obj = page.video
                context.close()
                browser.close()

                if video_obj is None:
                    raise RuntimeError("Playwright لم ينتج فيديو")
                video_path_temp = video_obj.path()
                shutil.move(video_path_temp, str(raw_video))

            # دمج الصوت + إعادة ترميز للجودة العالية
            output_path = str(output_path)
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", str(raw_video),
                "-i", audio_path,
                "-c:v", VideoConfig.CODEC,
                "-profile:v", VideoConfig.PROFILE,
                "-preset", "medium",  # متوسط — يتم re-encode عاللاحقاً للنهائي
                "-crf", str(VideoConfig.CRF),
                "-pix_fmt", VideoConfig.PIX_FMT,
                "-r", str(self.fps),
                "-c:a", VideoConfig.AUDIO_CODEC,
                "-b:a", VideoConfig.AUDIO_BITRATE,
                "-shortest",
                output_path
            ]
            r = sp.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                logger.error(f"❌ ffmpeg فشل: {r.stderr[-500:]}")
                return False

            return Path(output_path).exists() and Path(output_path).stat().st_size > 1000

        except Exception as e:
            logger.error(f"❌ فشل رندر المشهد: {e}")
            return False
        finally:
            # تنظيف
            for f in [html_file, raw_video]:
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass

    # ──────────────────────────────────────────────────────────────
    # Episode assembly
    # ──────────────────────────────────────────────────────────────
    def assemble_episode(self, script, ep_dir: str) -> str:
        logger.info(f"🎬 رندرة procedural للحلقة {script.episode_number}...")
        ep_path = Path(ep_dir)
        seg_dir = ep_path / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)

        segs = []
        idx = 0

        # ━━━━ INTRO ━━━━
        if script.intro_scene.audio_path:
            out = str(seg_dir / f"s_{idx:03d}_intro.mp4")
            ok = self._render_scene(
                visual_scene=script.intro_scene.visual_scene.value,
                palette=script.intro_scene.palette,
                narrator_text=script.intro_scene.narrator_text,
                audio_path=script.intro_scene.audio_path,
                output_path=out,
                is_ayah=False,
                keywords=script.intro_scene.keywords,
            )
            if ok:
                segs.append(out)
                idx += 1
                logger.info(f"  ✓ intro segment built")

        # ━━━━ AYAH SCENES (3 parts each) ━━━━
        for scene in script.ayah_scenes:
            scene_type = scene.visual_scene.value
            palette = scene.palette
            keywords = scene.keywords

            # 1) intro_text (تمهيد قبل الآية)
            if scene.intro_audio:
                out = str(seg_dir / f"s_{idx:03d}_ayah{scene.scene_id}_intro.mp4")
                if self._render_scene(scene_type, palette, scene.intro_text, scene.intro_audio, out, False, keywords):
                    segs.append(out); idx += 1

            # 2) recitation (التلاوة بالنص القرآني)
            if scene.ayah_audio:
                out = str(seg_dir / f"s_{idx:03d}_ayah{scene.scene_id}_recite.mp4")
                if self._render_scene(scene_type, palette, scene.ayah.text, scene.ayah_audio, out, True, []):
                    segs.append(out); idx += 1

            # 3) explain_text (الشرح بعد الآية)
            if scene.explain_audio:
                out = str(seg_dir / f"s_{idx:03d}_ayah{scene.scene_id}_explain.mp4")
                if self._render_scene(scene_type, palette, scene.explain_text, scene.explain_audio, out, False, keywords):
                    segs.append(out); idx += 1

            logger.info(f"  ✓ ayah {scene.ayah.number} ({scene_type}) built")

        # ━━━━ MID SCENES (لو موجودة) ━━━━
        for sc in script.mid_scenes:
            if sc.audio_path:
                out = str(seg_dir / f"s_{idx:03d}_mid.mp4")
                if self._render_scene(sc.visual_scene.value, sc.palette, sc.narrator_text, sc.audio_path, out, False, sc.keywords):
                    segs.append(out); idx += 1

        # ━━━━ OUTRO ━━━━
        if script.outro_scene.audio_path:
            out = str(seg_dir / f"s_{idx:03d}_outro.mp4")
            if self._render_scene(
                script.outro_scene.visual_scene.value,
                script.outro_scene.palette,
                script.outro_scene.narrator_text,
                script.outro_scene.audio_path,
                out, False, script.outro_scene.keywords,
            ):
                segs.append(out); idx += 1

        if not segs:
            raise RuntimeError("❌ لم يتم إنتاج أي segment")

        logger.info(f"📦 تم بناء {len(segs)} segment، جاري الدمج...")

        # ━━━━ FINAL CONCAT ━━━━
        list_file = ep_path / "concat_list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for s in segs:
                f.write(f"file '{Path(s).absolute()}'\n")

        # اسم متوافق مع orchestrator: ep_NNN_raw.mp4
        final_out = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4")

        # نعيد الترميز كاملاً لضمان توحيد الخصائص
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", VideoConfig.CODEC,
            "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF),
            "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT,
            "-r", str(self.fps),
            "-c:a", VideoConfig.AUDIO_CODEC,
            "-b:a", VideoConfig.AUDIO_BITRATE,
            final_out
        ]
        r = sp.run(concat_cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg concat فشل: {r.stderr[-400:]}")

        try: list_file.unlink()
        except: pass

        logger.info(f"✅ Episode {script.episode_number} raw → {final_out}")
        return final_out
