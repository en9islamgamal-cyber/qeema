"""
Gamification Engine - QEEMA Pipeline
Adds visual progress bars and interactive end-screens.
"""
def add_interactive_progress_bar(video_p: Path, output_p: Path):
    """
    يرسم شريط تقدم (Progress Bar) في أسفل الفيديو يتحرك مع الوقت.
    """
    # لون ذهبي لشريط التقدم ليتناسب مع "قيمة"
    bar_color = "0xFFD700"
    filter_chain = (
        f"drawbox=y=ih-15:color={bar_color}@0.8:width=iw*(t/duration):height=10:t=fill"
    )
    
    cmd = [
        "ffmpeg", "-y", "-i", str(video_p),
        "-vf", filter_chain, "-c:a", "copy", str(output_p)
    ]
    subprocess.run(cmd, check=True)
