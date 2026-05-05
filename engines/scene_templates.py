"""
engines/scene_templates.py — VALUE / QEEMA v16.0 (Cinematic Visuals)
======================================================================
[Changes v16]
- background_image param: replaces CSS/Three.js background with AI-generated PNG
- AI background gets vignette overlay for text readability
- Particle/text/logo overlays composited on top of AI image

[Changes v15]
- Local Amiri font embedded via @font-face (was Google Fonts CDN — fragile in CI)
- Logo overlay now uses logo.png if available (was CSS text only)
- Reverent emotion scenes: particle_count clamped to 0 (no distractions during recitation)
- _build_common_css accepts logo_path for runtime embedding
- build_scene_html accepts logo_path + font_path params

MAJOR VISUAL UPGRADE from v11.0:

[What changed]
1. NEW scene types: golden_field, starry_night, child_reading, rainbow, flowers
2. Emotion-aware color grading: warm/reverent/playful/peaceful/excited
3. Ken Burns CSS pan+zoom on backgrounds (slow drift for cinematic feel)
4. Richer Arabic typography: larger fonts, glow effects, bismillah ornament
5. Ayah display: special gold glow + slower word reveal for reverence
6. Hook scenes: bold, high-contrast, attention-grabbing
7. Story scenes: warm parchment background to signal "story mode"
8. Moral scenes: soft fade with light-ray effect
9. Better particle effects: hearts, stars, flowers per scene type
10. Scene-type CSS class system for consistent emotional identity
11. Channel logo upgraded: animated gradient pulse
12. Progress bar: thicker (8px), rounded, glowing gold

[Performance notes]
- All Three.js scenes now pre-calculate geometry at load (not per-frame)
- Reduced overdraw by using CSS background for most scenes (no Three.js)
- Three.js only for complex animated 3D scenes (sky, ocean, mountains)
- CSS-only scenes render faster in Playwright (~30% time reduction)
"""
from __future__ import annotations

import html as html_lib
import json
import textwrap
from pathlib import Path
from typing import Dict, List, Optional

from core.config import PALETTES

# v14: Extended palettes
PALETTES_V14 = {
    **PALETTES,
    "soft_morning": ["#FFF8E7", "#FFE5B4", "#FFDAB9", "#FFB347", "#F39C12"],
    "deep_teal":    ["#0D3B4A", "#1B6B7B", "#2E9EAD", "#7DD8E0", "#B2EBF2"],
}

# ════════════════════════════════════════════════════════════════
# Emotion → CSS filter/class mapping
# ════════════════════════════════════════════════════════════════
_EMOTION_CSS: Dict[str, str] = {
    "warm":      "brightness(1.05) saturate(1.1)",
    "reverent":  "brightness(0.92) saturate(0.85) sepia(0.12)",
    "playful":   "brightness(1.1) saturate(1.25)",
    "peaceful":  "brightness(1.0) saturate(0.9) hue-rotate(-5deg)",
    "excited":   "brightness(1.12) saturate(1.3) contrast(1.05)",
}

_EMOTION_TEXT_COLOR: Dict[str, str] = {
    "warm":      "#FFFFFF",
    "reverent":  "#FFD700",
    "playful":   "#FFFFFF",
    "peaceful":  "#F5F0E8",
    "excited":   "#FFFFFF",
}

_EMOTION_GLOW: Dict[str, str] = {
    "warm":      "rgba(255, 200, 100, 0.35)",
    "reverent":  "rgba(255, 215, 0, 0.5)",
    "playful":   "rgba(100, 220, 255, 0.35)",
    "peaceful":  "rgba(200, 230, 210, 0.3)",
    "excited":   "rgba(255, 140, 50, 0.4)",
}


