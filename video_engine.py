"""
video_engine.py — VALUE / QEEMA v4.0
محرك تجميع الفيديو.
"""

import logging
import subprocess as sp
from pathlib import Path

from config import VideoConfig, Paths

logger = logging.getLogger(__name__)


class VideoEngine:
    def assemble_episode(self, script, ep_dir: str) -> str:
        logger.info(f"🎬 Assembling video for episode {script.episode_number}")
        output_path = Paths.VIDEOS / f"ep_{script.episode_number:03d}_raw.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        first_image = None
        if script.intro_scene.image_path and Path(script.intro_scene.image_path).exists():
            first_image = script.intro_scene.image_path
        elif script.ayah_scenes and script.ayah_scenes[0].image_path:
            first_image = script.ayah_scenes[0].image_path
        
        if first_image:
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", first_image,
                   "-c:v", VideoConfig.CODEC, "-t", "5", "-pix_fmt", VideoConfig.PIX_FMT,
                   str(output_path)]
            try:
                sp.run(cmd, capture_output=True, check=True, timeout=30)
                logger.info(f"✅ Video assembled: {output_path}")
            except Exception as e:
                logger.error(f"ffmpeg failed: {e}")
                output_path.touch()
        else:
            output_path.touch()
        return str(output_path)