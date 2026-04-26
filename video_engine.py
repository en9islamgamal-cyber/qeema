"""
video_engine.py — VALUE / QEEMA v8.0 (Cinematic HTML-to-Video Engine)
=====================================================================
هذا المحرك ينقل جودة الفيديو إلى مستوى استوديوهات الإنتاج (Meta AI Style).
- يبني المشاهد كصفحات ويب (HTML/CSS) متقدمة.
- يضيف جسيمات سحرية متحركة (Glowing Particles).
- يولد نصوصاً متوهجة وظلالاً ناعمة.
- يقوم برندرة الصفحة (تصوير الشاشة) بـ 60FPS باستخدام Playwright.
- مزود بنظام Fallback قوي يعتمد على FFmpeg العادي في حالات الفشل.
"""

import logging
import os
import shutil
import time
import subprocess as sp
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING
from config import VideoConfig, Paths

if TYPE_CHECKING:
    from models import EpisodeScript

logger = logging.getLogger(__name__)
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}

# استيراد متصفح Playwright للرندرة الفائقة
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    logger.critical("⚠️ Playwright غير مثبت! المحرك السينمائي معطل، سيتم تفعيل نظام الطوارئ (FFmpeg Fallback). لتفعيله: pip install playwright && playwright install")

# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════
def _run(cmd: List[str], timeout: int = 900) -> bool:
    try:
        r = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.error(f"❌ FFmpeg Error:\n{r.stderr[-500:]}")
            return False
        return True
    except Exception as e:
        logger.error(f"❌ FFmpeg Exception: {e}")
        return False

def _probe_duration(path: str) -> float:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        return float(sp.run(cmd, capture_output=True, text=True).stdout.strip())
    except: 
        return 0.0

def _escape_dt(s: str) -> str:
    """Escape text for FFmpeg drawtext (Fallback use only)"""
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