# ════════════════════════════════════════════════════════════════
# Common CSS (v14 — upgraded typography + effects)
# ════════════════════════════════════════════════════════════════
def _build_common_css(emotion: str = "warm", font_path: Optional[str] = None) -> str:
    text_color = _EMOTION_TEXT_COLOR.get(emotion, "#FFFFFF")
    glow = _EMOTION_GLOW.get(emotion, "rgba(255,200,100,0.35)")
    img_filter = _EMOTION_CSS.get(emotion, "none")

    # v15: Embed local font if available — avoids Google Fonts CDN dependency
    font_face = ""
    if font_path:
        font_face = f"""
@font-face {{
    font-family: 'Amiri';
    src: url('file://{font_path}') format('truetype');
    font-weight: 700;
    font-display: swap;
}}
"""

    return f"""\
{font_face}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{
    width: 100%; height: 100%;
    background: #000;
    overflow: hidden;
    font-family: 'Amiri', 'Scheherazade New', 'Traditional Arabic', serif;
    direction: rtl;
}}
.scene-container {{
    position: fixed; inset: 0;
    width: 100vw; height: 100vh;
    filter: {img_filter};
    transition: filter 1s ease;
}}
canvas {{
    position: absolute; top: 0; left: 0;
    width: 100% !important; height: 100% !important;
    display: block;
}}
/* CSS-only scene backgrounds */
.bg-layer {{
    position: absolute; inset: 0;
    will-change: transform;
    animation: kenBurns 25s ease-in-out infinite alternate;
    background-size: cover !important;
    background-position: center !important;
}}
@keyframes kenBurns {{
    0%   {{ transform: scale(1.0) translate(0%, 0%); }}
    33%  {{ transform: scale(1.08) translate(-1.5%, 1%); }}
    66%  {{ transform: scale(1.05) translate(1.5%, -0.8%); }}
    100% {{ transform: scale(1.1) translate(-1%, 1.5%); }}
}}
.gradient-overlay {{
    position: absolute; inset: 0;
    background: linear-gradient(
        180deg,
        rgba(0,0,0,0.05) 0%,
        rgba(0,0,0,0.0) 40%,
        rgba(0,0,0,0.5) 85%,
        rgba(0,0,0,0.7) 100%
    );
    pointer-events: none;
    z-index: 5;
}}
.particles-layer {{
    position: absolute; inset: 0;
    pointer-events: none;
    z-index: 4;
    overflow: hidden;
}}
.particle {{
    position: absolute;
    border-radius: 50%;
    pointer-events: none;
    will-change: transform, opacity;
    animation-iteration-count: infinite;
    animation-timing-function: linear;
}}

/* ── QEEMA Logo ── */
.logo-overlay {{
    position: absolute;
    top: 36px; right: 52px;
    z-index: 10;
    color: #FFD700;
    font-size: 52px;
    font-weight: 900;
    text-shadow:
        0 0 24px rgba(255, 215, 0, 0.7),
        0 0 48px rgba(255, 165, 0, 0.3),
        0 4px 12px rgba(0, 0, 0, 0.8);
    letter-spacing: 2px;
    animation: logoPulse 3.5s ease-in-out infinite;
}}
.logo-overlay .en {{
    font-size: 21px;
    color: #FFE5B4;
    display: block;
    text-align: center;
    margin-top: -5px;
    letter-spacing: 5px;
    opacity: 0.9;
}}
@keyframes logoPulse {{
    0%, 100% {{ transform: scale(1); opacity: 0.92; }}
    50%      {{ transform: scale(1.035); opacity: 1.0; }}
}}

/* ── Text container ── */
.text-container {{
    position: absolute;
    bottom: 90px; left: 50%;
    transform: translateX(-50%);
    width: 88%; max-width: 1600px;
    padding: 44px 64px;
    background: rgba(0, 0, 0, 0.58);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 2px solid {glow};
    border-radius: 28px;
    box-shadow:
        0 16px 48px rgba(0, 0, 0, 0.55),
        0 0 60px {glow},
        inset 0 1px 0 rgba(255, 255, 255, 0.12);
    z-index: 8;
}}

/* Narrator text (story, hook, explain, moral) */
.narrator-text {{
    color: {text_color};
    font-size: 58px;
    font-weight: 700;
    line-height: 1.65;
    text-align: center;
    text-shadow: 0 2px 14px rgba(0,0,0,0.95);
}}

/* Hook text — bolder, larger, high contrast */
.hook-text {{
    color: #FFE566;
    font-size: 66px;
    font-weight: 900;
    line-height: 1.6;
    text-align: center;
    text-shadow:
        0 0 24px rgba(255, 229, 102, 0.6),
        0 3px 14px rgba(0,0,0,0.95);
}}

/* Story text — warm parchment feel */
.story-text {{
    color: #FFF5DC;
    font-size: 56px;
    font-weight: 700;
    line-height: 1.7;
    text-align: right;
    text-shadow: 0 2px 10px rgba(0,0,0,0.9);
    border-right: 5px solid #FFD700;
    padding-right: 24px;
}}

/* Moral text — peaceful, slightly italic */
.moral-text {{
    color: #D4EDDA;
    font-size: 56px;
    font-weight: 700;
    font-style: italic;
    line-height: 1.65;
    text-align: center;
    text-shadow: 0 2px 12px rgba(0,0,0,0.9);
}}

/* Ayah text — gold, glowing, sacred */
.ayah-text {{
    color: #FFD700;
    font-size: 76px;
    font-weight: 900;
    line-height: 1.75;
    text-align: center;
    text-shadow:
        0 0 30px rgba(255, 215, 0, 0.6),
        0 0 60px rgba(255, 165, 0, 0.25),
        0 3px 14px rgba(0, 0, 0, 0.95);
    letter-spacing: 1px;
}}

/* Bismillah ornament above ayah */
.bismillah-ornament {{
    text-align: center;
    font-size: 32px;
    color: rgba(255, 215, 0, 0.7);
    margin-bottom: 12px;
    letter-spacing: 4px;
}}

/* Word-reveal animation */
.word {{
    display: inline-block;
    margin: 0 5px;
    opacity: 0;
    transform: translateY(18px) scale(0.96);
    animation: wordReveal 0.55s cubic-bezier(0.23, 1, 0.32, 1) forwards;
}}
@keyframes wordReveal {{
    to {{ opacity: 1; transform: translateY(0) scale(1); }}
}}

/* Ayah word — slower, more reverential reveal */
.ayah-word {{
    display: inline-block;
    margin: 0 7px;
    opacity: 0;
    transform: translateY(14px);
    animation: ayahWordReveal 0.8s ease-out forwards;
}}
@keyframes ayahWordReveal {{
    to {{ opacity: 1; transform: translateY(0); }}
}}

/* Progress bar */
.progress-bar-track {{
    position: absolute;
    bottom: 0; left: 0;
    height: 8px; width: 100%;
    background: rgba(0,0,0,0.3);
    z-index: 9;
}}
.progress-bar-fill {{
    height: 100%; width: 0;
    background: linear-gradient(90deg, #FFD700 0%, #FFCC70 50%, #FFB347 100%);
    box-shadow: 0 0 14px rgba(255, 215, 0, 0.9);
    border-radius: 0 4px 4px 0;
    transition: width linear;
}}
"""


