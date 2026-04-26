"""
visual_engine.py — VALUE / QEEMA v10.0 (Procedural HTML/Three.js Renderer)
============================================================================
لا يستخدم أي API خارجي. يولد المشاهد بـ:
  - Three.js procedural 3D scenes (Pixar-inspired)
  - SVG generative backgrounds
  - CSS animated particles
  - Word-level text animations synchronized with TTS

كل visual_scene له template خاص:
  - garden:        أشجار متحركة + فراشات + ضوء ذهبي
  - sky:           نجوم متلألئة + قمر + غيوم
  - house:         بيت 3D + نوافذ مضاءة
  - mosque:        مسجد بقبة وهلال
  - ocean:         موج + سحاب
  - desert:        كثبان + شمس
  - mountains:     جبال + غيوم
  - child_praying: طفل في الصلاة (silhouette)
  - family:        قلوب متحركة + أيدي
  - abstract_warm: تدرجات لونية + أشكال هندسية
"""
import logging
from typing import Dict, Any, List
from config import ProceduralConfig, BrandingConfig, Paths
from models import VisualScene

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Common CSS / JS bases
# ════════════════════════════════════════════════════════════════
COMMON_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    width: 1920px; height: 1080px;
    overflow: hidden;
    background: #000;
    font-family: 'Amiri', 'Noto Sans Arabic', 'Tajawal', serif;
    direction: rtl;
}
.scene-container {
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    overflow: hidden;
}
canvas#three-canvas {
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    z-index: 1;
}
.gradient-overlay {
    position: absolute; inset: 0;
    z-index: 2;
    pointer-events: none;
    background: linear-gradient(to top,
        rgba(0,0,0,0.85) 0%,
        rgba(0,0,0,0.3) 35%,
        transparent 60%,
        rgba(0,0,0,0.2) 100%);
}
.particles-layer {
    position: absolute; inset: 0;
    z-index: 3;
    pointer-events: none;
}
.particle {
    position: absolute;
    border-radius: 50%;
    pointer-events: none;
    will-change: transform, opacity;
}
.logo-overlay {
    position: absolute;
    top: 30px; right: 30px;
    width: 120px; height: 120px;
    z-index: 100;
    opacity: 0.9;
    filter: drop-shadow(0 0 20px rgba(255, 215, 0, 0.5));
    animation: logo-pulse 4s ease-in-out infinite;
}
@keyframes logo-pulse {
    0%, 100% { opacity: 0.9; transform: scale(1); }
    50%      { opacity: 1.0; transform: scale(1.05); }
}
.text-container {
    position: absolute;
    bottom: 100px; left: 50%;
    transform: translateX(-50%);
    width: 80%;
    text-align: center;
    z-index: 50;
}
.narrator-text {
    display: inline-block;
    color: #fff;
    font-size: 56px;
    font-weight: 700;
    line-height: 1.6;
    padding: 30px 60px;
    background: rgba(10, 22, 40, 0.7);
    backdrop-filter: blur(15px);
    border: 2px solid rgba(255, 215, 0, 0.4);
    border-radius: 30px;
    text-shadow: 0 0 25px rgba(255, 215, 0, 0.6),
                 0 4px 8px rgba(0,0,0,0.8);
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    animation: text-enter 1s ease-out forwards;
}
@keyframes text-enter {
    from { opacity: 0; transform: translateY(40px); }
    to   { opacity: 1; transform: translateY(0); }
}
.word {
    display: inline-block;
    opacity: 0;
    animation: word-fade-in 0.4s ease forwards;
    margin: 0 6px;
}
@keyframes word-fade-in {
    to { opacity: 1; }
}
.ayah-text {
    color: #FFD700;
    font-size: 72px;
    text-shadow: 0 0 40px rgba(255, 215, 0, 0.8);
    line-height: 1.8;
}
.progress-bar {
    position: absolute;
    bottom: 0; left: 0;
    height: 6px;
    background: linear-gradient(90deg, #D4AF37, #FFD700, #FFA500);
    box-shadow: 0 0 25px #FFD700;
    z-index: 200;
    animation: progress-fill var(--duration) linear forwards;
}
@keyframes progress-fill {
    from { width: 0; }
    to   { width: 100%; }
}
"""


# ════════════════════════════════════════════════════════════════
# Three.js scene templates
# ════════════════════════════════════════════════════════════════
THREEJS_BASE = """
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
const SCENE_DATA = __SCENE_DATA__;
const W = 1920, H = 1080;

// ── Three.js setup ─────────────────────────────────
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(60, W/H, 0.1, 2000);
camera.position.set(0, 5, 30);
camera.lookAt(0, 0, 0);

const canvas = document.getElementById('three-canvas');
const renderer = new THREE.WebGLRenderer({
    canvas: canvas,
    antialias: true,
    alpha: true,
    preserveDrawingBuffer: true
});
renderer.setSize(W, H);
renderer.setPixelRatio(1);
renderer.setClearColor(0x000000, 0);

// ── Ambient lighting (Pixar-style soft) ────────────
const ambient = new THREE.AmbientLight(0xffeecc, 0.6);
scene.add(ambient);
const sun = new THREE.DirectionalLight(0xfff5e1, 1.2);
sun.position.set(20, 30, 20);
scene.add(sun);
const rim = new THREE.DirectionalLight(0xffaa66, 0.8);
rim.position.set(-15, 10, -10);
scene.add(rim);

// ── Color helpers ──────────────────────────────────
const PALETTE = SCENE_DATA.palette;
function hexToInt(hex) { return parseInt(hex.replace('#',''), 16); }

// ── SCENE BUILDERS ─────────────────────────────────
const builders = {

    garden: () => {
        // أرض خضراء
        const ground = new THREE.Mesh(
            new THREE.PlaneGeometry(80, 80),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[2])})
        );
        ground.rotation.x = -Math.PI/2;
        ground.position.y = -3;
        scene.add(ground);

        // أشجار
        for (let i = 0; i < 12; i++) {
            const trunk = new THREE.Mesh(
                new THREE.CylinderGeometry(0.3, 0.5, 3, 8),
                new THREE.MeshLambertMaterial({color: 0x6B4423})
            );
            const leaves = new THREE.Mesh(
                new THREE.SphereGeometry(2, 12, 12),
                new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[0])})
            );
            leaves.position.y = 3;
            trunk.position.set((Math.random()-0.5)*60, -1.5, -10 - Math.random()*30);
            leaves.position.copy(trunk.position);
            leaves.position.y += 3;
            // scaling for variety
            const s = 0.8 + Math.random()*0.6;
            trunk.scale.setScalar(s); leaves.scale.setScalar(s);
            scene.add(trunk); scene.add(leaves);
        }

        // فراشات بسيطة (octahedrons)
        const butterflies = [];
        for (let i = 0; i < 6; i++) {
            const b = new THREE.Mesh(
                new THREE.OctahedronGeometry(0.4),
                new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[3])})
            );
            b.position.set((Math.random()-0.5)*20, Math.random()*8, -5 - Math.random()*15);
            b.userData = {phase: Math.random()*Math.PI*2, speed: 0.3+Math.random()*0.5};
            butterflies.push(b);
            scene.add(b);
        }

        return (t) => {
            butterflies.forEach((b, i) => {
                b.position.y += Math.sin(t*b.userData.speed + b.userData.phase)*0.02;
                b.position.x += Math.cos(t*b.userData.speed*0.7 + b.userData.phase)*0.03;
                b.rotation.y = t*2;
            });
            scene.rotation.y = Math.sin(t*0.05)*0.05;
        };
    },

    sky: () => {
        // خلفية ليلية
        scene.background = new THREE.Color(hexToInt(PALETTE[0]));

        // نجوم
        const starGeo = new THREE.BufferGeometry();
        const positions = [];
        for (let i = 0; i < 800; i++) {
            positions.push((Math.random()-0.5)*200, (Math.random()-0.5)*200, -30 - Math.random()*100);
        }
        starGeo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
        const starMat = new THREE.PointsMaterial({
            color: hexToInt(PALETTE[3]),
            size: 1.2,
            transparent: true,
            opacity: 0.95,
            sizeAttenuation: true
        });
        const stars = new THREE.Points(starGeo, starMat);
        scene.add(stars);

        // قمر
        const moon = new THREE.Mesh(
            new THREE.SphereGeometry(3, 32, 32),
            new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[4])})
        );
        moon.position.set(15, 10, -25);
        scene.add(moon);

        // glow halo
        const halo = new THREE.Mesh(
            new THREE.SphereGeometry(4.5, 32, 32),
            new THREE.MeshBasicMaterial({
                color: hexToInt(PALETTE[3]),
                transparent: true, opacity: 0.15
            })
        );
        halo.position.copy(moon.position);
        scene.add(halo);

        return (t) => {
            stars.rotation.z = t*0.02;
            starMat.opacity = 0.7 + Math.sin(t*2)*0.25;
            moon.position.x = 15 + Math.sin(t*0.1)*1.5;
        };
    },

    house: () => {
        // أرض
        const ground = new THREE.Mesh(
            new THREE.PlaneGeometry(60, 60),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[2])})
        );
        ground.rotation.x = -Math.PI/2;
        ground.position.y = -2;
        scene.add(ground);

        // البيت
        const houseBody = new THREE.Mesh(
            new THREE.BoxGeometry(8, 6, 8),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[1])})
        );
        houseBody.position.set(0, 1, -10);
        scene.add(houseBody);

        // السقف
        const roof = new THREE.Mesh(
            new THREE.ConeGeometry(7, 4, 4),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[0])})
        );
        roof.rotation.y = Math.PI/4;
        roof.position.set(0, 6, -10);
        scene.add(roof);

        // نوافذ مضيئة
        const windows = [];
        for (let i = 0; i < 2; i++) {
            const w = new THREE.Mesh(
                new THREE.PlaneGeometry(1.5, 1.8),
                new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[4])})
            );
            w.position.set(-2 + i*4, 1, -5.99);
            windows.push(w);
            scene.add(w);
        }

        return (t) => {
            // وميض ناعم في النوافذ
            windows.forEach((w, i) => {
                w.material.opacity = 0.85 + Math.sin(t*1.5 + i)*0.15;
                w.material.transparent = true;
            });
            scene.rotation.y = Math.sin(t*0.08)*0.04;
        };
    },

    mosque: () => {
        scene.background = new THREE.Color(hexToInt(PALETTE[1]));

        // قاعدة المسجد
        const base = new THREE.Mesh(
            new THREE.BoxGeometry(15, 5, 12),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[2])})
        );
        base.position.set(0, 0, -15);
        scene.add(base);

        // القبة
        const dome = new THREE.Mesh(
            new THREE.SphereGeometry(4, 32, 16, 0, Math.PI*2, 0, Math.PI/2),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[0])})
        );
        dome.position.set(0, 2.5, -15);
        scene.add(dome);

        // المئذنتين
        for (let i = 0; i < 2; i++) {
            const minaret = new THREE.Mesh(
                new THREE.CylinderGeometry(0.6, 0.8, 12, 12),
                new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[2])})
            );
            minaret.position.set(-7 + i*14, 4, -15);
            scene.add(minaret);

            const cap = new THREE.Mesh(
                new THREE.ConeGeometry(0.8, 1.5, 8),
                new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[3])})
            );
            cap.position.set(-7 + i*14, 10.7, -15);
            scene.add(cap);
        }

        // الهلال
        const crescent = new THREE.Mesh(
            new THREE.TorusGeometry(0.8, 0.15, 8, 24, Math.PI*1.4),
            new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[3])})
        );
        crescent.position.set(0, 8, -15);
        crescent.rotation.z = Math.PI/2;
        scene.add(crescent);

        return (t) => {
            crescent.rotation.x = Math.sin(t*0.4)*0.1;
            scene.rotation.y = Math.sin(t*0.06)*0.03;
        };
    },

    ocean: () => {
        scene.background = new THREE.Color(hexToInt(PALETTE[0]));

        // ماء بـ wireframe متحرك
        const waterGeo = new THREE.PlaneGeometry(100, 60, 60, 30);
        const waterMat = new THREE.MeshLambertMaterial({
            color: hexToInt(PALETTE[1]),
            wireframe: false,
            side: THREE.DoubleSide
        });
        const water = new THREE.Mesh(waterGeo, waterMat);
        water.rotation.x = -Math.PI/2.5;
        water.position.y = -2;
        scene.add(water);

        // غيوم
        const clouds = [];
        for (let i = 0; i < 8; i++) {
            const c = new THREE.Mesh(
                new THREE.SphereGeometry(2, 12, 8),
                new THREE.MeshBasicMaterial({color: 0xffffff, transparent: true, opacity: 0.7})
            );
            c.position.set((Math.random()-0.5)*40, 6 + Math.random()*4, -20 - Math.random()*15);
            c.scale.set(2 + Math.random(), 1, 1);
            clouds.push(c);
            scene.add(c);
        }

        const positionAttr = water.geometry.attributes.position;
        const initialZ = [];
        for (let i = 0; i < positionAttr.count; i++) {
            initialZ.push(positionAttr.getZ(i));
        }

        return (t) => {
            for (let i = 0; i < positionAttr.count; i++) {
                const x = positionAttr.getX(i);
                const y = positionAttr.getY(i);
                positionAttr.setZ(i, initialZ[i] + Math.sin(x*0.3 + t*1.5)*0.4 + Math.cos(y*0.3 + t)*0.3);
            }
            positionAttr.needsUpdate = true;
            clouds.forEach((c, i) => {
                c.position.x += 0.02 * (i%2 === 0 ? 1 : -1);
                if (c.position.x > 25) c.position.x = -25;
                if (c.position.x < -25) c.position.x = 25;
            });
        };
    },

    desert: () => {
        scene.background = new THREE.Color(hexToInt(PALETTE[1]));

        // كثبان
        const dune = new THREE.Mesh(
            new THREE.PlaneGeometry(120, 60, 30, 15),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[2])})
        );
        dune.rotation.x = -Math.PI/2.2;
        dune.position.y = -3;
        const positions = dune.geometry.attributes.position;
        for (let i = 0; i < positions.count; i++) {
            positions.setZ(i, Math.sin(positions.getX(i)*0.15)*1.5 + Math.cos(positions.getY(i)*0.2)*1.2);
        }
        positions.needsUpdate = true;
        scene.add(dune);

        // الشمس
        const sun = new THREE.Mesh(
            new THREE.SphereGeometry(4, 32, 32),
            new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[3])})
        );
        sun.position.set(10, 8, -25);
        scene.add(sun);

        const sunGlow = new THREE.Mesh(
            new THREE.SphereGeometry(7, 32, 32),
            new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[4]), transparent: true, opacity: 0.3})
        );
        sunGlow.position.copy(sun.position);
        scene.add(sunGlow);

        return (t) => {
            sun.position.x = 10 + Math.sin(t*0.1)*1;
            sunGlow.position.copy(sun.position);
            sunGlow.material.opacity = 0.25 + Math.sin(t*1)*0.1;
        };
    },

    mountains: () => {
        scene.background = new THREE.Color(hexToInt(PALETTE[1]));

        // 3 جبال
        for (let i = 0; i < 3; i++) {
            const mountain = new THREE.Mesh(
                new THREE.ConeGeometry(7 + i*2, 12 + i*3, 6),
                new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[i % PALETTE.length])})
            );
            mountain.position.set(-12 + i*12, 2, -20 - i*3);
            scene.add(mountain);
        }

        // أرض
        const ground = new THREE.Mesh(
            new THREE.PlaneGeometry(100, 50),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[2])})
        );
        ground.rotation.x = -Math.PI/2;
        ground.position.y = -4;
        scene.add(ground);

        return (t) => {
            scene.rotation.y = Math.sin(t*0.05)*0.05;
        };
    },

    child_praying: () => {
        scene.background = new THREE.Color(hexToInt(PALETTE[1]));

        // طفل يصلي (silhouette بأشكال هندسية)
        const body = new THREE.Mesh(
            new THREE.SphereGeometry(1.5, 16, 16, 0, Math.PI*2, 0, Math.PI/2),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[0])})
        );
        body.position.set(0, -1, -8);
        scene.add(body);

        const head = new THREE.Mesh(
            new THREE.SphereGeometry(0.7, 16, 16),
            new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[3])})
        );
        head.position.set(0, 0.3, -8);
        scene.add(head);

        // نور مقدس فوقه
        const halo = new THREE.Mesh(
            new THREE.RingGeometry(1.2, 1.5, 32),
            new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[3]), transparent: true, opacity: 0.6})
        );
        halo.position.set(0, 1.5, -8);
        halo.rotation.x = Math.PI/2;
        scene.add(halo);

        // ضوء سماوي ينزل
        for (let i = 0; i < 5; i++) {
            const ray = new THREE.Mesh(
                new THREE.PlaneGeometry(0.3, 12),
                new THREE.MeshBasicMaterial({color: hexToInt(PALETTE[3]), transparent: true, opacity: 0.3})
            );
            ray.position.set((Math.random()-0.5)*4, 5, -8);
            ray.rotation.z = (Math.random()-0.5)*0.3;
            scene.add(ray);
        }

        return (t) => {
            halo.material.opacity = 0.4 + Math.sin(t*1.5)*0.25;
            halo.scale.setScalar(1 + Math.sin(t)*0.1);
        };
    },

    family: () => {
        scene.background = new THREE.Color(hexToInt(PALETTE[2]));

        // قلوب متطايرة
        const hearts = [];
        for (let i = 0; i < 15; i++) {
            const h = new THREE.Mesh(
                new THREE.SphereGeometry(0.5, 12, 12),
                new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[0])})
            );
            h.position.set(
                (Math.random()-0.5)*30,
                -5 + Math.random()*10,
                -8 - Math.random()*15
            );
            h.userData = {speed: 0.3 + Math.random()*0.5, phase: Math.random()*Math.PI*2};
            hearts.push(h);
            scene.add(h);
        }

        return (t) => {
            hearts.forEach(h => {
                h.position.y += 0.015 * h.userData.speed;
                h.position.x += Math.sin(t + h.userData.phase)*0.01;
                if (h.position.y > 8) h.position.y = -5;
                h.scale.setScalar(0.8 + Math.sin(t*2 + h.userData.phase)*0.3);
            });
        };
    },

    abstract_warm: () => {
        scene.background = new THREE.Color(hexToInt(PALETTE[1]));

        // أشكال هندسية متحركة
        const shapes = [];
        const geometries = [
            new THREE.IcosahedronGeometry(1.5),
            new THREE.OctahedronGeometry(1.5),
            new THREE.TetrahedronGeometry(1.5),
            new THREE.DodecahedronGeometry(1.5),
        ];
        for (let i = 0; i < 8; i++) {
            const s = new THREE.Mesh(
                geometries[i % geometries.length],
                new THREE.MeshLambertMaterial({color: hexToInt(PALETTE[i % PALETTE.length])})
            );
            s.position.set((Math.random()-0.5)*25, (Math.random()-0.5)*10, -10 - Math.random()*15);
            s.userData = {
                rx: 0.3 + Math.random()*0.5,
                ry: 0.3 + Math.random()*0.5,
                phase: Math.random()*Math.PI*2
            };
            shapes.push(s);
            scene.add(s);
        }

        return (t) => {
            shapes.forEach(s => {
                s.rotation.x = t * s.userData.rx;
                s.rotation.y = t * s.userData.ry;
                s.position.y += Math.sin(t + s.userData.phase)*0.01;
            });
        };
    },
};

