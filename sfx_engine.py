from __future__ import annotations

from __future__ import annotations

import json
import logging
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any

from config import Paths, VideoConfig

logger = logging.getLogger(__name__)

MUSIC_KEY_BY_MOOD = {
    "intro": "intro",
    "calm": "calm",
    "reverent": "reverent",
    "happy": "happy",
    "excited": "excited",
    "outro": "outro",
}

VOLUME_STRATEGY = {
    "under_speech": -20.0,
    "under_speech_ayah": -24.0,
    "solo": -6.0,
}

MOOD_SETUP = {
    "intro": {
        "music": "intro_nasheed.mp3",
        "volume": VOLUME_STRATEGY["under_speech"],
        "fade_in": 1.2,
        "fade_out": 1.5,
    },
    "quran_intro": {
        "music": "quran_ambient.mp3",
        "volume": VOLUME_STRATEGY["under_speech_ayah"],
        "fade_in": 1.0,
        "fade_out": 1.2,
    },
    "ayah": {
        "music": "quran_ambient.mp3",
        "volume": VOLUME_STRATEGY["under_speech_ayah"],
        "fade_in": 0.8,
        "fade_out": 1.0,
    },
    "explain": {
        "music": "calm_bg.mp3",
        "volume": VOLUME_STRATEGY["under_speech"],
        "fade_in": 1.0,
        "fade_out": 1.4,
    },
    "outro": {
        "music": "outro_nasheed.mp3",
        "volume": VOLUME_STRATEGY["solo"],
        "fade_in": 1.0,
        "fade_out": 2.0,
    },
    "none": {
        "music": None,
        "volume": 0.0,
        "fade_in": 0.0,
        "fade_out": 0.0,
    },
}

LOUDNORM_TARGET = {
    "I": -13.0,
    "TP": -1.5,
    "LRA": 11.0,
}

def _run(cmd: list[str], label: str = "", timeout: int = 120) -> bool:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        error = r.stderr.strip()
        logger.warning("⚠️ %s failed: %s", label, error[-200:])
        return False
    return True

def _probe_audio(path: str) -> Dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {r.stderr}")
    return json.loads(r.stdout)

def _probe_audio_duration(path: str) -> float:
    try:
        data = _probe_audio(path)
        return float(data["format"]["duration"])
    except Exception:
        logger.warning("Could not get duration for %s, assuming 10.0", Path(path).name)
        return 10.0

def _probe_audio_format(path: str) -> Dict[str, Any]:
    try:
        data = _probe_audio(path)
        for s in data.get("streams", []):
            if s.get("codec_type") == "audio":
                return {
                    "rate": int(s.get("sample_rate", VideoConfig.AUDIO_RATE)),
                    "channels": int(s.get("channels", 2)),
                }
    except Exception:
        pass
    return {"rate": VideoConfig.AUDIO_RATE, "channels": 2}

def _db_to_afade_volume(db: float) -> str:
    return f"{10 ** (db / 20):.6f}"