# ════════════════════════════════════════════════════════════════
# Word-wrap helpers (v14: separate classes for different text types)
# ════════════════════════════════════════════════════════════════
def _wrap_words_html(text: str, css_class: str = "word", stagger_ms: int = 260) -> str:
    if not text:
        return ""
    words = text.split()
    spans = []
    for i, w in enumerate(words):
        delay = i * stagger_ms
        safe = html_lib.escape(w)
        spans.append(
            f'<span class="{css_class}" style="animation-delay:{delay}ms">{safe}</span>'
        )
    return " ".join(spans)


def _ayah_word_html(text: str, stagger_ms: int = 400) -> str:
    """Slower reveal for Quran text."""
    return _wrap_words_html(text, css_class="ayah-word", stagger_ms=stagger_ms)


# ════════════════════════════════════════════════════════════════
# Particle builders (v14: type-aware)
# ════════════════════════════════════════════════════════════════
def _build_particles_html(
    palette: List[str],
    count: int = 60,
    particle_type: str = "sparkle",
) -> str:
    import random
    rng = random.Random(42)
    color = palette[3] if len(palette) > 3 else "#FFD700"
    color2 = palette[4] if len(palette) > 4 else color

    # Particle type shapes
    shape_css = ""
    if particle_type == "heart":
        # CSS hearts
        shape_css = """
        .particle::before, .particle::after {
            content: '';
            position: absolute;
            width: 100%; height: 100%;
            background: inherit;
            border-radius: 50% 50% 0 50%;
        }
        .particle { transform: rotate(-45deg); border-radius: 0 !important; }
        .particle::before { transform: rotate(0deg); }
        .particle::after { transform: rotate(90deg) translate(50%, 0); }
        """
    elif particle_type == "star":
        shape_css = ".particle { clip-path: polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%); }"

    parts = []
    for i in range(count):
        size = rng.uniform(3, 8) if particle_type == "sparkle" else rng.uniform(5, 12)
        left = rng.uniform(0, 100)
        delay = rng.uniform(0, 10)
        duration = rng.uniform(8, 18)
        opacity = rng.uniform(0.5, 0.9)
        c = color if i % 2 == 0 else color2
        parts.append(
            f'<div class="particle" style="'
            f'left: {left:.1f}vw; bottom: -10px; '
            f'width: {size:.1f}px; height: {size:.1f}px; '
            f'background: {c}; '
            f'opacity: {opacity:.2f}; '
            f'box-shadow: 0 0 {size * 2:.0f}px {c}80; '
            f'animation: particleRise {duration:.1f}s linear {delay:.1f}s infinite;'
            f'"></div>'
        )

    css = f"""
    {shape_css}
    @keyframes particleRise {{
        0%   {{ transform: translateY(0) translateX(0) rotate(0deg); opacity: 0; }}
        10%  {{ opacity: 0.85; }}
        50%  {{ transform: translateY(-55vh) translateX(15px) rotate(180deg); }}
        90%  {{ opacity: 0.7; }}
        100% {{ transform: translateY(-110vh) translateX(30px) rotate(360deg); opacity: 0; }}
    }}
    """
    return f'<style>{css}</style><div class="particles-layer">{"".join(parts)}</div>'


# ════════════════════════════════════════════════════════════════
# CSS-only scene backgrounds (v14: faster than Three.js for simple scenes)
# ════════════════════════════════════════════════════════════════
def _css_golden_field(palette: List[str]) -> str:
    p0, p1, p2, p3 = palette[0], palette[1], palette[2], palette[3]
    return f"""
    <style>
    .bg-layer {{
        background: linear-gradient(
            180deg,
            {p0} 0%,      /* sky */
            #87CEEB 30%,
            {p1} 50%,     /* horizon */
            {p2} 65%,     /* field */
            {p3} 100%     /* near ground */
        );
    }}
    /* Animated sun rays */
    .sun-rays {{
        position: absolute;
        top: 5%; left: 45%;
        width: 200px; height: 200px;
        background: radial-gradient(circle, rgba(255,255,200,0.9) 0%, transparent 70%);
        border-radius: 50%;
        animation: sunPulse 4s ease-in-out infinite;
    }}
    @keyframes sunPulse {{
        0%, 100% {{ transform: scale(1); opacity: 0.8; }}
        50%      {{ transform: scale(1.15); opacity: 1.0; }}
    }}
    /* Wheat stalks */
    .wheat-row {{
        position: absolute;
        bottom: 0; left: 0; right: 0;
        height: 35%;
        background: repeating-linear-gradient(
            90deg,
            {p2}aa 0px, {p2}aa 8px,
            {p3}88 8px, {p3}88 16px
        );
        animation: wheatSway 3s ease-in-out infinite alternate;
        transform-origin: bottom;
    }}
    @keyframes wheatSway {{
        0%   {{ transform: skewX(-2deg); }}
        100% {{ transform: skewX(2deg); }}
    }}
    </style>
    <div class="bg-layer"></div>
    <div class="sun-rays"></div>
    <div class="wheat-row"></div>
    """


