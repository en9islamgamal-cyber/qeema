"""
thumbnail_engine.py — VALUE / QEEMA v2
Thumbnail احترافي 1280×720 للـ YouTube
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional
from config import Paths
from models import EpisodeScript

logger = logging.getLogger(__name__)

def _get_font(size: int, bold: bool = False):
    try:
        from PIL import ImageFont
        for d in Paths.FONTS.glob("*.ttf"):
            try: return ImageFont.truetype(str(d), size)
            except: pass
        for p in [
            "/usr/share/fonts/truetype/arabic/Amiri-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        ]:
            if Path(p).exists():
                try: return ImageFont.truetype(p, size)
                except: pass
        return ImageFont.load_default()
    except ImportError:
        return None

class ThumbnailEngine:
    SIZE = (1280, 720)
    GRADIENT = [(20, 20, 60), (10, 55, 40)]

    def create(
        self,
        script: EpisodeScript,
        episode_num: int,
        scene_image: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageEnhance
            import arabic_reshaper
            from bidi.algorithm import get_display
        except ImportError:
            logger.warning("PIL غير مثبت — تجاوز Thumbnail")
            return ""

        out = output_path or str(Paths.THUMBNAILS / f"ep_{episode_num:03d}.jpg")
        Path(out).parent.mkdir(parents=True, exist_ok=True)

        # ── الخلفية ──────────────────────────
        if scene_image and Path(scene_image).exists():
            bg = Image.open(scene_image).convert("RGB").resize(self.SIZE, Image.LANCZOS)
            bg = ImageEnhance.Brightness(bg).enhance(0.45)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=2))
        else:
            bg = self._gradient(self.SIZE)

        draw = ImageDraw.Draw(bg, "RGBA")
        W, H = self.SIZE

        # ── زخارف هندسية ─────────────────────
        for cx, cy, r, a in [(W*0.85,H*0.15,200,18),(W*0.12,H*0.85,130,12)]:
            for rr in [r, r*0.68, r*0.42]:
                draw.ellipse([cx-rr,cy-rr,cx+rr,cy+rr], outline=(255,215,0,a), width=2)

        # ── شارة السورة (ذهبية) ──────────────
        bx,by,bw,bh = W-410,25,380,85
        draw.rounded_rectangle([bx,by,bx+bw,by+bh], radius=18, fill=(255,215,0,225))
        draw.rounded_rectangle([bx,by,bx+bw,by+bh], radius=18, outline=(255,240,120,240), width=3)

        f_large = _get_font(44, bold=True)
        if f_large:
            try:
                t = arabic_reshaper.reshape(f"سورة {script.surah_name}")
                t = get_display(t)
            except: t = f"سورة {script.surah_name}"
            tb = draw.textbbox((0,0), t, font=f_large)
            tx = bx + (bw-(tb[2]-tb[0]))//2
            draw.text((tx, by+14), t, font=f_large, fill=(15,15,30,255))

        # رقم الحلقة
        f_sm = _get_font(28)
        if f_sm:
            try:
                ep_t = arabic_reshaper.reshape(f"الحلقة {episode_num}")
                ep_t = get_display(ep_t)
            except: ep_t = f"الحلقة {episode_num}"
            draw.text((bx+12, by+bh+8), ep_t, font=f_sm, fill=(255,235,100,240))

        # ── العنوان (يسار) ───────────────────
        f_title = _get_font(40, bold=True)
        if f_title:
            tx_x, tx_y = 30, H//2 - 60
            try:
                title = arabic_reshaper.reshape(script.title)
                title = get_display(title)
            except: title = script.title

            words = title.split()
            lines, cur = [], []
            for w in words:
                cur.append(w)
                tb = draw.textbbox((0,0)," ".join(cur),font=f_title)
                if tb[2]-tb[0] > 560:
                    if len(cur)>1: lines.append(" ".join(cur[:-1])); cur=[w]
                    else: lines.append(" ".join(cur)); cur=[]
            if cur: lines.append(" ".join(cur))

            total_h = len(lines)*52+20
            draw.rounded_rectangle(
                [tx_x-12, tx_y-12, tx_x+590, tx_y+total_h],
                radius=14, fill=(0,0,0,145),
            )
            for i, line in enumerate(lines[:4]):
                draw.text((tx_x, tx_y+i*52), line, font=f_title, fill=(255,255,255,255))

        # ── نجوم زخرفية ──────────────────────
        for sx,sy,sr in [(660,70,22),(705,105,14),(680,50,10)]:
            draw.ellipse([sx-sr,sy-sr,sx+sr,sy+sr], fill=(255,215,0,200))

        # ── شعار القناة ──────────────────────
        logo_p = Paths.OVERLAYS / "channel_logo.png"
        if logo_p.exists():
            try:
                logo = Image.open(str(logo_p)).convert("RGBA").resize((100,100), Image.LANCZOS)
                bg.paste(logo, (W//2-50, 15), logo)
            except: pass
        elif f_sm:
            try:
                ch = arabic_reshaper.reshape("قيمة | VALUE")
                ch = get_display(ch)
            except: ch = "VALUE | قيمة"
            draw.text((24, 20), ch, font=f_sm, fill=(255,215,0,230))

        bg.save(out, "JPEG", quality=96, optimize=True)
        logger.info(f"✅ Thumbnail: {out}")
        return out

    def _gradient(self, size):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", size)
        d   = ImageDraw.Draw(img)
        c1, c2 = self.GRADIENT
        W, H = size
        for y in range(H):
            r_v = y/H
            r = int(c1[0]+(c2[0]-c1[0])*r_v)
            g = int(c1[1]+(c2[1]-c1[1])*r_v)
            b = int(c1[2]+(c2[2]-c1[2])*r_v)
            d.line([(0,y),(W,y)], fill=(r,g,b))
        return img
