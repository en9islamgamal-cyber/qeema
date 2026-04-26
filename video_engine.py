"""
video_engine.py — VALUE / QEEMA v9.0 (Ultimate Web Renderer)
============================================================
الرندرة باستخدام Playwright + HTML5 Canvas + CSS Particle System.
"""
import logging, os, time, subprocess as sp
from pathlib import Path
from playwright.sync_api import sync_playwright
from config import VideoConfig, Paths

logger = logging.getLogger(__name__)

class VideoEngine:
    def __init__(self):
        self.W, self.H = VideoConfig.RESOLUTION_WIDTH, VideoConfig.RESOLUTION_HEIGHT
        self.logo = f"file://{Path('assets/logo.png').absolute()}" if Path('assets/logo.png').exists() else ""
        Paths.WEB_RENDERS = Paths.TEMP / "web_renders"
        Paths.WEB_RENDERS.mkdir(parents=True, exist_ok=True)

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
                    requestAnimationFrame(draw);
                }}
                draw();
            </script>
        </body>
        </html>
        """

    def _render_scene(self, img, aud, sub, out, is_recite=False):
        dur = float(sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", aud], capture_output=True, text=True).stdout.strip()) + (0.8 if is_recite else 0.2)
        html_file = Paths.TEMP / f"scene_{time.time()}.html"
        html_file.write_text(self._get_html_template(img, sub, dur), encoding="utf-8")
        raw_vid = str(Paths.WEB_RENDERS / f"v_{time.time()}.webm")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--disable-web-security'])
            context = browser.new_context(record_video_dir=str(Paths.WEB_RENDERS), record_video_size={"width": self.W, "height": self.H})
            page = context.new_page()
            page.goto(f"file://{html_file.absolute()}")
            page.wait_for_timeout(int(dur * 1000))
            vid_path = page.video.path()
            context.close(); browser.close()
            os.rename(vid_path, raw_vid)
        
        sp.run(["ffmpeg", "-y", "-i", raw_vid, "-i", aud, "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac", "-shortest", out], capture_output=True)
        try: html_file.unlink(); Path(raw_vid).unlink()
        except: pass

    def assemble_episode(self, script: any, ep_dir: str) -> str:
        logger.info(f"🎬 رندرة الحلقة {script.episode_number} بأعلى جودة...")
        ep_path = Path(ep_dir); seg_dir = ep_path / "segments"; seg_dir.mkdir(parents=True, exist_ok=True)
        segs = []; idx = 0
        
        # تنفيذ الرندرة لكل مشهد
        for scene in [script.intro_scene] + script.ayah_scenes + [script.outro_scene]:
            # في حال كانت آية، يتم رندرة 3 أجزاء (تمهيد، تلاوة، شرح)
            if hasattr(scene, 'ayah'):
                for a_type, a_path, a_text in [('intro', scene.intro_audio, ""), ('recite', scene.ayah_audio, scene.ayah.text), ('explain', scene.explain_audio, "")]:
                    if a_path:
                        out = str(seg_dir / f"s_{idx:03d}.mp4")
                        self._render_scene(scene.image_path, a_path, a_text, out, is_recite=(a_type=='recite'))
                        segs.append(out); idx += 1
            else:
                out = str(seg_dir / f"s_{idx:03d}.mp4")
                self._render_scene(scene.image_path, scene.audio_path, "", out)
                segs.append(out); idx += 1

        # الدمج النهائي
        list_file = ep_path / "list.txt"
        with open(list_file, "w") as f:
            for s in segs: f.write(f"file '{Path(s).absolute()}'\n")
        
        final_out = str(Paths.VIDEOS / f"ep_{script.episode_number:03d}.mp4")
        sp.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", final_out], capture_output=True)
        return final_out