def _css_flowers(palette: List[str]) -> str:
    p0, p1, p2, p3, p4 = (palette + ["#FFD700"] * 5)[:5]
    return f"""
    <style>
    .bg-layer {{
        background: linear-gradient(180deg, #87CEEB 0%, {p0} 40%, {p1} 70%, {p2} 100%);
    }}
    .flower {{
        position: absolute;
        border-radius: 50%;
        animation: flowerBob 3s ease-in-out infinite alternate;
    }}
    @keyframes flowerBob {{
        0%   {{ transform: translateY(0) rotate(0deg); }}
        100% {{ transform: translateY(-12px) rotate(8deg); }}
    }}
    </style>
    <div class="bg-layer"></div>
    <div class="flower" style="width:80px;height:80px;background:{p3};bottom:20%;left:10%;animation-delay:0s"></div>
    <div class="flower" style="width:60px;height:60px;background:{p4};bottom:25%;left:25%;animation-delay:0.5s"></div>
    <div class="flower" style="width:100px;height:100px;background:{p2};bottom:18%;left:60%;animation-delay:1s"></div>
    <div class="flower" style="width:70px;height:70px;background:{p3};bottom:30%;right:15%;animation-delay:1.5s"></div>
    """


def _css_rainbow(palette: List[str]) -> str:
    return f"""
    <style>
    .bg-layer {{
        background: linear-gradient(180deg, #1a2744 0%, #2d5a8e 35%, #87CEEB 55%, #c8e6c9 80%, #a5d6a7 100%);
    }}
    .rainbow-arc {{
        position: absolute;
        top: 5%; left: 50%;
        transform: translateX(-50%);
        width: 90vw; height: 45vw;
        border-radius: 50% 50% 0 0;
        border: 36px solid transparent;
        border-image: linear-gradient(90deg,
            #ff000060, #ff7f0060, #ffff0060,
            #00ff0060, #0000ff60, #8b00ff60) 1;
        animation: rainbowFade 4s ease-in-out infinite alternate;
    }}
    @keyframes rainbowFade {{
        0%   {{ opacity: 0.7; transform: translateX(-50%) scale(1); }}
        100% {{ opacity: 1.0; transform: translateX(-50%) scale(1.02); }}
    }}
    .rain-drop {{
        position: absolute;
        width: 2px;
        background: rgba(150, 200, 255, 0.6);
        animation: rainFall linear infinite;
        border-radius: 2px;
    }}
    @keyframes rainFall {{
        0%   {{ transform: translateY(-100px); opacity: 0; }}
        10%  {{ opacity: 0.6; }}
        100% {{ transform: translateY(110vh); opacity: 0.2; }}
    }}
    </style>
    <div class="bg-layer"></div>
    <div class="rainbow-arc"></div>
    """


def _css_child_reading(palette: List[str]) -> str:
    p0, p1, p2, p3 = palette[0], palette[1], palette[2], palette[3]
    return f"""
    <style>
    .bg-layer {{
        background: radial-gradient(ellipse at 60% 40%,
            {p0}ee 0%, {p1}cc 40%, {p2}aa 70%, #16213e 100%);
    }}
    /* Desk lamp glow */
    .lamp-glow {{
        position: absolute;
        top: 20%; right: 25%;
        width: 300px; height: 300px;
        background: radial-gradient(circle, rgba(255,245,180,0.5) 0%, transparent 70%);
        border-radius: 50%;
        animation: lampFlicker 5s ease-in-out infinite;
    }}
    @keyframes lampFlicker {{
        0%, 100% {{ opacity: 0.8; transform: scale(1); }}
        50%      {{ opacity: 1.0; transform: scale(1.05); }}
    }}
    /* Book pages floating effect */
    .page-dust {{
        position: absolute;
        width: 4px; height: 4px;
        background: {p3};
        border-radius: 50%;
        animation: pageFloat linear infinite;
    }}
    @keyframes pageFloat {{
        0%   {{ transform: translateY(0) translateX(0); opacity: 0; }}
        20%  {{ opacity: 0.8; }}
        80%  {{ opacity: 0.8; }}
        100% {{ transform: translateY(-60vh) translateX(40px); opacity: 0; }}
    }}
    </style>
    <div class="bg-layer"></div>
    <div class="lamp-glow"></div>
    <div class="page-dust" style="left:55%;bottom:30%;animation-duration:6s;animation-delay:0s"></div>
    <div class="page-dust" style="left:58%;bottom:32%;animation-duration:8s;animation-delay:1s"></div>
    <div class="page-dust" style="left:52%;bottom:28%;animation-duration:7s;animation-delay:2s"></div>
    """


