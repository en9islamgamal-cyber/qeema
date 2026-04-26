"""
sfx_engine.py — VALUE / QEEMA v9.0 (Cinematic Audio Mastering)
==============================================================
المسؤول عن جعل الانتقالات الصوتية انسيابية كالأفلام العالمية.
"""
import logging, subprocess as sp
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

class SFXEngine:
    def process_all(self, audio_map: Dict[str, str], script: any, ep_dir: str) -> Dict[str, str]:
        logger.info("🎛️ جاري المكساج السينمائي (Normalizing & Seamless Transitions)...")
        processed = {}
        out_dir = Path(ep_dir) / "sfx_mastered"
        out_dir.mkdir(parents=True, exist_ok=True)

        for key, path in audio_map.items():
            if not Path(path).exists(): continue
            out_file = str(out_dir / Path(path).name)
            
            # القرآن: تأخير 0.5 ثانية + دخول ناعم 1 ثانية + صمت نهاية 1 ثانية
            if "ayah" in key or "recite" in key:
                filters = "aresample=44100,adelay=500|500,afade=t=in:st=0:d=1.0,apad=pad_dur=1.0,loudnorm=I=-16:TP=-1.5:LRA=11"
            else:
                # الراوي: تلاشي بسيط للدخول والخروج لمنع النقرات الصوتية
                filters = "aresample=44100,afade=t=in:st=0:d=0.2,afade=t=out:st=999:d=0.3,loudnorm=I=-16:TP=-1.5:LRA=11"

            cmd = ["ffmpeg", "-y", "-i", path, "-af", filters, "-c:a", "libmp3lame", "-q:a", "2", out_file]
            sp.run(cmd, capture_output=True)
            processed[key] = out_file
            
        return processed