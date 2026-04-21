"""
sfx_engine.py — VALUE / QEEMA v2
معالجة الصوت: موسيقى خلفية + تطبيع + ducking
"""
from __future__ import annotations
import logging, subprocess, shutil
from pathlib import Path
from config import Paths, VideoConfig

logger = logging.getLogger(__name__)

MUSIC = {
    "intro":    "intro_nasheed.mp3",
    "calm":     "calm_bg.mp3",
    "reverent": "quran_ambient.mp3",
    "happy":    "cheerful_nasheed.mp3",
    "excited":  "celebration.mp3",
    "outro":    "outro_nasheed.mp3",
}
VOLUME = {
    "under_speech": "-20dB",
    "solo":         "-6dB",
    "quran":        "-24dB",
}

def _run(cmd, label="", timeout=120):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        logger.warning(f"⚠️ {label}: {r.stderr[-100:]}")
        return False
    return True

class SFXEngine:
    def __init__(self):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg غير مثبت")

    def mix_with_music(
        self, voice_path: str, output_path: str, mood: str = "calm"
    ) -> str:
        music_f = Paths.MUSIC / MUSIC.get(mood, MUSIC["calm"])
        if not music_f.exists():
            shutil.copy(voice_path, output_path)
            return output_path

        vol = VOLUME["quran"] if mood == "reverent" else VOLUME["under_speech"]
        cmd = [
            "ffmpeg", "-y",
            "-i", voice_path,
            "-stream_loop", "-1",
            "-i", str(music_f),
            "-filter_complex",
            f"[1:a]volume={vol},afade=t=in:st=0:d=1.2,afade=t=out:st=9999:d=1.5[m];"
            f"[0:a][m]amix=inputs=2:duration=first:weights='1 0.25'[out]",
            "-map", "[out]",
            "-ar", str(VideoConfig.AUDIO_RATE),
            "-ac", "2",
            "-b:a", "192k",
            output_path,
        ]
        if _run(cmd, f"mix {mood}"):
            return output_path
        shutil.copy(voice_path, output_path)
        return output_path

    def normalize(self, input_path: str, output_path: str) -> str:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            output_path,
        ]
        if _run(cmd, "normalize"):
            return output_path
        shutil.copy(input_path, output_path)
        return output_path

    def silence(self, duration: float, output_path: str) -> str:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=r={VideoConfig.AUDIO_RATE}:cl=stereo",
            "-t", str(duration),
            output_path,
        ]
        _run(cmd, f"silence {duration}s")
        return output_path

    def process_all(self, audio_map: dict, script, ep_dir: str) -> dict:
        """يعالج جميع الملفات الصوتية بإضافة الموسيقى والتطبيع"""
        proc_dir = Path(ep_dir) / "processed_audio"
        proc_dir.mkdir(parents=True, exist_ok=True)
        processed = {}

        mood_map = {}
        for s in script.all_narrator_scenes:
            mood_map[f"scene_{s.scene_id}"] = s.mood.value
        for s in script.ayah_scenes:
            mood_map[f"ayah_{s.scene_id}"] = "reverent"

        for key, path in audio_map.items():
            if not Path(path).exists():
                processed[key] = path
                continue
            out = str(proc_dir / f"{key}.mp3")
            # تحديد المزاج
            mood = "reverent" if "quran" in key else mood_map.get(key, "calm")
            self.mix_with_music(path, out, mood)
            processed[key] = out

        logger.info(f"✅ معالجة {len(processed)} ملف صوتي")
        return processed
