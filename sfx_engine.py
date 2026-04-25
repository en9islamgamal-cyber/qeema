"""
sfx_engine.py — VALUE / QEEMA v5.0
====================================
محرك المؤثرات الصوتية الفعلي:
  - Normalize كل ملف لـ -16 LUFS (loudness consistency)
  - Trim silence من البداية والنهاية
  - Fade in/out 100ms
  - عدم إعادة التشكيل للقرآن (نتركه كما نزل من القارئ)
"""

import logging
import shutil
import subprocess as sp
from pathlib import Path
from typing import Dict

from config import SFXConfig

logger = logging.getLogger(__name__)


def _run(cmd, timeout=120) -> bool:
    try:
        r = sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            logger.warning(f"sfx ffmpeg: {r.stderr[-200:]}")
            return False
        return True
    except Exception as e:
        logger.error(f"sfx exception: {e}")
        return False


class SFXEngine:
    """محرك معالجة الصوت — تطبيع وانتقالات."""

    def _process_one(self, in_path: str, out_path: str, is_quran: bool = False) -> bool:
        """
        Pipeline:
          - silenceremove (trim silence at edges)
          - loudnorm (consistent volume)
          - afade in/out
        """
        # للآيات نتجنب الـ loudnorm عشان مانفقدش أسلوب القارئ
        if is_quran:
            af = "afade=t=in:st=0:d=0.1,afade=t=out:st=0:d=0.05"
        else:
            af = (
                "silenceremove=start_periods=1:start_silence=0.2:start_threshold=-40dB,"
                "areverse,silenceremove=start_periods=1:start_silence=0.2:start_threshold=-40dB,areverse,"
                f"loudnorm=I={SFXConfig.NORMALIZATION_TARGET}:TP=-1.5:LRA=11,"
                f"afade=t=in:st=0:d={SFXConfig.FADE_IN_DURATION},"
                f"afade=t=out:st=999:d={SFXConfig.FADE_OUT_DURATION}"
            )

        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-af", af,
            "-c:a", "libmp3lame", "-b:a", "192k",
            out_path,
        ]
        return _run(cmd, timeout=60)

    def process_all(self, audio_map: Dict[str, str], script, ep_dir: str) -> Dict[str, str]:
        """معالجة كل الصوتيات."""
        logger.info("🎵 Processing audio with normalize + fade...")
        processed: Dict[str, str] = {}
        sfx_dir = Path(ep_dir) / "sfx"
        sfx_dir.mkdir(parents=True, exist_ok=True)

        for key, src in audio_map.items():
            if not Path(src).exists():
                logger.warning(f"⚠️ Skipping missing: {src}")
                processed[key] = src
                continue

            out = str(sfx_dir / Path(src).name)
            is_quran = key.endswith("_ayah")  # تلاوات القرآن
            ok = self._process_one(src, out, is_quran=is_quran)

            if ok and Path(out).exists():
                processed[key] = out
            else:
                logger.warning(f"⚠️ SFX failed for {key}, keeping original")
                processed[key] = src

        logger.info(f"✅ Processed {len(processed)} audio files")
        return processed
