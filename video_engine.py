"""
Video & Motion Graphics Engine - QEEMA Pipeline
Handles FFmpeg complex filters for cinematic movements, logo integration, and scene assembly.
"""

import os
import subprocess
import logging
from pathlib import Path

log = logging.getLogger("qeema_video")

# =============================================================================
# 1. CAMERA MOVEMENT LOGIC (Dynamic Ken Burns)
# =============================================================================

def get_camera_filter(movement_type: str, fps: int = 30) -> str:
    """
    Translates script directions into FFmpeg zoompan filters.
    """
    # Base zoompan settings: 1920x1080 output, smoothing applied
    base_settings = f"d=0:s=1920x1080:fps={fps}"
    
    if movement_type == "zoom_in":
        # Slow zoom in to center
        return f"zoompan=z='min(zoom+0.0008,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':{base_settings}"
    
    elif movement_type == "zoom_out":
        # Start zoomed in (1.15), slowly zoom out to 1.0
        return f"zoompan=z='if(eq(on,1),1.15,max(zoom-0.0008,1.0))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':{base_settings}"
    
    elif movement_type == "pan_right":
        # Zoomed in slightly (1.15), pan camera to the right
        return f"zoompan=z='1.15':x='if(eq(on,1),0,x+1.5)':y='ih/2-(ih/zoom/2)':{base_settings}"
        
    elif movement_type == "pan_left":
        # Zoomed in slightly (1.15), pan camera to the left
        return f"zoompan=z='1.15':x='if(eq(on,1),iw-iw/zoom,x-1.5)':y='ih/2-(ih/zoom/2)':{base_settings}"
        
    else:
        # Default: Very subtle slow zoom in (Breath effect)
        return f"zoompan=z='min(zoom+0.0003,1.05)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':{base_settings}"

# =============================================================================
# 2. SCENE ASSEMBLY (The "Editor")
# =============================================================================

def assemble_cinematic_scene(
    image_path: Path, 
    audio_path: Path, 
    logo_path: Path, 
    output_path: Path, 
    camera_movement: str = "zoom_in"
) -> None:
    """
    Combines Image, Audio, applies camera movement, and adds the logo watermark.
    """
    log.info(f"🎞️ Assembling scene: {output_path.name} | Movement: {camera_movement}")
    
    # 1. Get the motion filter
    motion_filter = get_camera_filter(camera_movement)
    
    # 2. Build the complex FFmpeg filter chain
    # [0:v] is the image. We scale it up first to avoid pixelation during zoom.
    # [2:v] is the logo. We scale it, make it slightly transparent (colorchannelmixer).
    # Then we overlay the logo on top of the moving background.
    filter_complex = (
        f"[0:v]scale=3840:2160,{motion_filter},format=yuv420p[bg];"
        f"[2:v]scale=150:150,format=rgba,colorchannelmixer=aa=0.85[wm];"
        f"[bg][wm]overlay=W-w-40:40[vout]" # Position logo Top-Right with 40px margin
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", "30", "-i", str(image_path),  # Input 0: Image
        "-i", str(audio_path),                                    # Input 1: Audio
        "-loop", "1", "-framerate", "30", "-i", str(logo_path),   # Input 2: Logo
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "1:a",                          # Map video out and audio in
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",     # High quality video
        "-c:a", "aac", "-b:a", "192k",                            # High quality audio
        "-shortest",                                              # End when the shortest stream (audio) ends
        "-movflags", "+faststart",
        str(output_path)
    ]
    
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        log.info(f"✅ Scene {output_path.name} created successfully!")
    except subprocess.CalledProcessError as e:
        log.error(f"❌ Failed to assemble scene:\n{e.stderr[-1500:]}")
        raise

# =============================================================================
# 3. BRANDING: INTRO & OUTRO
# =============================================================================

def create_branding_sequence(logo_path: Path, output_path: Path, is_intro: bool = True) -> None:
    """
    Creates a smooth 4-second animation for the logo (Intro or Outro).
    """
    duration = 4.0
    log.info(f"🎬 Creating {'Intro' if is_intro else 'Outro'} sequence...")
    
    # Background color based on your brand (e.g., a warm cream or dark blue)
    bg_color = "0xFFFFFF" if is_intro else "0x1A2A4F"
    
    filter_complex = (
        f"[0:v]format=yuv420p[bg];"
        f"[1:v]scale=500:500,format=rgba[logo];"
        f"[bg][logo]overlay=(W-w)/2:(H-h)/2," # Center the logo
        f"fade=t=in:st=0:d=1.0,fade=t=out:st={duration - 1.0}:d=1.0[vout]" # Fade in and out
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={bg_color}:s=1920x1080:d={duration}:r=30", # Background
        "-loop", "1", "-framerate", "30", "-t", str(duration), "-i", str(logo_path), # Logo
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}", # Silent audio track
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(output_path)
    ]
    
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        log.info("✅ Branding sequence created!")
    except subprocess.CalledProcessError as e:
        log.error(f"❌ Failed to create branding sequence:\n{e.stderr[-1000:]}")
        raise