// ── Build current scene ────────────────────────────
const updateScene = builders[SCENE_DATA.scene_type] ?
    builders[SCENE_DATA.scene_type]() :
    builders.abstract_warm();

// ── Animation loop ─────────────────────────────────
let startTime = performance.now();
function animate() {
    const t = (performance.now() - startTime) / 1000;
    if (updateScene) updateScene(t);

    // Ken Burns: حركة كاميرا بطيئة
    camera.position.x = Math.sin(t * 0.08) * 1.5;
    camera.position.z = 30 - t * 0.15;  // zoom-in بطيء جداً
    camera.lookAt(0, 0, 0);

    renderer.render(scene, camera);
    requestAnimationFrame(animate);
}
animate();
</script>
"""


# ════════════════════════════════════════════════════════════════
# HTML Template Generator
# ════════════════════════════════════════════════════════════════
def build_scene_html(
    scene_type: str,
    palette: List[str],
    narrator_text: str,
    duration: float,
    is_ayah: bool = False,
    logo_uri: str = "",
    keywords: List[str] = None,
) -> str:
    """يبني HTML كامل لمشهد procedural."""
    import json as jsonlib

    keywords = keywords or []

    scene_data = jsonlib.dumps({
        "scene_type": scene_type,
        "palette": palette,
        "duration": duration,
        "keywords": keywords,
    }, ensure_ascii=False)

    threejs_block = THREEJS_BASE.replace("__SCENE_DATA__", scene_data)

    # word-by-word rendering للنص
    words_html = ""
    if narrator_text and not is_ayah:
        words = narrator_text.split()
        n = len(words)
        # كل كلمة تظهر بدورها
        for i, w in enumerate(words):
            delay = (i / max(n, 1)) * min(duration * 0.6, 3.0)  # توزع على 60% من المدة
            words_html += f'<span class="word" style="animation-delay:{delay:.2f}s">{w}</span>'

    text_html = ""
    if is_ayah and narrator_text:
        text_html = f'<div class="text-container"><div class="narrator-text ayah-text">{narrator_text}</div></div>'
    elif narrator_text:
        text_html = f'<div class="text-container"><div class="narrator-text">{words_html}</div></div>'

    logo_html = f'<img src="{logo_uri}" class="logo-overlay" alt="logo">' if logo_uri else ""

    # Particles via CSS
    particles_html = ""
    for i in range(ProceduralConfig.PARTICLE_COUNT):
        import random
        x = random.uniform(0, 100)
        y = random.uniform(0, 100)
        size = random.uniform(2, 5)
        delay = random.uniform(0, 5)
        dur = random.uniform(8, 15)
        color = palette[i % len(palette)]
        particles_html += (
            f'<div class="particle" style="'
            f'left:{x:.1f}%;top:{y:.1f}%;'
            f'width:{size:.1f}px;height:{size:.1f}px;'
            f'background:{color};'
            f'box-shadow:0 0 {size*3:.0f}px {color};'
            f'animation:float-particle {dur:.1f}s linear {delay:.1f}s infinite;'
            f'opacity:0.7;"></div>'
        )

    # Particles keyframes
    particles_css = """
    @keyframes float-particle {
        0%   { transform: translateY(0) translateX(0) scale(1); opacity: 0; }
        10%  { opacity: 0.7; }
        90%  { opacity: 0.7; }
        100% { transform: translateY(-200px) translateX(20px) scale(0.5); opacity: 0; }
    }
    """

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
{COMMON_CSS}
{particles_css}
</style>
</head>
<body>
<div class="scene-container">
    <canvas id="three-canvas"></canvas>
    <div class="gradient-overlay"></div>
    <div class="particles-layer">
        {particles_html}
    </div>
    {logo_html}
    {text_html}
    <div class="progress-bar" style="--duration:{duration}s;"></div>
</div>
{threejs_block}
</body>
</html>
"""