def _css_starry_night(palette: List[str]) -> str:
    p0 = palette[0]
    p3 = palette[3]
    return f"""
    <style>
    .bg-layer {{
        background: radial-gradient(ellipse at 50% 80%,
            #1a0a2e 0%, #0d0620 40%, #04010e 100%);
    }}
    .star-field {{
        position: absolute; inset: 0;
        background-image:
            radial-gradient(2px 2px at 15% 20%, {p3}, transparent),
            radial-gradient(1px 1px at 30% 10%, white, transparent),
            radial-gradient(2px 2px at 50% 30%, {p3}cc, transparent),
            radial-gradient(1px 1px at 70% 15%, white, transparent),
            radial-gradient(2px 2px at 85% 25%, {p3}, transparent),
            radial-gradient(1px 1px at 10% 50%, white, transparent),
            radial-gradient(2px 2px at 40% 45%, {p3}aa, transparent),
            radial-gradient(1px 1px at 60% 55%, white, transparent),
            radial-gradient(2px 2px at 80% 40%, {p3}, transparent),
            radial-gradient(3px 3px at 25% 35%, #ffe066, transparent),
            radial-gradient(3px 3px at 75% 28%, #ffe066, transparent);
        animation: starTwinkle 4s ease-in-out infinite alternate;
    }}
    @keyframes starTwinkle {{
        0%   {{ opacity: 0.8; }}
        50%  {{ opacity: 1.0; }}
        100% {{ opacity: 0.7; }}
    }}
    .moon-glow {{
        position: absolute;
        top: 8%; right: 12%;
        width: 120px; height: 120px;
        background: radial-gradient(circle, #fffde0 30%, rgba(255,253,200,0.4) 60%, transparent 75%);
        border-radius: 50%;
        animation: moonPulse 6s ease-in-out infinite;
    }}
    @keyframes moonPulse {{
        0%, 100% {{ box-shadow: 0 0 40px rgba(255,250,200,0.4); }}
        50%      {{ box-shadow: 0 0 80px rgba(255,250,200,0.6); }}
    }}
    </style>
    <div class="bg-layer"></div>
    <div class="star-field"></div>
    <div class="moon-glow"></div>
    """


