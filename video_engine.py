"""
video_engine.py — VALUE / QEEMA v9.1 (Ultimate Web Renderer + Pipeline-Compat)
=============================================================================
الرندرة باستخدام Playwright + HTML5 Canvas + CSS Particle System.

v9.1 = v9.0 + إصلاحات توافق الـ orchestrator:
  - اسم ملف الإخراج بقى "ep_NNN_raw.mp4" بدل "ep_NNN.mp4"
    (عشان آلية الـ resume في orchestrator._stage_video تشتغل صح)
  - shutil.move بدل os.rename لتجنب فشل cross-filesystem
  - تنظيف اسم الملف المؤقت لتجنب التعارض في الـ paths
  - الجودة 100% زي ما هي (نفس CRF, FPS, CSS animations)
"""
import logging
import os
import shutil
import subprocess as sp
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import VideoConfig, Paths

logger = logging.getLogger(__name__)


class VideoEngine:
    def __init__(self):
        self.W = VideoConfig.RESOLUTION_WIDTH
        self.H = VideoConfig.RESOLUTION_HEIGHT
        logo_path = Path('assets/logo.png').absolute()
        self.logo = f"file://{logo_path}" if logo_path.exists() else ""
        Paths.WEB_RENDERS.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────
    # HTML Template (نفس التصميم السينمائي 100%)
    # ──────────────────────────────────────────────────────────────
    def _get_html_template(self, img_path: str, text: str, dur: float) -> str:
        img_uri = f"file://{Path(img_path).absolute()}"
        return f"""
        <!DOCTYPE html>
        <html dir="rtl">
        <head>
            <style>
                body {{ margin: 0; background: #000; overflow: hidden; font-family: 'Amiri', serif; }}
                .bg {{ position: absolute; width: 110%; height: 110%; background: url('{img_uri}') center/cover;
                       animation: kenburns {dur+1}s linear forwards; top: -5%; left: -5%; }}
                .overlay {{ position: absolute; width: 100%; height: 100%;
                            background: linear-gradient(to top, rgba(0,0,0,0.9) 0%, transparent 60%); }}
                #canvas {{ position: absolute; top: 0; left: 0; }}
                .text-box {{ position: absolute; bottom: 120px; width: 100%; text-align: center;
                             animation: slideUp 1s ease-out; z-index: 100; }}
                .text {{ display: inline-block; color: #fff; font-size: 60px; font-weight: bold;
                         text-shadow: 0 0 20px #FFD700, 0 0 40px #FFA500;
                         background: rgba(0,0,0,0.5); padding: 20px 50px; border-radius: 30px;
                         border: 2px solid rgba(255,215,0,0.4); backdrop-filter: blur(10px); }}
                .logo {{ position: absolute; top: 40px; right: 40px; width: 180px; opacity: 0.9; z-index: 200; }}
                .progress {{ position: absolute; bottom: 0; left: 0; height: 10px; background: linear-gradient(90deg, #FFD700, #FFA500);
                             animation: load {dur}s linear forwards; box-shadow: 0 0 20px #FFD700; }}
                @keyframes kenburns {{ 0% {{ transform: scale(1); }} 100% {{ transform: scale(1.1) rotate(0.5deg); }} }}
                @keyframes load {{ from {{ width: 0; }} to {{ width: 100%; }} }}
                @keyframes slideUp {{ from {{ opacity: 0; transform: translateY(50px); }} to {{ opacity: 1; transform: translateY(0); }} }}
            </style>
        </head>
        <body>
            <div class="bg"></div>
            <div class="overlay"></div>
            <canvas id="canvas"></canvas>
            <img src="{self.logo}" class="logo">
            <div class="text-box">{"<div class='text'>"+text+"</div>" if text else ""}</div>
            <div class="progress"></div>
            <script>
                const canvas = document.getElementById('canvas');
                const ctx = canvas.getContext('2d');
                canvas.width = {self.W}; canvas.height = {self.H};
                let particles = [];
                for(let i=0; i<50; i++) particles.push({{x: Math.random()*canvas.width, y: Math.random()*canvas.height, r: Math.random()*3+1, v: Math.random()*0.5+0.2}});
                function draw() {{
                    ctx.clearRect(0, 0, canvas.width, canvas.height);
                    ctx.fillStyle = "rgba(255, 215, 0, 0.6)";
                    particles.forEach(p => {{
                        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI*2); ctx.fill();
                        p.y -= p.v; if(p.y < -10) p.y = canvas.height + 10;
                    }});
                    draw_id = requestAnimationFrame(draw);
                }}
                let draw_id = null;
                draw();
            </script>
        </body>
        </html>
        """

    # ──────────────────────────────────────────────────────────────
    # رندرة مشهد واحد
    # ──────────────────────────────────────────────────────────────
    def _get_audio_duration(self, audio_path: str) -> float:
        """قراءة مدة ملف الصوت بدقة عبر ffprobe."""
        try:
            result = sp.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of",
                 "default=noprint_wrappers=1:nokey=1", audio_path],
                capture_output=True, text=True, timeout=30
            )
            return float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"⚠️ ffprobe failed for {audio_path}: {e} — fallback 5s")
            return 5.0

    def _render_scene(self, img: str, aud: str, sub: str, out: str, is_recite: bool = False):
        # حساب المدة + هامش
        dur = self._get_audio_duration(aud) + (0.8 if is_recite else 0.2)

        # مفاتيح فريدة لتجنب التعارض بين العمليات المتزامنة
        unique_id = f"{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
        html_file = Paths.TEMP / f"scene_{unique_id}.html"
        raw_vid = str(Paths.WEB_RENDERS / f"v_{unique_id}.webm")

        try:
            html_file.write_text(self._get_html_template(img, sub, dur), encoding="utf-8")

            # رندرة بـ Playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--disable-web-security', '--no-sandbox']
                )
                context = browser.new_context(
                    viewport={"width": self.W, "height": self.H},
                    record_video_dir=str(Paths.WEB_RENDERS),
                    record_video_size={"width": self.W, "height": self.H}
                )
                page = context.new_page()
                page.goto(f"file://{html_file.absolute()}")
                page.wait_for_timeout(int(dur * 1000))
                vid_path = page.video.path()
                context.close()
                browser.close()

                # ✅ shutil.move بدل os.rename (يتعامل مع cross-filesystem)
                shutil.move(vid_path, raw_vid)

            # دمج الفيديو الـ webm بالصوت → mp4 عالي الجودة
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", raw_vid,
                "-i", aud,
                "-c:v", VideoConfig.CODEC,
                "-profile:v", VideoConfig.PROFILE,
                "-preset", "fast",          # خفيف للقطع الفردية (concat لاحقاً)
                "-crf", str(VideoConfig.CRF),
                "-pix_fmt", VideoConfig.PIX_FMT,
                "-r", str(VideoConfig.FPS),
                "-c:a", VideoConfig.AUDIO_CODEC,
                "-b:a", VideoConfig.AUDIO_BITRATE,
                "-shortest",
                out
            ]
            r = sp.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                logger.error(f"❌ ffmpeg failed: {r.stderr[-300:]}")
                raise RuntimeError("ffmpeg encoding failed")
        finally:
            # تنظيف الملفات المؤقتة
            for tmp in [html_file, Path(raw_vid)]:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass

    # ──────────────────────────────────────────────────────────────
    # تجميع الحلقة كاملة
    # ──────────────────────────────────────────────────────────────
    def assemble_episode(self, script, ep_dir: str) -> str:
        logger.info(f"🎬 رندرة الحلقة {script.episode_number} بأعلى جودة (60FPS)...")
        ep_path = Path(ep_dir)
        seg_dir = ep_path / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        segs = []
        idx = 0

        # تنفيذ الرندرة لكل مشهد
        all_scenes = [script.intro_scene] + list(script.ayah_scenes) + list(script.mid_scenes) + [script.outro_scene]

        for scene in all_scenes:
            # في حال كانت آية، يتم رندرة 3 أجزاء (تمهيد، تلاوة، شرح)
            if hasattr(scene, 'ayah'):
                parts = [
                    ('intro', scene.intro_audio, ""),
                    ('recite', scene.ayah_audio, scene.ayah.text),
                    ('explain', scene.explain_audio, ""),
                ]
                for a_type, a_path, a_text in parts:
                    if a_path and Path(a_path).exists():
                        out = str(seg_dir / f"s_{idx:03d}.mp4")
                        try:
                            self._render_scene(scene.image_path, a_path, a_text, out, is_recite=(a_type == 'recite'))
                            if Path(out).exists() and Path(out).stat().st_size > 1000:
                                segs.append(out)
                                idx += 1
                            else:
                                logger.warning(f"⚠️ Segment {idx} ({a_type}) لم يُنتج، تخطي.")
                        except Exception as e:
                            logger.error(f"❌ فشل رندرة segment {idx} ({a_type}): {e}")
            else:
                if scene.audio_path and Path(scene.audio_path).exists():
                    out = str(seg_dir / f"s_{idx:03d}.mp4")
                    try:
                        self._render_scene(scene.image_path, scene.audio_path, "", out)
                        if Path(out).exists() and Path(out).stat().st_size > 1000:
                            segs.append(out)
                            idx += 1
                    except Exception as e:
                        logger.error(f"❌ فشل رندرة scene {idx}: {e}")

        if not segs:
            raise RuntimeError("❌ لم يتم إنتاج أي segment صالح")

        # الدمج النهائي
        list_file = ep_path / "list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for s in segs:
                f.write(f"file '{Path(s).absolute()}'\n")

        # ✅ اسم الملف بقى متوافق مع الـ orchestrator (_raw.mp4)
        final_out = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4")

        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            final_out
        ]
        r = sp.run(concat_cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            # الـ -c copy فشل (غالباً اختلاف خصائص) — نعيد الترميز
            logger.warning("⚠️ concat copy فشل، نعيد الترميز...")
            reencode_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c:v", VideoConfig.CODEC,
                "-profile:v", VideoConfig.PROFILE,
                "-crf", str(VideoConfig.CRF),
                "-preset", VideoConfig.PRESET,
                "-pix_fmt", VideoConfig.PIX_FMT,
                "-r", str(VideoConfig.FPS),
                "-c:a", VideoConfig.AUDIO_CODEC,
                "-b:a", VideoConfig.AUDIO_BITRATE,
                final_out
            ]
            r2 = sp.run(reencode_cmd, capture_output=True, text=True, timeout=900)
            if r2.returncode != 0:
                raise RuntimeError(f"ffmpeg concat failed: {r2.stderr[-400:]}")

        try:
            list_file.unlink()
        except Exception:
            pass

        logger.info(f"✅ Episode {script.episode_number} raw video → {final_out}")
        return final_out