class SFXEngine:
    def __init__(self):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg not found — SFXEngine requires FFmpeg installed.")

    def _pick_mood(self, key: str) -> str:
        if key == "intro" or "intro" in key:
            return "intro"
        if "quran" in key:
            if "intro" in key:
                return "quran_intro"  # خاصة بالآيات
            return "ayah"
        if "explain" in key or "mid" in key:
            return "explain"
        if "outro" in key:
            return "outro"
        if "calm" in key or "narrator" in key:
            return "explain"
        return "none"

    def _pick_music_file(self, mood: str, default: str = "calm_bg.mp3") -> str:
        if mood == "none":
            return ""
        entry = MOOD_SETUP[mood]
        music_name = entry.get("music", default)
        music_path = Paths.MUSIC / music_name
        if music_path.exists():
            return str(music_path)
        return ""

    def _calculate_dynamic_fadeout(self, duration: float, base_duration: float = 1.5) -> float:
        if duration <= 2 * base_duration:
            return duration * 0.5
        return base_duration

    def _normalize_only(self, input_path: str, output_path: str) -> bool:
        """Normalized audio only using loudnorm (no music)"""
        fmt = _probe_audio_format(input_path)

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-af", (
                f"loudnorm=I={LOUDNORM_TARGET['I']:.1f}:TP={LOUDNORM_TARGET['TP']:.1f}:"
                f"LRA={LOUDNORM_TARGET['LRA']:.1f}"
            ),
            "-ar", str(fmt["rate"]),
            "-ac", str(fmt["channels"]),
            output_path,
        ]
        return _run(cmd, "SFX Normalize Only")

    def _mix_and_normalize(
        self,
        speech_path: str,
        music_path: str,
        output_path: str,
        music_volume_db: float,
        fade_in_speech: float,
        fade_out_speech: float,
    ) -> bool:
        fmt = _probe_audio_format(speech_path)
        dur = _probe_audio_duration(speech_path)

        music_volume = _db_to_afade_volume(music_volume_db)

        cmd = [
            "ffmpeg", "-y",
            "-i", speech_path,
            "-stream_loop", "-1",
            "-i", music_path,
            "-filter_complex",
            # [0] = speech, [1] = music
            # 1. Speech fade in/out
            f"[0:a]afade=t=in:st=0:d={fade_in_speech:.2f},"
            f"afade=t=out:st={max(0, dur - fade_out_speech):.2f}:d={fade_out_speech:.2f}[speech_fade];"
            # 2. Music ducking + fade in/out
            f"[1:a]volume={music_volume},"
            f"afade=t=in:st=0:d={fade_in_speech*1.2:.2f},"
            f"afade=t=out:st={max(0, dur - fade_out_speech*1.2):.2f}:d={fade_out_speech*1.2:.2f}[music_duck];"
            # 3. Mix with weights
            f"[speech_fade][music_duck]amix=inputs=2:duration=first:weights='1 0.25'[mixed];"
            # 4. Normalization
            f"[mixed]loudnorm=I={LOUDNORM_TARGET['I']:.1f}:TP={LOUDNORM_TARGET['TP']:.1f}:"
            f"LRA={LOUDNORM_TARGET['LRA']:.1f}[normalized]",
            "-map", "[normalized]",
            "-ar", str(fmt["rate"]),
            "-ac", str(fmt["channels"]),
            output_path,
        ]

        return _run(cmd, f"SFX Mix+Norm ({output_path})")

    def process_all(self, audio_map: dict, script, ep_dir: str) -> dict:
        """
        صحّح مسار التوليد:
        - لكل مقطع: نكتشف mood بناءً على key
        - نختار موسيقى ملائمة أو نستخدم normalize فقط
        - نحسب fade-out ديناميكياً حسب طول الملف
        """
        proc_dir = Path(ep_dir) / "processed_audio"
        proc_dir.mkdir(parents=True, exist_ok=True)
        processed: Dict[str, str] = {}

        for key, path in audio_map.items():
            if not Path(path).exists():
                logger.warning("⚠️ Audio file missing: %s — will keep original", path)
                processed[key] = path
                continue

            out_final = str(proc_dir / f"{key}.mp3")

            mood = self._pick_mood(key)
            setup = MOOD_SETUP.get(mood, MOOD_SETUP["none"])
            music_f = self._pick_music_file(mood) if "none" not in mood else ""

            # إذا لم توجد موسيقى، نكتفي بالتطبيع بدون مزج
            if not music_f:
                logger.debug("No music for %s, normalizing only: %s", key, mood)
                if self._normalize_only(path, out_final):
                    processed[key] = out_final
                else:
                    shutil.copy2(path, out_final)
                    processed[key] = out_final
                continue

            # معالجة مزج الكلام مع الموسيقى وتطبيع في خطوة واحدة
            dur = _probe_audio_duration(path)
            fade_in = setup["fade_in"]
            fade_out = self._calculate_dynamic_fadeout(dur, setup["fade_out"])

            if self._mix_and_normalize(
                speech_path=path,
                music_path=music_f,
                output_path=out_final,
                music_volume_db=setup["volume"],
                fade_in_speech=fade_in,
                fade_out_speech=fade_out,
            ):
                processed[key] = out_final
            else:
                shutil.copy2(path, out_final)
                processed[key] = out_final

        logger.info("🎧 SFX processing complete for %d tracks.", len(processed))
        return processed

    def silence(self, duration: float, output_path: str) -> str:
        fmt = {"rate": VideoConfig.AUDIO_RATE, "channels": 2}
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=r={fmt['rate']}:cl={'stereo' if fmt['channels'] >= 2 else 'mono'}",
            "-t", str(duration),
            "-ar", str(fmt["rate"]),
            "-ac", str(fmt["channels"]),
            output_path,
        ]
        if _run(cmd, f"Silence ({duration}s)"):
            return output_path
        else:
            raise RuntimeError("Failed to generate silence")