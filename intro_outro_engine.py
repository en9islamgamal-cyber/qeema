"""
intro_outro_engine.py — VALUE / QEEMA v10.0 (Procedural Branding)
====================================================================
ينشئ intro/outro سينمائي بـ HTML/CSS/JS:
  - Intro 5 ثواني: لوجو + اسم القناة + tagline + golden particles
  - Outro 5 ثواني: لوجو + "اشترك" + "قِيمَة | VALUE"
  - Render via Playwright (نفس video_engine)
  - يحفظهم في assets/branding/ ويعيد استخدامهم لكل الحلقات
"""
import logging
import shutil
import subprocess as sp
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import BrandingConfig, Paths, VideoConfig

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# HTML Templates
# ════════════════════════════════════════════════════════════════
def _build_intro_html(logo_uri: str, duration: float) -> str:
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    width: 1920px; height: 1080px; overflow: hidden;
    background: linear-gradient(135deg, #FFFAF0 0%, #FFF8DC 50%, #FFEFD5 100%);
    font-family: 'Amiri', 'Tajawal', serif; direction: rtl;
}}
.container {{ position: absolute; inset: 0; }}

.logo-stage {{
    position: absolute; top: 18%; left: 50%;
    transform: translateX(-50%);
    width: 320px; height: 320px;
    animation: logo-arrive 0.8s ease-out forwards,
               logo-glow 2.5s ease-in-out 0.8s infinite alternate;
    opacity: 0;
}}
.logo-stage img {{ width: 100%; height: 100%; object-fit: contain; }}
@keyframes logo-arrive {{
    from {{ opacity: 0; transform: translateX(-50%) scale(0.5); }}
    to   {{ opacity: 1; transform: translateX(-50%) scale(1); }}
}}
@keyframes logo-glow {{
    from {{ filter: drop-shadow(0 0 20px rgba(212, 175, 55, 0.4)); }}
    to   {{ filter: drop-shadow(0 0 40px rgba(212, 175, 55, 0.8)); }}
}}