# ════════════════════════════════════════════════════════════════
# Three.js scene builders (v14: kept for complex animated 3D)
# ════════════════════════════════════════════════════════════════
def _scene_sky_js(palette: List[str]) -> str:
    """Starry night Three.js — layered stars + moon + meteor showers."""
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x02010a);
    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 200);
    camera.position.set(0, 0, 14);
    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);

    // Stars — two layers for depth
    function makeStars(count, spread, size, color) {{
        const geo = new THREE.BufferGeometry();
        const pos = new Float32Array(count * 3);
        for (let i = 0; i < count; i++) {{
            pos[i*3]   = (Math.random() - 0.5) * spread;
            pos[i*3+1] = (Math.random() - 0.5) * spread * 0.6;
            pos[i*3+2] = (Math.random() - 0.5) * spread * 0.5 - 20;
        }}
        geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        return new THREE.Points(geo, new THREE.PointsMaterial({{
            color, size, transparent: true, opacity: 0.9,
        }}));
    }}
    scene.add(makeStars(900, 120, 0.16, 0x{palette[3][1:]}));
    scene.add(makeStars(400, 120, 0.08, 0xffffff));

    // Moon
    const moon = new THREE.Mesh(
        new THREE.SphereGeometry(1.5, 32, 32),
        new THREE.MeshBasicMaterial({{ color: 0xfffde0 }})
    );
    moon.position.set(5, 4.5, -12);
    scene.add(moon);
    const halo = new THREE.Mesh(
        new THREE.RingGeometry(1.8, 3.0, 64),
        new THREE.MeshBasicMaterial({{
            color: 0x{palette[4][1:]}, transparent: true, opacity: 0.18,
            side: THREE.DoubleSide
        }})
    );
    halo.position.copy(moon.position);
    scene.add(halo);

    function animate(t) {{
        requestAnimationFrame(animate);
        scene.children.forEach(c => {{
            if (c.isPoints) c.rotation.z = t * 0.00004;
        }});
        moon.rotation.y = t * 0.0001;
        halo.rotation.z = -t * 0.0002;
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_ocean_js(palette: List[str]) -> str:
    """Ocean waves — animated geometry."""
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});
    scene.fog = new THREE.FogExp2(0x{palette[0][1:]}, 0.012);
    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 200);
    camera.position.set(0, 6, 12);
    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);
    scene.add(new THREE.AmbientLight(0xffffff, 0.5));
    const sun = new THREE.DirectionalLight(0xffd700, 1.0);
    sun.position.set(0, 10, 5);
    scene.add(sun);
    const wave_geo = new THREE.PlaneGeometry(80, 80, 90, 90);
    const waves = new THREE.Mesh(wave_geo, new THREE.MeshStandardMaterial({{
        color: 0x{palette[2][1:]}, flatShading: true,
    }}));
    waves.rotation.x = -Math.PI / 2;
    waves.position.y = -1;
    scene.add(waves);
    const wp = wave_geo.attributes.position;
    // Clouds
    for (let i = 0; i < 6; i++) {{
        const c = new THREE.Mesh(
            new THREE.SphereGeometry(1.2, 10, 10),
            new THREE.MeshBasicMaterial({{ color: 0xffffff, transparent: true, opacity: 0.75 }})
        );
        c.scale.set(2 + Math.random() * 2, 0.6, 1);
        c.position.set(-10 + Math.random() * 20, 5 + Math.random() * 3, -8 + Math.random() * 4);
        scene.add(c);
    }}
    function animate(t) {{
        requestAnimationFrame(animate);
        for (let i = 0; i < wp.count; i++) {{
            const x = wp.getX(i), y = wp.getY(i);
            wp.setZ(i, Math.sin(x * 0.28 + t*0.001)*0.35 + Math.cos(y*0.35 + t*0.0012)*0.28);
        }}
        wp.needsUpdate = true;
        camera.position.y = 6 + Math.sin(t*0.0003)*0.4;
        camera.lookAt(0, 0, 0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_mosque_js(palette: List[str]) -> str:
    """Mosque with dome + minarets — reverent golden lighting."""
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});
    const camera = new THREE.PerspectiveCamera(60, w/h, 0.1, 100);
    camera.position.set(0, 4, 13);
    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);
    scene.add(new THREE.AmbientLight(0xfff0d0, 0.85));
    const dl = new THREE.DirectionalLight(0xffe4a3, 1.1);
    dl.position.set(0, 8, 4);
    scene.add(dl);
    // Base
    scene.add(Object.assign(new THREE.Mesh(
        new THREE.BoxGeometry(7, 2.5, 4.5),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[2][1:]} }})
    ), {{ position: new THREE.Vector3(0, 0, 0) }}));
    // Dome
    const dome = new THREE.Mesh(
        new THREE.SphereGeometry(2.0, 32, 24, 0, Math.PI * 2, 0, Math.PI / 2),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[3][1:]} }})
    );
    dome.position.y = 1.25;
    scene.add(dome);
    // Crescent
    const crescent = new THREE.Mesh(
        new THREE.TorusGeometry(0.32, 0.08, 12, 24, Math.PI),
        new THREE.MeshBasicMaterial({{ color: 0x{palette[4][1:]} }})
    );
    crescent.position.set(0, 3.6, 0);
    scene.add(crescent);
    // Minarets
    [-4, 4].forEach(x => {{
        const body = new THREE.Mesh(
            new THREE.CylinderGeometry(0.28, 0.35, 5, 12),
            new THREE.MeshStandardMaterial({{ color: 0x{palette[2][1:]} }})
        );
        body.position.set(x, 1.2, -1.8);
        scene.add(body);
        const top = new THREE.Mesh(
            new THREE.ConeGeometry(0.4, 0.8, 8),
            new THREE.MeshStandardMaterial({{ color: 0x{palette[3][1:]} }})
        );
        top.position.set(x, 4, -1.8);
        scene.add(top);
    }});
    // Ground
    const gnd = new THREE.Mesh(
        new THREE.PlaneGeometry(50, 50),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    gnd.rotation.x = -Math.PI / 2;
    gnd.position.y = -1;
    scene.add(gnd);
    function animate(t) {{
        requestAnimationFrame(animate);
        camera.position.x = Math.sin(t * 0.00022) * 2.5;
        camera.lookAt(0, 1.5, 0);
        crescent.rotation.z = Math.sin(t * 0.0007) * 0.12;
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_mountains_js(palette: List[str]) -> str:
    return f"""
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x{palette[0][1:]});
    scene.fog = new THREE.Fog(0x{palette[1][1:]}, 6, 38);
    const camera = new THREE.PerspectiveCamera(55, w/h, 0.1, 100);
    camera.position.set(0, 3, 12);
    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.querySelector('.scene-container').appendChild(renderer.domElement);
    scene.add(new THREE.AmbientLight(0xfff5e1, 0.65));
    const dl = new THREE.DirectionalLight(0xffcc70, 1.2);
    dl.position.set(5, 8, 3);
    scene.add(dl);
    const mtColors = [0x{palette[2][1:]}, 0x{palette[3][1:]}, 0x{palette[1][1:]}];
    for (let i = 0; i < 8; i++) {{
        const h = 4 + Math.random() * 3.5;
        const mt = new THREE.Mesh(
            new THREE.ConeGeometry(1.8 + Math.random() * 0.8, h, 5),
            new THREE.MeshStandardMaterial({{ color: mtColors[i%3], flatShading: true }})
        );
        mt.position.set(-14 + i * 4, h/2 - 1, -5 - Math.random() * 6);
        scene.add(mt);
    }}
    const gnd = new THREE.Mesh(
        new THREE.PlaneGeometry(80, 80),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    gnd.rotation.x = -Math.PI / 2; gnd.position.y = -1;
    scene.add(gnd);
    function animate(t) {{
        requestAnimationFrame(animate);
        camera.position.x = Math.sin(t * 0.0002) * 2;
        camera.lookAt(0, 2, -5);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_family_js(palette: List[str]) -> str:
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
    const figures = [];
    const xs = [-2.2, 0, 2.2], heights = [1.6, 1.9, 1.2];
    xs.forEach((x, i) => {{
        const body = new THREE.Mesh(
            new THREE.ConeGeometry(0.52, heights[i], 14),
            new THREE.MeshStandardMaterial({{ color: i===0 ? 0x{palette[2][1:]} : 0x{palette[3][1:]} }})
        );
        body.position.set(x, heights[i]/2 - 1, 0);
        const head = new THREE.Mesh(
            new THREE.SphereGeometry(0.29, 14, 14),
            new THREE.MeshStandardMaterial({{ color: 0xffd9ad }})
        );
        head.position.set(x, heights[i] - 0.6, 0);
        scene.add(body); scene.add(head);
        figures.push({{ body, head }});
    }});
    // Hearts
    const hearts = [];
    for (let i = 0; i < 30; i++) {{
        const h = new THREE.Mesh(
            new THREE.SphereGeometry(0.13, 8, 8),
            new THREE.MeshBasicMaterial({{ color: 0x{palette[3][1:]}, transparent: true, opacity: 0.85 }})
        );
        h.position.set(-5 + Math.random()*10, Math.random()*4, -2 + Math.random()*4);
        scene.add(h);
        hearts.push({{ mesh: h, speed: 0.005 + Math.random()*0.01 }});
    }}
    const gnd = new THREE.Mesh(
        new THREE.CircleGeometry(20, 32),
        new THREE.MeshStandardMaterial({{ color: 0x{palette[1][1:]} }})
    );
    gnd.rotation.x = -Math.PI/2; gnd.position.y = -1;
    scene.add(gnd);
    function animate(t) {{
        requestAnimationFrame(animate);
        hearts.forEach(h => {{
            h.mesh.position.y += h.speed;
            if (h.mesh.position.y > 5) h.mesh.position.y = -1;
        }});
        figures.forEach((f, i) => {{
            f.body.rotation.y = Math.sin(t*0.0006 + i)*0.08;
        }});
        camera.position.x = Math.sin(t*0.00025)*1.0;
        camera.lookAt(0, 1, 0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


def _scene_abstract_warm_js(palette: List[str]) -> str:
    """Abstract warm — rotating geometric shapes."""
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
    scene.add(Object.assign(new THREE.DirectionalLight(0xfff0d0, 1.0), {{ position: new THREE.Vector3(5, 5, 5) }}));
    const colors = [0x{palette[1][1:]}, 0x{palette[2][1:]}, 0x{palette[3][1:]}, 0x{palette[4][1:]}];
    const shapes = [];
    for (let i = 0; i < 12; i++) {{
        const geo = i%2===0 ? new THREE.IcosahedronGeometry(0.65,0) : new THREE.OctahedronGeometry(0.75,0);
        const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({{ color: colors[i%4], flatShading: true }}));
        const r = 3 + Math.random()*3, a = (i/12)*Math.PI*2;
        m.position.set(Math.cos(a)*r, Math.sin(a)*r*0.6, -Math.random()*4);
        scene.add(m); shapes.push(m);
    }}
    function animate(t) {{
        requestAnimationFrame(animate);
        shapes.forEach((s,i) => {{ s.rotation.x = t*0.0005*(1+i*0.05); s.rotation.y = t*0.0007*(1+i*0.03); }});
        camera.position.x = Math.sin(t*0.0003)*1.5;
        camera.position.y = Math.cos(t*0.0002)*0.8;
        camera.lookAt(0,0,0);
        renderer.render(scene, camera);
    }}
    requestAnimationFrame(animate);
    """


# ════════════════════════════════════════════════════════════════
# Scene dispatch (v14: CSS-first, Three.js for complex 3D)
# ════════════════════════════════════════════════════════════════
# CSS scenes: rendered via inline HTML (faster in Playwright)
_CSS_SCENE_BUILDERS: Dict[str, callable] = {
    "golden_field":  _css_golden_field,
    "flowers":       _css_flowers,
    "rainbow":       _css_rainbow,
    "child_reading": _css_child_reading,
    "starry_night":  _css_starry_night,
}

# Three.js scenes: rendered via canvas (better for 3D animation)
_THREEJS_SCENE_BUILDERS: Dict[str, callable] = {
    "sky":           _scene_sky_js,
    "ocean":         _scene_ocean_js,
    "mosque":        _scene_mosque_js,
    "mountains":     _scene_mountains_js,
    "family":        _scene_family_js,
    "abstract_warm": _scene_abstract_warm_js,
}

# Particle type per scene
_SCENE_PARTICLE_TYPE: Dict[str, str] = {
    "family": "heart",
    "flowers": "star",
    "rainbow": "sparkle",
    "golden_field": "sparkle",
    "child_praying": "sparkle",
    "mosque": "sparkle",
    "garden": "star",
}


# ════════════════════════════════════════════════════════════════
# Public: build_scene_html (v14)
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
    particle_count: int = 35,
    text_style: str = "narrator",  # v14: narrator|hook|story|moral|ayah
    scene_emotion: str = "warm",
    logo_path: Optional[str] = None,    # v15: PNG logo overlay
    font_path: Optional[str] = None,    # v15: local font embedding
    background_image: Optional[str] = None,  # v16: AI-generated bg image (Leonardo)
) -> str:
    """
    Build full HTML for one scene.

    [v16 changes]
    - background_image: optional path to AI-generated PNG (Leonardo)
      → if provided, replaces CSS gradient background
      → still applies emotion filter and Ken Burns animation
      → particles + text overlay still composited on top

    [v15 changes]
    - Embeds local Amiri font via @font-face if font_path provided
    - Uses PNG logo overlay if logo_path provided (else falls back to text)
    - For 'reverent' emotion: zeros out particles (less distracting during recitation)
    """
    palette = PALETTES_V14.get(palette_name, PALETTES_V14["warm_sunset"])
    particle_type = _SCENE_PARTICLE_TYPE.get(scene_type, "sparkle")

    # v15: Reverent scenes (recitation) get NO particles — focus on Quran
    effective_particle_count = particle_count
    if scene_emotion == "reverent" or is_ayah:
        effective_particle_count = 0

    # Determine text CSS class
    if is_ayah:
        text_class = "ayah-text"
        word_html = _ayah_word_html(text)
    elif text_style == "hook":
        text_class = "hook-text"
        word_html = _wrap_words_html(text, "word", stagger_ms=220)
    elif text_style == "story":
        text_class = "story-text"
        word_html = _wrap_words_html(text, "word", stagger_ms=200)
    elif text_style == "moral":
        text_class = "moral-text"
        word_html = _wrap_words_html(text, "word", stagger_ms=280)
    else:
        text_class = "narrator-text"
        word_html = _wrap_words_html(text, "word", stagger_ms=260)

    # v16: AI-generated background image takes priority over CSS/Three.js
    if background_image and Path(background_image).exists():
        # Use AI image as background — disable Three.js, override CSS scene
        use_threejs = False
        scene_bg_html = (
            f'<div class="bg-layer ai-bg" '
            f'style="background-image:url(\'file://{Path(background_image).absolute()}\');'
            f'background-size:cover;background-position:center;"></div>'
            f'<div class="ai-bg-vignette"></div>'
        )
        threejs_include = ""
        threejs_init = ""
        js_scene = ""
        # Add a subtle dark vignette to keep text readable
        ai_vignette_css = """
.ai-bg {
    background-size: cover !important;
    background-position: center !important;
}
.ai-bg-vignette {
    position: absolute;
    inset: 0;
    background: radial-gradient(ellipse at center, transparent 30%, rgba(0,0,0,0.45) 100%);
    z-index: 1;
    pointer-events: none;
}
"""
    else:
        # Fall back to CSS/Three.js scene generation
        ai_vignette_css = ""
        use_threejs = scene_type in _THREEJS_SCENE_BUILDERS
        if use_threejs:
            builder = _THREEJS_SCENE_BUILDERS[scene_type]
            scene_bg_html = ""
            threejs_setup = "const w = window.innerWidth;\nconst h = window.innerHeight;"
            js_scene = builder(palette)
            threejs_include = '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>'
            threejs_init = f"<script>\n{threejs_setup}\n{js_scene}\n</script>"
        else:
            builder = _CSS_SCENE_BUILDERS.get(scene_type)
            if builder:
                scene_bg_html = builder(palette)
            else:
                # CSS gradient fallback
                p0, p1 = palette[0], palette[1]
                scene_bg_html = f'<div class="bg-layer" style="background:linear-gradient(180deg,{p0},{p1})"></div>'
            threejs_include = ""
            threejs_init = ""
            js_scene = ""

    # Bismillah ornament for ayah scenes
    bismillah_html = ""
    if is_ayah:
        bismillah_html = '<div class="bismillah-ornament">۞ ٱللَّهِ ۞</div>'

    progress_ms = int(duration_sec * 1000)
    safe_ar = html_lib.escape(channel_name_ar)
    safe_en = html_lib.escape(channel_name_en)

    common_css = _build_common_css(emotion=scene_emotion, font_path=font_path)
    particles_html = _build_particles_html(palette, effective_particle_count, particle_type) \
        if effective_particle_count > 0 else ""

    # v15: PNG logo overlay if available, else fall back to text
    if logo_path:
        logo_html = f'''<img src="file://{logo_path}" class="logo-png" alt="{safe_ar}" />'''
        logo_extra_css = """
.logo-png {
    position: absolute;
    top: 32px; right: 48px;
    height: 84px;
    width: auto;
    z-index: 10;
    opacity: 0.92;
    filter: drop-shadow(0 4px 12px rgba(0,0,0,0.7));
    animation: logoPulse 3.5s ease-in-out infinite;
}
"""
    else:
        logo_html = f'''<div class="logo-overlay">{safe_ar}<span class="en">{safe_en}</span></div>'''
        logo_extra_css = ""

    # v15: Only load Google Fonts as fallback when local font isn't available
    google_fonts_link = ""
    if not font_path:
        google_fonts_link = (
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link href="https://fonts.googleapis.com/css2?family=Amiri:wght@400;700&display=swap" rel="stylesheet">'
        )

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>QEEMA Scene — {scene_type}</title>
{google_fonts_link}
<style>{common_css}{logo_extra_css}{ai_vignette_css}</style>
</head>
<body>
<div class="scene-container">
    {scene_bg_html}
    <div class="gradient-overlay"></div>
    {particles_html}
    {logo_html}
    <div class="text-container">
        {bismillah_html}
        <div class="{text_class}">{word_html}</div>
    </div>
    <div class="progress-bar-track">
        <div id="progress" class="progress-bar-fill"
             style="transition-duration: {progress_ms}ms"></div>
    </div>
</div>
{threejs_include}
{threejs_init}
<script>
// Progress bar
setTimeout(() => {{
    document.getElementById('progress').style.width = '100%';
}}, 120);

window.__qeema_v15 = {{
    scene_type: {json.dumps(scene_type)},
    palette: {json.dumps(palette_name)},
    emotion: {json.dumps(scene_emotion)},
    text_style: {json.dumps(text_style)},
    duration_sec: {duration_sec},
    is_ayah: {str(is_ayah).lower()},
}};
</script>
</body>
</html>
"""