# ══════════════════════════════════════════════════════════════════
# VideoEngine Main Class
# ══════════════════════════════════════════════════════════════════
class VideoEngine:
    def __init__(self):
        self.W, self.H = VideoConfig.RESOLUTION_WIDTH, VideoConfig.RESOLUTION_HEIGHT
        self.fps = VideoConfig.FPS
        self.logo = self._find_logo()
        self.bg_music = self._find_bgm()
        self.font_path = self._find_font()
        
        # تهيئة مسارات رندرة الويب
        if not hasattr(Paths, "WEB_RENDERS"):
            Paths.WEB_RENDERS = Paths.TEMP / "web_renders"
        Paths.WEB_RENDERS.mkdir(parents=True, exist_ok=True)

    def _find_logo(self) -> str:
        for c in [Paths.ASSETS / "logo.png", Paths.ASSETS / "logo.jpg"]:
            if Path(c).exists(): return f"file://{Path(c).absolute()}"
        return ""

    def _find_bgm(self) -> Optional[str]:
        for c in [Paths.OVERLAYS / "bgm.mp3", Paths.ASSETS / "bgm.mp3"]:
            if Path(c).exists(): return str(c.absolute())
        return None
        
    def _find_font(self) -> str:
        for c in [Paths.FONTS / "Amiri-Bold.ttf", Paths.FONTS / "NotoSansArabic-Bold.ttf"]:
            if Path(c).exists(): return str(c.absolute())
        return ""

    # ─────────────────────────────────────────────────────────────
    # Web Engine (HTML/CSS Magic)
    # ─────────────────────────────────────────────────────────────
    def _generate_html(self, media_path: str, subtitle: Optional[str], duration: float) -> str:
        """
        يولد HTML يحتوي على:
        - صورة/فيديو كخلفية مع Ken Burns.
        - جسيمات سحرية صاعدة (Glowing Particles).
        - نصوص متوهجة وظلال (Meta AI Style).
        - شريط تقدم سينمائي.
        """
        is_video = Path(media_path).suffix.lower() in VIDEO_EXT
        media_uri = f"file://{Path(media_path).absolute()}"
        
        bg_element = f'<video class="bg" src="{media_uri}" autoplay loop muted></video>' if is_video \
                     else f'<div class="bg" style="background-image: url(\'{media_uri}\');"></div>'

        logo_html = f'<img class="logo" src="{self.logo}" />' if self.logo else ""
        sub_html = f'<div class="subtitle-container"><div class="subtitle">{subtitle}</div></div>' if subtitle else ""
        
        # كود JS لتوليد الجسيمات السحرية
        particles_js = """
        <script>
            document.addEventListener("DOMContentLoaded", () => {
                const container = document.getElementById('particles');
                const particleCount = 40;
                for(let i=0; i<particleCount; i++) {
                    let p = document.createElement('div');
                    p.className = 'particle';
                    p.style.left = Math.random() * 100 + '%';
                    p.style.animationDuration = (Math.random() * 3 + 2) + 's';
                    p.style.animationDelay = (Math.random() * 2) + 's';
                    container.appendChild(p);
                }
            });
        </script>
        """

        return f"""
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
            <meta charset="UTF-8">
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Amiri:wght@700&display=swap');
                
                body {{ 
                    margin: 0; padding: 0; width: {self.W}px; height: {self.H}px; 
                    overflow: hidden; background: #000; 
                    font-family: 'Amiri', serif; 
                }}
                
                /* الخلفية وتأثير Ken Burns الناعم */
                .bg {{ 
                    position: absolute; top:-5%; left:-5%; width: 110%; height: 110%; 
                    background-size: cover; background-position: center; object-fit: cover;
                    animation: zoom {duration + 2}s linear forwards; 
                }}
                
                /* تدرج لوني للظلال أسفل الشاشة */
                .overlay {{ 
                    position: absolute; top:0; left:0; width: 100%; height: 100%; 
                    background: linear-gradient(to top, rgba(0,0,0,0.95) 0%, rgba(0,0,0,0.4) 30%, transparent 80%); 
                }}
                
                /* الجسيمات السحرية المتوهجة */
                #particles {{ position: absolute; top:0; left:0; width: 100%; height: 100%; z-index: 5; pointer-events: none; }}
                .particle {{
                    position: absolute; bottom: -20px; width: 8px; height: 8px;
                    background: radial-gradient(circle, #fff 0%, #FFD700 40%, transparent 80%);
                    border-radius: 50%; opacity: 0;
                    animation: floatUp linear infinite;
                    box-shadow: 0 0 10px #FFD700;
                }}
                
                /* الشعار */
                .logo {{ 
                    position: absolute; top: 40px; right: 40px; width: 180px; 
                    opacity: 0.9; filter: drop-shadow(0px 0px 20px rgba(255,255,255,0.4)); 
                    z-index: 10; 
                }}
                
                /* النص المتوهج كالذهب */
                .subtitle-container {{ 
                    position: absolute; bottom: 120px; width: 100%; text-align: center; 
                    animation: fade {duration}s ease-in-out forwards; z-index: 10; 
                }}
                .subtitle {{ 
                    display: inline-block; font-size: 65px; color: #ffffff; line-height: 1.5;
                    padding: 20px 50px; border-radius: 30px; 
                    background: rgba(20, 20, 20, 0.65); border: 2px solid rgba(255, 215, 0, 0.5); 
                    text-shadow: 0px 0px 25px rgba(255, 215, 0, 1), 2px 2px 8px rgba(0,0,0,1); 
                    backdrop-filter: blur(12px); max-width: 85%;
                }}
                
                /* شريط التقدم */
                .progress-track {{ position: absolute; bottom: 0; left: 0; height: 8px; width: 100%; background: rgba(255,255,255,0.1); z-index: 10; }}
                .progress-fill {{ 
                    height: 100%; width: 0%; 
                    background: linear-gradient(90deg, #FFD700, #FFA500, #FF8C00); 
                    box-shadow: 0px 0px 20px rgba(255, 215, 0, 1); 
                    animation: load {duration}s linear forwards; 
                }}
                
                /* Keyframes */
                @keyframes zoom {{ 0% {{ transform: scale(1); }} 100% {{ transform: scale(1.08); }} }}
                @keyframes load {{ 0% {{ width: 0%; }} 100% {{ width: 100%; }} }}
                @keyframes floatUp {{ 
                    0% {{ transform: translateY(0) scale(0.5); opacity: 0; }} 
                    20% {{ opacity: 0.8; }} 
                    80% {{ opacity: 0.5; }} 
                    100% {{ transform: translateY(-800px) scale(1.5); opacity: 0; }} 
                }}
                @keyframes fade {{ 
                    0% {{ opacity: 0; transform: translateY(40px); }} 
                    5%, 95% {{ opacity: 1; transform: translateY(0); }} 
                    100% {{ opacity: 0; transform: translateY(-20px); }} 
                }}
            </style>
        </head>
        <body>
            {bg_element}
            <div class="overlay"></div>
            <div id="particles"></div>
            {logo_html}
            {sub_html}
            <div class="progress-track"><div class="progress-fill"></div></div>
            {particles_js}
        </body>
        </html>
        """

    def _render_web(self, media: str, aud: str, out: str, sub: Optional[str], dur: float) -> bool:
        """يصور صفحة الـ HTML المتوهجة بمتصفح خفي ثم يدمجها مع الصوت الأصلي"""
        if not HAS_PLAYWRIGHT: 
            return False
            
        html_code = self._generate_html(media, sub, dur)
        html_file = Paths.WEB_RENDERS / f"scene_{int(time.time()*1000)}.html"
        html_file.write_text(html_code, encoding="utf-8")
        
        raw_vid = str(Paths.WEB_RENDERS / f"vid_{int(time.time()*1000)}.webm")
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--disable-web-security', '--allow-file-access-from-files'])
                ctx = browser.new_context(
                    record_video_dir=str(Paths.WEB_RENDERS), 
                    record_video_size={"width": self.W, "height": self.H}
                )
                page = ctx.new_page()
                page.goto(f"file://{html_file.absolute()}")
                
                # انتظار اكتمال المشهد بالمللي ثانية
                page.wait_for_timeout(int(dur * 1000))
                
                vid_path = page.video.path()
                ctx.close()
                browser.close()
                
                os.rename(vid_path, raw_vid)
        except Exception as e:
            logger.error(f"❌ Playwright Render Failed: {e}")
            return False

        # دمج الفيديو الصامت المصور مع الصوت باستخدام ترميز عالي الجودة
        cmd = [
            "ffmpeg", "-y", 
            "-i", raw_vid, "-i", aud, 
            "-c:v", VideoConfig.CODEC, "-preset", "fast", "-crf", "18", 
            "-c:a", "aac", "-b:a", "192k", 
            "-shortest", out
        ]
        res = _run(cmd, timeout=400)
        
        try: html_file.unlink(); Path(raw_vid).unlink()
        except: pass
        return res

    # ─────────────────────────────────────────────────────────────
    # FFmpeg Fallback (نظام الطوارئ)
    # ─────────────────────────────────────────────────────────────
    def _fallback_render(self, media: str, aud: str, out: str, sub: Optional[str], dur: float) -> bool:
        """نظام الطوارئ: يستخدم FFmpeg الصرف إذا تعطل المتصفح."""
        is_video = Path(media).suffix.lower() in VIDEO_EXT
        W, H, fps = self.W, self.H, self.fps
        
        if is_video:
            vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            inputs = ["-stream_loop", "-1", "-i", media, "-i", aud]
        else:
            vf = f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},zoompan=z='min(zoom+0.0006,1.06)':d={int(dur*fps)}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps},setsar=1"
            inputs = ["-loop", "1", "-i", media, "-i", aud]
            
        if sub and self.font_path:
            esc = _escape_dt(sub)
            vf += f",drawtext=fontfile='{self.font_path}':text='{esc}':fontsize=50:fontcolor=#FFD700:box=1:boxcolor=black@0.6:boxborderw=20:x=(w-text_w)/2:y=h-200"
            
        vf += f",fade=t=in:st=0:d=0.5,fade=t=out:st={dur-0.5}:d=0.5"

        cmd = ["ffmpeg", "-y"] + inputs + ["-vf", vf, "-c:v", VideoConfig.CODEC, "-profile:v", VideoConfig.PROFILE, "-crf", str(VideoConfig.CRF), "-preset", VideoConfig.PRESET, "-pix_fmt", VideoConfig.PIX_FMT, "-c:a", VideoConfig.AUDIO_CODEC, "-b:a", VideoConfig.AUDIO_BITRATE, "-r", str(fps), "-t", f"{dur:.3f}", "-shortest", out]
        return _run(cmd)

    # ─────────────────────────────────────────────────────────────
    # Router & Final Assembly
    # ─────────────────────────────────────────────────────────────
    def _build_segment(self, img: str, aud: str, out: str, sub: Optional[str] = None, is_recitation: bool = False) -> bool:
        # حساب الطول: 0.8 ثانية إضافة للقرآن (لتغطية صمت الـ Padding)، و 0.3 للراوي
        dur = _probe_duration(aud) + (0.8 if is_recitation else 0.3)
        if dur <= 0.3: return False
        
        logger.debug(f"🎨 جاري رندرة المشهد (مدة: {dur:.1f}s)...")
        
        # 1. المحاولة السينمائية
        if self._render_web(img, aud, out, sub, dur):
            return True
            
        # 2. الطوارئ
        logger.warning("⚠️ العودة لنظام الطوارئ (FFmpeg Fallback)...")
        return self._fallback_render(img, aud, out, sub, dur)

    def assemble_episode(self, script: "EpisodeScript", ep_dir: str) -> str:
        logger.info(f"🎬 بدء التجميع السينمائي للحلقة {script.episode_number}...")
        ep_path = Path(ep_dir)
        seg_dir = ep_path / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        segs = []
        idx = 0

        # Intro
        if script.intro_scene.image_path and script.intro_scene.audio_path:
            seg = str(seg_dir / f"seg_{idx:03d}.mp4")
            if self._build_segment(script.intro_scene.image_path, script.intro_scene.audio_path, seg): 
                segs.append(seg); idx += 1

        # Ayahs
        for s in script.ayah_scenes:
            if not s.image_path: continue
            
            if s.intro_audio:
                seg = str(seg_dir / f"seg_{idx:03d}.mp4")
                if self._build_segment(s.image_path, s.intro_audio, seg): segs.append(seg); idx += 1
                
            if s.ayah_audio:
                seg = str(seg_dir / f"seg_{idx:03d}.mp4")
                if self._build_segment(s.image_path, s.ayah_audio, seg, subtitle=s.ayah.text, is_recitation=True): segs.append(seg); idx += 1
                
            if s.explain_audio:
                seg = str(seg_dir / f"seg_{idx:03d}.mp4")
                if self._build_segment(s.image_path, s.explain_audio, seg): segs.append(seg); idx += 1

        # Outro
        if script.outro_scene.image_path and script.outro_scene.audio_path:
            seg = str(seg_dir / f"seg_{idx:03d}.mp4")
            if self._build_segment(script.outro_scene.image_path, script.outro_scene.audio_path, seg): segs.append(seg)

        if not segs: 
            logger.error("❌ لا يوجد مقاطع لتجميعها.")
            return ""
        
        # Concat
        list_file = ep_path / "list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for s in segs: f.write(f"file '{Path(s).absolute()}'\n")
        
        merged = ep_path / "merged.mp4"
        logger.info("📦 جاري لحام المقاطع...")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(merged)])
        
        # BGM
        out = Paths.VIDEOS / f"ep_{script.episode_number:03d}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        
        if self.bg_music:
            logger.info("🎵 جاري دمج موسيقى الخلفية (BGM)...")
            # Ducking: خفض الموسيقى عند وجود صوت
            bgm_cmd = [
                "ffmpeg", "-y", "-i", str(merged), "-stream_loop", "-1", "-i", self.bg_music, 
                "-filter_complex", "[1:a]volume=0.03[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2", 
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)
            ]
            _run(bgm_cmd)
        else:
            shutil.copy(merged, out)
            
        try: list_file.unlink(); merged.unlink()
        except: pass
            
        logger.info(f"🎉 اكتمل الفيديو السينمائي: {out.name}")
        return str(out)