.title-ar {{
    position: absolute; top: 60%; left: 50%;
    transform: translateX(-50%);
    color: #0A1628;
    font-size: 160px; font-weight: 900;
    text-shadow: 0 4px 12px rgba(212, 175, 55, 0.4);
    opacity: 0; letter-spacing: 8px;
    animation: title-arrive 0.7s ease-out 0.9s forwards;
}}
@keyframes title-arrive {{
    from {{ opacity: 0; transform: translateX(-50%) translateY(40px); }}
    to   {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
}}

.title-en {{
    position: absolute; top: 76%; left: 50%;
    transform: translateX(-50%);
    color: #D4AF37;
    font-size: 72px; font-weight: 700;
    letter-spacing: 24px;
    opacity: 0;
    animation: title-arrive 0.6s ease-out 1.3s forwards;
}}

.tagline {{
    position: absolute; top: 86%; left: 50%;
    transform: translateX(-50%);
    background: #0A1628; color: white;
    padding: 14px 50px; border-radius: 50px;
    font-size: 42px; font-weight: 600;
    box-shadow: 0 8px 32px rgba(10, 22, 40, 0.4);
    opacity: 0;
    animation: tag-arrive 0.6s ease-out 1.8s forwards;
}}
@keyframes tag-arrive {{
    from {{ opacity: 0; transform: translateX(-50%) scale(0.8); }}
    to   {{ opacity: 1; transform: translateX(-50%) scale(1); }}
}}

.particles {{ position: absolute; inset: 0; pointer-events: none; }}
.particle {{
    position: absolute; width: 8px; height: 8px;
    background: radial-gradient(circle, #FFD700 30%, transparent);
    border-radius: 50%;
    animation: particle-rise linear infinite;
}}
@keyframes particle-rise {{
    0%   {{ transform: translateY(100vh) scale(0.5); opacity: 0; }}
    20%  {{ opacity: 1; }}
    80%  {{ opacity: 1; }}
    100% {{ transform: translateY(-100px) scale(1); opacity: 0; }}
}}
</style>
</head>
<body>
<div class="container">
    <div class="particles" id="particles"></div>
    <div class="logo-stage">
        <img src="{logo_uri}" alt="logo">
    </div>
    <div class="title-ar">قِيمَة</div>
    <div class="title-en">VALUE</div>
    <div class="tagline">{BrandingConfig.CHANNEL_TAGLINE_AR}</div>
</div>
<script>
const p = document.getElementById('particles');
for (let i = 0; i < 60; i++) {{
    const d = document.createElement('div');
    d.className = 'particle';
    d.style.left = Math.random()*100 + '%';
    d.style.animationDuration = (4 + Math.random()*4) + 's';
    d.style.animationDelay = Math.random()*4 + 's';
    d.style.width = d.style.height = (4 + Math.random()*8) + 'px';
    p.appendChild(d);
}}
</script>
</body>
</html>
"""


def _build_outro_html(logo_uri: str, duration: float) -> str:
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    width: 1920px; height: 1080px; overflow: hidden;
    background: linear-gradient(135deg, #0A1628 0%, #1E3A5F 100%);
    font-family: 'Amiri', 'Tajawal', serif; direction: rtl;
}}
.container {{ position: absolute; inset: 0; }}

.logo-stage {{
    position: absolute; top: 12%; left: 50%;
    transform: translateX(-50%);
    width: 240px; height: 240px;
    animation: logo-arrive 0.7s ease-out forwards;
    opacity: 0;
    filter: drop-shadow(0 0 40px rgba(212, 175, 55, 0.7));
}}
.logo-stage img {{ width: 100%; height: 100%; object-fit: contain; }}
@keyframes logo-arrive {{
    from {{ opacity: 0; transform: translateX(-50%) scale(0.7); }}
    to   {{ opacity: 1; transform: translateX(-50%) scale(1); }}
}}

.subscribe {{
    position: absolute; top: 50%; left: 50%;
    transform: translateX(-50%);
    background: #D4AF37; color: #0A1628;
    padding: 30px 80px; border-radius: 100px;
    font-size: 88px; font-weight: 900;
    box-shadow: 0 12px 48px rgba(212, 175, 55, 0.5);
    opacity: 0;
    animation: subscribe-arrive 0.6s ease-out 0.8s forwards;
}}
@keyframes subscribe-arrive {{
    from {{ opacity: 0; transform: translateX(-50%) scale(0.6); }}
    to   {{ opacity: 1; transform: translateX(-50%) scale(1); }}
}}

.brand-line {{
    position: absolute; top: 78%; left: 50%;
    transform: translateX(-50%);
    color: #D4AF37; font-size: 56px; font-weight: 700;
    letter-spacing: 12px;
    opacity: 0;
    animation: subscribe-arrive 0.6s ease-out 1.5s forwards;
}}

.particles {{ position: absolute; inset: 0; pointer-events: none; }}
.particle {{
    position: absolute; width: 6px; height: 6px;
    background: #FFD700; border-radius: 50%;
    box-shadow: 0 0 12px #FFD700;
    animation: float-up linear infinite;
}}
@keyframes float-up {{
    0%   {{ transform: translateY(100vh); opacity: 0; }}
    10%  {{ opacity: 1; }}
    90%  {{ opacity: 1; }}
    100% {{ transform: translateY(-100px); opacity: 0; }}
}}
</style>
</head>
<body>
<div class="container">
    <div class="particles" id="particles"></div>
    <div class="logo-stage">
        <img src="{logo_uri}" alt="logo">
    </div>
    <div class="subscribe">{BrandingConfig.SUBSCRIBE_TEXT}</div>
    <div class="brand-line">{BrandingConfig.CHANNEL_NAME_AR} | {BrandingConfig.CHANNEL_NAME_EN}</div>
</div>
<script>
const p = document.getElementById('particles');
for (let i = 0; i < 80; i++) {{
    const d = document.createElement('div');
    d.className = 'particle';
    d.style.left = Math.random()*100 + '%';
    d.style.animationDuration = (3 + Math.random()*5) + 's';
    d.style.animationDelay = Math.random()*5 + 's';
    p.appendChild(d);
}}
</script>
</body>
</html>
"""


# ════════════════════════════════════════════════════════════════
# Core builder helper
# ════════════════════════════════════════════════════════════════
def _render_html_to_video(html_content: str, duration: float, output_path: str) -> bool:
    """يرندر HTML لـ video بـ Playwright + ffmpeg."""
    Paths.WEB_RENDERS.mkdir(parents=True, exist_ok=True)
    Paths.TEMP_HTML.mkdir(parents=True, exist_ok=True)

    unique_id = f"{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
    html_file = Paths.TEMP_HTML / f"branding_{unique_id}.html"
    raw_video = Paths.WEB_RENDERS / f"branding_{unique_id}.webm"

    try:
        html_file.write_text(html_content, encoding="utf-8")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-web-security", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                viewport={"width": VideoConfig.RESOLUTION_WIDTH, "height": VideoConfig.RESOLUTION_HEIGHT},
                record_video_dir=str(Paths.WEB_RENDERS),
                record_video_size={"width": VideoConfig.RESOLUTION_WIDTH, "height": VideoConfig.RESOLUTION_HEIGHT}
            )
            page = context.new_page()
            page.goto(f"file://{html_file.absolute()}", wait_until="load")
            page.wait_for_timeout(int(duration * 1000) + 200)
            video_obj = page.video
            context.close()
            browser.close()

            if video_obj is None:
                return False
            shutil.move(video_obj.path(), str(raw_video))

        # ترميز نهائي + صمت (مش هنحط jingle لو مش موجود)
        jingle = Paths.JINGLE
        if jingle.exists():
            cmd = [
                "ffmpeg", "-y",
                "-i", str(raw_video),
                "-i", str(jingle),
                "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
                "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
                "-pix_fmt", VideoConfig.PIX_FMT, "-r", str(VideoConfig.FPS),
                "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
                "-t", str(duration), "-shortest",
                output_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(raw_video),
                "-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
                "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
                "-pix_fmt", VideoConfig.PIX_FMT, "-r", str(VideoConfig.FPS),
                "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
                "-shortest", output_path
            ]
        r = sp.run(cmd, capture_output=True, text=True, timeout=180)
        return r.returncode == 0

    except Exception as e:
        logger.error(f"❌ branding render failed: {e}")
        return False
    finally:
        for f in [html_file, raw_video]:
            try:
                if f.exists(): f.unlink()
            except: pass


# ════════════════════════════════════════════════════════════════
# IntroOutroEngine
# ════════════════════════════════════════════════════════════════
class IntroOutroEngine:
    def __init__(self, force_rebuild: bool = False):
        self.force_rebuild = force_rebuild
        Paths.BRANDING.mkdir(parents=True, exist_ok=True)

        logo_path = Paths.LOGO_PRIMARY.absolute()
        self.logo_uri = f"file://{logo_path}" if logo_path.exists() else ""
        if not self.logo_uri:
            logger.warning("⚠️ logo.png مش موجود — مش هيظهر اللوجو في الانترو/الأوترو")

    def build_intro(self) -> str:
        out = Paths.INTRO_VIDEO
        if out.exists() and not self.force_rebuild and out.stat().st_size > 50_000:
            logger.info("♻️ Intro موجود")
            return str(out)

        logger.info("🎬 بناء Intro procedural...")
        html = _build_intro_html(self.logo_uri, BrandingConfig.INTRO_DURATION)
        if _render_html_to_video(html, BrandingConfig.INTRO_DURATION, str(out)):
            logger.info(f"✅ Intro: {out}")
            return str(out)
        logger.error("❌ Intro build failed")
        return ""

    def build_outro(self) -> str:
        out = Paths.OUTRO_VIDEO
        if out.exists() and not self.force_rebuild and out.stat().st_size > 50_000:
            logger.info("♻️ Outro موجود")
            return str(out)

        logger.info("🎬 بناء Outro procedural...")
        html = _build_outro_html(self.logo_uri, BrandingConfig.OUTRO_DURATION)
        if _render_html_to_video(html, BrandingConfig.OUTRO_DURATION, str(out)):
            logger.info(f"✅ Outro: {out}")
            return str(out)
        logger.error("❌ Outro build failed")
        return ""

    def wrap_episode(self, main_video: str, output_path: str) -> str:
        intro = self.build_intro()
        outro = self.build_outro()

        if not intro or not outro:
            logger.warning("⚠️ Intro/Outro غير متاحة، نسخ الفيديو الأصلي")
            shutil.copy(main_video, output_path)
            return output_path

        list_file = Path(output_path).parent / "wrap_list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            f.write(f"file '{Path(intro).absolute()}'\n")
            f.write(f"file '{Path(main_video).absolute()}'\n")
            f.write(f"file '{Path(outro).absolute()}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE,
            "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET,
            "-pix_fmt", VideoConfig.PIX_FMT, "-r", str(VideoConfig.FPS),
            "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE,
            output_path,
        ]
        r = sp.run(cmd, capture_output=True, text=True, timeout=600)
        try: list_file.unlink()
        except: pass

        if r.returncode == 0 and Path(output_path).exists():
            logger.info(f"✅ Wrapped: {output_path}")
            return output_path

        logger.error(f"❌ Wrap failed: {r.stderr[-300:]}")
        shutil.copy(main_video, output_path)
        return output_path
