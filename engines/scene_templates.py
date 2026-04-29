"""
engines/scene_templates.py — VALUE / QEEMA v11.0 (Production)
==================================================================
HTML/CSS/Three.js templates for procedural scene rendering.

[Approach]
Each scene produces a self-contained HTML file that:
  1. Renders a Three.js or pure-CSS scene matching the scene_type
  2. Animates with requestAnimationFrame
  3. Overlays the narrator text with word-by-word fade-in
  4. Has a bottom progress bar synced to audio duration
  5. Shows the QEEMA logo top-right

[Why pure HTML?]
- Playwright records the browser viewport directly to webm
- No need for image generation APIs (Leonardo etc. removed)
- Every scene is procedural and unique per-render

[Performance]
- All textures generated procedurally (no asset loading)
- Three.js loaded from CDN (cached after first scene)
- Word-by-word transitions are CSS animations (GPU-accelerated)
"""
from __future__ import annotations

import html as html_lib
import json
import textwrap
from typing import Dict, List

from core.config import PALETTES


# ════════════════════════════════════════════════════════════════
# Common: CSS reset + overlays + typography
# ════════════════════════════════════════════════════════════════
_COMMON_CSS: str = """\
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
    width: 100%; height: 100%;
    background: #000;
    overflow: hidden;
    font-family: 'Amiri', 'Scheherazade New', 'Traditional Arabic', serif;
    direction: rtl;
}
.scene-container {
    position: fixed; inset: 0;
    width: 100vw; height: 100vh;
}
canvas {
    position: absolute; top: 0; left: 0;
    width: 100% !important;
    height: 100% !important;
    display: block;
}
.gradient-overlay {
    position: absolute; inset: 0;
    background: linear-gradient(
        180deg,
        rgba(0,0,0,0.0) 0%,
        rgba(0,0,0,0.0) 50%,
        rgba(0,0,0,0.55) 100%
    );
    pointer-events: none;
    z-index: 5;
}
.particles-layer {
    position: absolute; inset: 0;
    pointer-events: none;
    z-index: 4;
    overflow: hidden;
}
.particle {
    position: absolute;
    border-radius: 50%;
    pointer-events: none;
    will-change: transform, opacity;
    animation-iteration-count: infinite;
    animation-timing-function: linear;
}

/* QEEMA logo top-right */
.logo-overlay {
    position: absolute;
    top: 40px; right: 60px;
    z-index: 10;
    color: #FFD700;
    font-size: 48px;
    font-weight: 900;
    text-shadow:
        0 0 20px rgba(255, 215, 0, 0.6),
        0 4px 12px rgba(0, 0, 0, 0.7);
    letter-spacing: 2px;
    animation: logoPulse 3s ease-in-out infinite;
}
.logo-overlay .en {
    font-size: 22px;
    color: #FFE5B4;
    display: block;
    text-align: center;
    margin-top: -4px;
    letter-spacing: 4px;
}
@keyframes logoPulse {
    0%, 100% { transform: scale(1); opacity: 0.95; }
    50%      { transform: scale(1.04); opacity: 1.0; }
}

/* Narrator / Ayah text container */
.text-container {
    position: absolute;
    bottom: 100px; left: 50%;
    transform: translateX(-50%);
    width: 85%; max-width: 1500px;
    padding: 40px 60px;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border: 2px solid rgba(255, 215, 0, 0.4);
    border-radius: 24px;
    box-shadow:
        0 12px 40px rgba(0, 0, 0, 0.5),
        inset 0 1px 0 rgba(255, 255, 255, 0.1);
    z-index: 8;
}
.narrator-text {
    color: #FFFFFF;
    font-size: 56px;
    font-weight: 700;
    line-height: 1.6;
    text-align: center;
    text-shadow: 0 2px 12px rgba(0,0,0,0.9);
}
.ayah-text {
    color: #FFD700;
    font-size: 72px;
    font-weight: 900;
    line-height: 1.7;
    text-align: center;
    text-shadow:
        0 0 24px rgba(255, 215, 0, 0.5),
        0 2px 12px rgba(0,0,0,0.9);
}
.word {
    display: inline-block;
    margin: 0 6px;
    opacity: 0;
    transform: translateY(20px);
    animation: wordFadeIn 0.6s ease-out forwards;
}
@keyframes wordFadeIn {
    to { opacity: 1; transform: translateY(0); }
}

/* Bottom progress bar */
.progress-bar-track {
    position: absolute;
    bottom: 0; left: 0;
    height: 6px; width: 100%;
    background: rgba(0,0,0,0.4);
    z-index: 9;
}
.progress-bar-fill {
    height: 100%; width: 0;
    background: linear-gradient(
        90deg,
        #FFD700 0%, #FFCC70 50%, #FFB347 100%
    );
    box-shadow: 0 0 10px rgba(255, 215, 0, 0.8);
    transition: width linear;
}
"""


