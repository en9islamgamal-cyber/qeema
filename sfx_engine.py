"""
SFX Engine - QEEMA Pipeline
Adds ambient backgrounds and cinematic transition sounds (Whooshes, Chimes).
"""
import subprocess
from pathlib import Path

def add_audio_layers(video_p: Path, sfx_type: str, output_p: Path):
    """
    يدمج طبقة المؤثرات (عصافير، رياح، لمعان) مع الفيديو الأصلي.
    نفرض وجود ملفات في assets/sfx/
    """
    sfx_map = {
        "nature": "birds_ambient.mp3",
        "wonder": "magical_chime.mp3",
        "transition": "soft_whoosh.mp3"
    }
    sfx_file = Path(f"assets/sfx/{sfx_map.get(sfx_type, 'birds_ambient.mp3')}")
    
    # FFmpeg: خلط صوت الفيديو الأصلي مع المؤثر الصوتي بمستوى منخفض (15%)
    filter_complex = (
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=0:20,volume=0.15[sfx];"
        f"[0:a][sfx]amix=inputs=2:duration=first[aout]"
    )
    
    cmd = [
        "ffmpeg", "-y", "-i", str(video_p), "-i", str(sfx_file),
        "-filter_complex", filter_complex, "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", str(output_p)
    ]
    subprocess.run(cmd, check=True)