# ════════════════════════════════════════════════════════════════
# Main VisualEngine class
# ════════════════════════════════════════════════════════════════
class VisualEngine:
    """
    في v10: ما يولّدش صور — يبني templates HTML اللي الـ video_engine يرسمها.
    أي استدعاء قديم لـ generate_episode_visuals بيُتجاهل (no-op).
    """

    def __init__(self):
        logger.info("✅ Procedural VisualEngine جاهز (Three.js + SVG)")

    def generate_episode_visuals(self, script, ep_dir: str) -> None:
        """No-op: المشاهد بتترسم في video_engine باستخدام visual_scene + palette."""
        logger.info("ℹ️ Procedural mode: تخطّي توليد الصور (سترسم وقت الرندرة)")

    @staticmethod
    def get_palette(palette_name: str) -> List[str]:
        return ProceduralConfig.PALETTES.get(palette_name, ProceduralConfig.PALETTES["warm_sunset"])

    @staticmethod
    def render_scene_html(scene_type, palette_name, text, duration, is_ayah=False, logo_uri="", keywords=None):
        """يستخدمها video_engine مباشرة."""
        palette = VisualEngine.get_palette(palette_name)
        return build_scene_html(scene_type, palette, text, duration, is_ayah, logo_uri, keywords or [])