# ════════════════════════════════════════════════════════════════
# Word-wrapping helper
# ════════════════════════════════════════════════════════════════
def _wrap_words_html(text: str, *, stagger_ms: int = 280) -> str:
    """Wrap each word in a <span> with staggered animation delay."""
    if not text:
        return ""
    words: List[str] = text.split()
    spans: List[str] = []
    for i, w in enumerate(words):
        delay = i * stagger_ms
        safe = html_lib.escape(w)
        spans.append(
            f'<span class="word" style="animation-delay:{delay}ms">{safe}</span>'
        )
    return " ".join(spans)


# ════════════════════════════════════════════════════════════════
# Scene-specific Three.js builders
# ════════════════════════════════════════════════════════════════
def _scene_garden_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.fog = new THREE.Fog(0x{palette[2][1:]}, 8, 30);

    // Sky-tinted background
    scene.background = new THREE.Color(0x{palette[0][1:]});

    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 100);
    camera.position.set(0, 3, 10);

    const renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: false }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    // Lighting
    scene.add(new THREE.AmbientLight(0xfff5e6, 0.7));
    const sun = new THREE.DirectionalLight(0xffeeaa, 1.1);
    sun.position.set(5, 10, 5);
    scene.add(sun);

    // Ground
    const ground = new THREE.Mesh(
        new THREE.PlaneGeometry(40, 40),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -1;
    scene.add(ground);

    // Trees (low-poly cones)
    const trees = [];
    const treeColors = [0x{palette[2][1:]}, 0x{palette[3][1:]}];
    for (let i = 0; i < 14; i++) {{
        const trunk = new THREE.Mesh(
            new THREE.CylinderGeometry(0.15, 0.2, 1.2, 6),
            new THREE.MeshStandardMaterial({{ color: 0x6B4226 }})
        );
        const top = new THREE.Mesh(
            new THREE.ConeGeometry(0.9, 1.6, 7),
            new THREE.MeshStandardMaterial({{
                color: treeColors[i % treeColors.length]
            }})
        );
        top.position.y = 1.4;
        const tree = new THREE.Group();
        tree.add(trunk);
        tree.add(top);
        const r = 4 + Math.random() * 8;
        const a = Math.random() * Math.PI * 2;
        tree.position.set(Math.cos(a) * r, -0.3, Math.sin(a) * r);
        const s = 0.7 + Math.random() * 0.6;
        tree.scale.set(s, s, s);
        scene.add(tree);
        trees.push(tree);
    }}

    function animate(t) {{
        requestAnimationFrame(animate);
        camera.position.x = Math.sin(t * 0.0002) * 1.2;
        camera.lookAt(0, 1, 0);
        trees.forEach((tr, i) => {{
            tr.rotation.z = Math.sin(t * 0.001 + i) * 0.04;
        }});
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_sky_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});

    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 200);
    camera.position.set(0, 0, 14);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    // Stars (instanced points)
    const starGeo = new THREE.BufferGeometry();
    const starCount = 800;
    const positions = new Float32Array(starCount * 3);
    const sizes = new Float32Array(starCount);
    for (let i = 0; i < starCount; i++) {{
        positions[i*3] = (Math.random() - 0.5) * 100;
        positions[i*3+1] = (Math.random() - 0.5) * 60;
        positions[i*3+2] = (Math.random() - 0.5) * 80 - 20;
        sizes[i] = 0.8 + Math.random() * 1.6;
    }}
    starGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const stars = new THREE.Points(
        starGeo,
        new THREE.PointsMaterial({{
            color: 0x{palette[3][1:]},
            size: 0.15,
            transparent: true,
            opacity: 0.9,
        }})
    );
    scene.add(stars);

    // Moon
    const moon = new THREE.Mesh(
        new THREE.SphereGeometry(1.4, 32, 32),
        new THREE.MeshBasicMaterial({{ color: 0x{palette[3][1:]} }})
    );
    moon.position.set(5, 4, -10);
    scene.add(moon);

    // Halo
    const halo = new THREE.Mesh(
        new THREE.RingGeometry(1.6, 2.4, 64),
        new THREE.MeshBasicMaterial({{
            color: 0x{palette[4][1:]},
            transparent: true,
            opacity: 0.3,
            side: THREE.DoubleSide,
        }})
    );
    halo.position.copy(moon.position);
    scene.add(halo);

    function animate(t) {{
        requestAnimationFrame(animate);
        stars.rotation.z = t * 0.00005;
        moon.rotation.y = t * 0.0002;
        halo.rotation.z = -t * 0.0003;
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_house_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});
    scene.fog = new THREE.Fog(0x{palette[1][1:]}, 10, 40);

    const camera = new THREE.PerspectiveCamera(55, w/h, 0.1, 100);
    camera.position.set(4, 4, 10);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xffe9c0, 0.8));
    const lamp = new THREE.PointLight(0xffd700, 1.2, 20);
    lamp.position.set(0, 2, 3);
    scene.add(lamp);

    // House body
    const house = new THREE.Mesh(
        new THREE.BoxGeometry(4, 3, 3),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[2][1:]} }})
    );
    house.position.y = 0.5;
    scene.add(house);

    // Roof
    const roof = new THREE.Mesh(
        new THREE.ConeGeometry(3, 1.5, 4),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[3][1:]} }})
    );
    roof.position.y = 2.75;
    roof.rotation.y = Math.PI / 4;
    scene.add(roof);

    // Windows (glowing)
    const winMat = new THREE.MeshBasicMaterial({{ color: 0xffe066 }});
    const positions = [[-1, 0.7, 1.51], [1, 0.7, 1.51]];
    positions.forEach(p => {{
        const w_mesh = new THREE.Mesh(
            new THREE.PlaneGeometry(0.6, 0.6),
            winMat
        );
        w_mesh.position.set(p[0], p[1], p[2]);
        scene.add(w_mesh);
    }});

    // Ground
    const ground = new THREE.Mesh(
        new THREE.CircleGeometry(20, 32),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -1;
    scene.add(ground);

    function animate(t) {{
        requestAnimationFrame(animate);
        camera.position.x = 4 + Math.sin(t * 0.0003) * 0.6;
        camera.lookAt(0, 1, 0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_mosque_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});

    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 100);
    camera.position.set(0, 4, 12);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xfff0d0, 0.9));
    const dl = new THREE.DirectionalLight(0xffe4a3, 1.0);
    dl.position.set(0, 8, 4);
    scene.add(dl);

    // Base
    const base = new THREE.Mesh(
        new THREE.BoxGeometry(6, 2, 4),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[2][1:]} }})
    );
    base.position.y = 0;
    scene.add(base);

    // Dome
    const dome = new THREE.Mesh(
        new THREE.SphereGeometry(1.8, 32, 24, 0, Math.PI * 2, 0, Math.PI / 2),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[3][1:]} }})
    );
    dome.position.y = 1.0;
    scene.add(dome);

    // Crescent
    const crescent = new THREE.Mesh(
        new THREE.TorusGeometry(0.3, 0.08, 12, 24, Math.PI),
        new THREE.MeshBasicMaterial({{ color: 0x{palette[4][1:]} }})
    );
    crescent.position.set(0, 3.2, 0);
    scene.add(crescent);

    // Minarets (left, right)
    [-3.5, 3.5].forEach(x => {{
        const min_body = new THREE.Mesh(
            new THREE.CylinderGeometry(0.25, 0.3, 4, 12),
            new THREE.MeshStandardMaterial({{ color: 0x{palette[2][1:]} }})
        );
        min_body.position.set(x, 1, -1.4);
        scene.add(min_body);
        const min_top = new THREE.Mesh(
            new THREE.ConeGeometry(0.35, 0.7, 8),
            new THREE.MeshStandardMaterial({{ color: 0x{palette[3][1:]} }})
        );
        min_top.position.set(x, 3.4, -1.4);
        scene.add(min_top);
    }});

    // Ground
    const ground = new THREE.Mesh(
        new THREE.PlaneGeometry(40, 40),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -1;
    scene.add(ground);

    function animate(t) {{
        requestAnimationFrame(animate);
        camera.position.x = Math.sin(t * 0.00025) * 2;
        camera.lookAt(0, 1.5, 0);
        crescent.rotation.z = Math.sin(t * 0.0008) * 0.1;
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_ocean_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});

    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 200);
    camera.position.set(0, 5, 10);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const sun = new THREE.DirectionalLight(0xffd700, 1.0);
    sun.position.set(0, 10, 5);
    scene.add(sun);

    // Animated wave plane
    const wave_geo = new THREE.PlaneGeometry(60, 60, 80, 80);
    const wave_mat = new THREE.MeshStandardMaterial({{
        color: 0x{palette[2][1:]},
        flatShading: true,
        wireframe: false,
    }});
    const waves = new THREE.Mesh(wave_geo, wave_mat);
    waves.rotation.x = -Math.PI / 2;
    waves.position.y = -1;
    scene.add(waves);
    const wave_pos = wave_geo.attributes.position;

    // Clouds
    const clouds = [];
    for (let i = 0; i < 6; i++) {{
        const c = new THREE.Mesh(
            new THREE.SphereGeometry(0.8, 12, 12),
            new THREE.MeshBasicMaterial({{
                color: 0xffffff,
                transparent: true,
                opacity: 0.7,
            }})
        );
        c.position.set(
            -10 + Math.random() * 20,
            5 + Math.random() * 3,
            -10 + Math.random() * 5
        );
        c.scale.set(2 + Math.random() * 2, 1, 1);
        scene.add(c);
        clouds.push(c);
    }}

    function animate(t) {{
        requestAnimationFrame(animate);
        for (let i = 0; i < wave_pos.count; i++) {{
            const x = wave_pos.getX(i);
            const y = wave_pos.getY(i);
            wave_pos.setZ(
                i,
                Math.sin(x * 0.3 + t * 0.001) * 0.3 +
                Math.cos(y * 0.4 + t * 0.0012) * 0.25
            );
        }}
        wave_pos.needsUpdate = true;
        clouds.forEach((c, i) => {{
            c.position.x += 0.005 + i * 0.001;
            if (c.position.x > 12) c.position.x = -12;
        }});
        camera.position.y = 5 + Math.sin(t * 0.0003) * 0.5;
        camera.lookAt(0, 0, 0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_desert_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});
    scene.fog = new THREE.Fog(0x{palette[1][1:]}, 12, 50);

    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 100);
    camera.position.set(0, 3, 10);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xfff0d0, 0.8));
    const sun = new THREE.DirectionalLight(0xffcc70, 1.3);
    sun.position.set(0, 10, 0);
    scene.add(sun);

    // Sun (visual)
    const sun_disk = new THREE.Mesh(
        new THREE.CircleGeometry(2, 32),
        new THREE.MeshBasicMaterial({{ color: 0x{palette[3][1:]} }})
    );
    sun_disk.position.set(0, 6, -20);
    scene.add(sun_disk);

    // Dunes (rolling cosine)
    const dune_geo = new THREE.PlaneGeometry(80, 40, 60, 30);
    for (let i = 0; i < dune_geo.attributes.position.count; i++) {{
        const x = dune_geo.attributes.position.getX(i);
        const y = dune_geo.attributes.position.getY(i);
        dune_geo.attributes.position.setZ(
            i,
            Math.sin(x * 0.15) * 0.7 + Math.cos(y * 0.2) * 0.6
        );
    }}
    dune_geo.computeVertexNormals();
    const dunes = new THREE.Mesh(
        dune_geo,
        new THREE.MeshStandardMaterial({{
            color: 0x{palette[2][1:]},
            flatShading: true,
        }})
    );
    dunes.rotation.x = -Math.PI / 2;
    dunes.position.y = -1;
    scene.add(dunes);

    function animate(t) {{
        requestAnimationFrame(animate);
        camera.position.x = Math.sin(t * 0.0002) * 2;
        camera.lookAt(0, 2, -10);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_mountains_js(palette: List[str]) -> str:
    color_a = palette[2][1:]
    color_b = palette[3][1:]
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});
    scene.fog = new THREE.Fog(0x{palette[1][1:]}, 5, 35);

    const camera = new THREE.PerspectiveCamera(55, w/h, 0.1, 100);
    camera.position.set(0, 3, 12);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xfff5e1, 0.6));
    const dl = new THREE.DirectionalLight(0xffcc70, 1.1);
    dl.position.set(5, 8, 3);
    scene.add(dl);

    // Mountains as cones
    const mtColors = [0x{color_a}, 0x{color_b}];
    const mts = [];
    for (let i = 0; i < 7; i++) {{
        const h_mt = 4 + Math.random() * 3;
        const mt = new THREE.Mesh(
            new THREE.ConeGeometry(2 + Math.random(), h_mt, 5),
            new THREE.MeshStandardMaterial({{
                color: mtColors[i % mtColors.length],
                flatShading: true,
            }})
        );
        mt.position.set(-12 + i * 3.5, h_mt / 2 - 1, -5 - Math.random() * 5);
        scene.add(mt);
        mts.push(mt);
    }}

    // Ground
    const ground = new THREE.Mesh(
        new THREE.PlaneGeometry(60, 60),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -1;
    scene.add(ground);

    function animate(t) {{
        requestAnimationFrame(animate);
        camera.position.x = Math.sin(t * 0.0002) * 2;
        camera.lookAt(0, 2, -5);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_child_praying_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});

    const camera = new THREE.PerspectiveCamera(55, w/h, 0.1, 100);
    camera.position.set(0, 2, 7);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xfff5e1, 0.85));

    // Child silhouette: simplified body (cone + sphere)
    const body = new THREE.Mesh(
        new THREE.ConeGeometry(0.6, 1.4, 16),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[3][1:]} }})
    );
    body.position.y = 0;
    scene.add(body);
    const head = new THREE.Mesh(
        new THREE.SphereGeometry(0.35, 16, 16),
        new THREE.MeshStandardMaterial({{ color: 0xffd9ad }})
    );
    head.position.y = 1.0;
    scene.add(head);

    // Halo above head
    const halo = new THREE.Mesh(
        new THREE.RingGeometry(0.4, 0.55, 32),
        new THREE.MeshBasicMaterial({{
            color: 0x{palette[4][1:]},
            transparent: true,
            opacity: 0.7,
            side: THREE.DoubleSide,
        }})
    );
    halo.position.y = 1.5;
    halo.rotation.x = -Math.PI / 2;
    scene.add(halo);

    // Light rays (cone, transparent)
    const rays = new THREE.Mesh(
        new THREE.ConeGeometry(2, 6, 32, 1, true),
        new THREE.MeshBasicMaterial({{
            color: 0x{palette[4][1:]},
            transparent: true,
            opacity: 0.18,
            side: THREE.DoubleSide,
        }})
    );
    rays.position.y = 4;
    rays.rotation.z = Math.PI;
    scene.add(rays);

    // Ground
    const ground = new THREE.Mesh(
        new THREE.CircleGeometry(20, 32),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.7;
    scene.add(ground);

    function animate(t) {{
        requestAnimationFrame(animate);
        halo.rotation.z = t * 0.0005;
        rays.rotation.y = t * 0.0003;
        body.rotation.y = Math.sin(t * 0.0006) * 0.1;
        camera.position.x = Math.sin(t * 0.0002) * 0.5;
        camera.lookAt(0, 1, 0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_family_js(palette: List[str]) -> str:
    body_a = palette[2][1:]
    body_b = palette[3][1:]
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});

    const camera = new THREE.PerspectiveCamera(55, w/h, 0.1, 100);
    camera.position.set(0, 2, 8);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xfff0d0, 0.9));

    // Three figures (cone bodies + sphere heads)
    const bodyColors = [0x{body_a}, 0x{body_b}];
    const figures = [];
    const xs = [-2.2, 0, 2.2];
    const heights = [1.6, 1.8, 1.2];
    xs.forEach((x, i) => {{
        const body = new THREE.Mesh(
            new THREE.ConeGeometry(0.5, heights[i], 16),
            new THREE.MeshStandardMaterial({{ color: bodyColors[i % bodyColors.length] }})
        );
        body.position.set(x, heights[i] / 2 - 1, 0);
        const head = new THREE.Mesh(
            new THREE.SphereGeometry(0.28, 16, 16),
            new THREE.MeshStandardMaterial({{ color: 0xffd9ad }})
        );
        head.position.set(x, heights[i] - 0.6, 0);
        scene.add(body);
        scene.add(head);
        figures.push({{body, head}});
    }});

    // Floating hearts
    const hearts = [];
    for (let i = 0; i < 25; i++) {{
        const h = new THREE.Mesh(
            new THREE.SphereGeometry(0.12, 8, 8),
            new THREE.MeshBasicMaterial({{
                color: 0x{palette[3][1:]},
                transparent: true,
                opacity: 0.85,
            }})
        );
        h.position.set(
            -5 + Math.random() * 10,
            Math.random() * 4,
            -2 + Math.random() * 4
        );
        scene.add(h);
        hearts.push({{
            mesh: h,
            speed: 0.005 + Math.random() * 0.01,
        }});
    }}

    // Ground
    const ground = new THREE.Mesh(
        new THREE.CircleGeometry(20, 32),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -1;
    scene.add(ground);

    function animate(t) {{
        requestAnimationFrame(animate);
        hearts.forEach((h) => {{
            h.mesh.position.y += h.speed;
            if (h.mesh.position.y > 5) h.mesh.position.y = -1;
            h.mesh.rotation.z = Math.sin(t * 0.001 + h.mesh.position.x) * 0.3;
        }});
        figures.forEach((f, i) => {{
            f.body.rotation.y = Math.sin(t * 0.0006 + i) * 0.08;
            f.head.position.y = (heights[i] - 0.6) + Math.sin(t * 0.001 + i) * 0.04;
        }});
        camera.position.x = Math.sin(t * 0.00025) * 1.0;
        camera.lookAt(0, 1, 0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_abstract_warm_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});

    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 100);
    camera.position.set(0, 0, 10);

    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
    const dl = new THREE.DirectionalLight(0xfff0d0, 1.0);
    dl.position.set(5, 5, 5);
    scene.add(dl);

    // Rotating polyhedra cluster
    const colors = [
        0x{palette[1][1:]}, 0x{palette[2][1:]},
        0x{palette[3][1:]}, 0x{palette[4][1:]},
    ];
    const shapes = [];
    for (let i = 0; i < 12; i++) {{
        const geo = i % 2 === 0
            ? new THREE.IcosahedronGeometry(0.6, 0)
            : new THREE.OctahedronGeometry(0.7, 0);
        const m = new THREE.Mesh(
            geo,
            new THREE.MeshStandardMaterial({{
                color: colors[i % colors.length],
                flatShading: true,
            }})
        );
        const r = 3 + Math.random() * 3;
        const a = (i / 12) * Math.PI * 2;
        m.position.set(Math.cos(a) * r, Math.sin(a) * r * 0.6, -Math.random() * 4);
        scene.add(m);
        shapes.push(m);
    }}

    function animate(t) {{
        requestAnimationFrame(animate);
        shapes.forEach((s, i) => {{
            s.rotation.x = t * 0.0005 * (1 + i * 0.05);
            s.rotation.y = t * 0.0007 * (1 + i * 0.03);
        }});
        camera.position.x = Math.sin(t * 0.0003) * 1.5;
        camera.position.y = Math.cos(t * 0.0002) * 0.8;
        camera.lookAt(0, 0, 0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


# ════════════════════════════════════════════════════════════════
# Scene builder dispatch
# ════════════════════════════════════════════════════════════════
_SCENE_BUILDERS: Dict[str, callable] = {
    "garden":        _scene_garden_js,
    "sky":           _scene_sky_js,
    "house":         _scene_house_js,
    "mosque":        _scene_mosque_js,
    "ocean":         _scene_ocean_js,
    "desert":        _scene_desert_js,
    "mountains":     _scene_mountains_js,
    "child_praying": _scene_child_praying_js,
    "family":        _scene_family_js,
    "abstract_warm": _scene_abstract_warm_js,
}


# ════════════════════════════════════════════════════════════════
# Particle CSS (color-aware)
# ════════════════════════════════════════════════════════════════
def _build_particles_html(palette: List[str], count: int = 60) -> str:
    """Generate inline CSS particles (golden/colored sparkles)."""
    import random
    rng = random.Random(42)  # deterministic per palette
    color = palette[3] if len(palette) > 3 else "#FFD700"

    parts: List[str] = []
    for i in range(count):
        size = rng.uniform(2, 6)
        left = rng.uniform(0, 100)
        delay = rng.uniform(0, 8)
        duration = rng.uniform(8, 16)
        opacity = rng.uniform(0.4, 0.9)
        parts.append(
            f'<div class="particle" style="'
            f'left: {left:.1f}vw; bottom: -10px; '
            f'width: {size:.1f}px; height: {size:.1f}px; '
            f'background: {color}; '
            f'opacity: {opacity:.2f}; '
            f'box-shadow: 0 0 {size * 2:.0f}px {color}; '
            f'animation: particleRise {duration:.1f}s linear {delay:.1f}s infinite;'
            f'"></div>'
        )

    css = """
    @keyframes particleRise {
        0%   { transform: translateY(0) translateX(0); opacity: 0; }
        10%  { opacity: var(--p-opacity, 0.7); }
        90%  { opacity: var(--p-opacity, 0.7); }
        100% { transform: translateY(-110vh) translateX(20px); opacity: 0; }
    }
    """
    return f'<style>{css}</style><div class="particles-layer">{"".join(parts)}</div>'


# ════════════════════════════════════════════════════════════════
# Public: build_scene_html
# ════════════════════════════════════════════════════════════════
def build_scene_html(
    *,
    scene_type: str,
    palette_name: str,
    text: str,
    is_ayah: bool,
    duration_sec: float,
    channel_name_ar: str = "قِيمَة",
    channel_name_en: str = "VALUE",
    particle_count: int = 60,
) -> str:
    """
    Build full HTML for one scene.
    Returns a complete <html>...</html> string.
    """
    palette = PALETTES.get(palette_name, PALETTES["warm_sunset"])
    builder = _SCENE_BUILDERS.get(scene_type, _scene_abstract_warm_js)

    text_class = "ayah-text" if is_ayah else "narrator-text"
    word_html = _wrap_words_html(text)

    # Word count drives total stagger duration
    word_count = max(len(text.split()), 1)
    progress_duration_ms = int(duration_sec * 1000)
    initial_delay_ms = 100  # let first frame render before progress starts

    safe_ar = html_lib.escape(channel_name_ar)
    safe_en = html_lib.escape(channel_name_en)

    threejs_setup = textwrap.dedent("""\
        const w = window.innerWidth;
        const h = window.innerHeight;
    """)

    js_scene = builder(palette)

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>QEEMA Scene</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Amiri:wght@400;700&display=swap" rel="stylesheet">
<style>{_COMMON_CSS}</style>
</head>
<body>
<div class="scene-container">
    <div class="gradient-overlay"></div>
    {_build_particles_html(palette, particle_count)}
    <div class="logo-overlay">
        {safe_ar}
        <span class="en">{safe_en}</span>
    </div>
    <div class="text-container">
        <div class="{text_class}">{word_html}</div>
    </div>
    <div class="progress-bar-track">
        <div id="progress" class="progress-bar-fill" style="transition-duration: {progress_duration_ms}ms"></div>
    </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{threejs_setup}
{js_scene}

// Progress bar animation
setTimeout(() => {{
    document.getElementById('progress').style.width = '100%';
}}, {initial_delay_ms});

// Word reveal info (for debugging)
window.__qeema_scene_info = {json.dumps({
        'scene_type': scene_type,
        'palette': palette_name,
        'duration_sec': duration_sec,
        'word_count': word_count,
        'is_ayah': is_ayah,
    })};
</script>
</body>
</html>
"""
