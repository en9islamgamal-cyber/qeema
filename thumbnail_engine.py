"""
Thumbnail Engine - QEEMA Pipeline
Generates high-impact YouTube thumbnails with text overlays.
"""
def create_pro_thumbnail(image_p: Path, title: str, output_p: Path):
    """
    يأخذ أفضل صورة من لقطات الفيديو ويضع عليها نصاً ضخماً وجذاباً.
    """
    font_p = "assets/Amiri-Bold.ttf"
    # فلتر FFmpeg لصنع نص بحدود سوداء وظل (Drop Shadow) ليظهر بوضوح
    drawtext = (
        f"drawtext=fontfile='{font_p}':text='{title}':fontcolor=white:fontsize=120:"
        f"x=(w-text_w)/2:y=(h-text_h)/2+200:borderw=5:bordercolor=black@0.8:"
        f"shadowcolor=black@0.6:shadowx=5:shadowy=5"
    )
    
    cmd = [
        "ffmpeg", "-y", "-i", str(image_p),
        "-vf", f"scale=1280:720,eq=brightness=0.05:contrast=1.2:saturation=1.3,{drawtext}",
        "-q:v", "2", str(output_p)
    ]
    subprocess.run(cmd, check=True)
