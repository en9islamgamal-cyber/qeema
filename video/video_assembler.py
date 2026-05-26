"""
video/video_assembler.py
====================================================================
Assembles the final 1080p MP4.

Process:
  1. For each visual scene, create a Ken Burns animated video segment
     (still image → slow zoom-pan → MP4 with target duration)
  2. Concatenate scenes with crossfade transitions
  3. Overlay the persistent logo watermark
  4. Mux with the final mixed audio
  5. Add 2-second logo splash at the start (intro animation)

Key design: we compute each scene's exact duration from the audio
timeline (built by audio_director). Scenes match audio segments 1:1.

Logo behavior:
  - Intro splash: large logo (420px) for first 2 seconds, fades out
  - Watermark: small logo (180px) bottom-right corner, 75% opacity,
    visible throughout
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from core.config import (
    TEMP_DIR, LOGO_PATH, get_pipeline_config,
)


log = logging.getLogger(__name__)


class VideoAssembleError(Exception):
    pass


@dataclass
class VideoScene:
    """A single scene: one image, one duration."""
    image_path: Path
    duration_sec: float
    label: str = ""


def build_episode_video(
    scenes: List[VideoScene],
    audio_path: Path,
    output: Path,
) -> Path:
    """
    Assemble the final episode video.

    scenes: list of (image, duration) — should match the audio timeline
    audio_path: the mixed episode audio (from audio_director)
    output: final MP4 path
    """
    cfg = get_pipeline_config()
    work_dir = TEMP_DIR / "video_assembly"
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    # 1) For each scene, create a Ken-Burns animated video clip
    scene_clips: List[Path] = []
    for i, sc in enumerate(scenes):
        clip = work_dir / f"scene_{i:02d}_{sc.label or 'x'}.mp4"
        _make_ken_burns_clip(sc.image_path, clip, sc.duration_sec, cfg)
        scene_clips.append(clip)

    # 2) Concatenate scenes with crossfades
    concat_with_xfade = work_dir / "scenes_concat.mp4"
    if len(scene_clips) == 1:
        # No crossfade needed; just use the single clip
        import shutil
        shutil.copy2(scene_clips[0], concat_with_xfade)
    else:
        _concat_with_crossfade(scene_clips, concat_with_xfade, cfg)

    # 3) Overlay logo watermark
    with_logo = work_dir / "scenes_with_logo.mp4"
    if LOGO_PATH.exists() and cfg.logo_overlay_enabled:
        _overlay_logo(concat_with_xfade, with_logo, cfg)
    else:
        log.warning(
            "Logo not found at %s; skipping watermark", LOGO_PATH,
        )
        import shutil
        shutil.copy2(concat_with_xfade, with_logo)

    # 4) Mux with audio
    final_no_intro = work_dir / "with_audio.mp4"
    _mux_audio(with_logo, audio_path, final_no_intro)

    # 5) Logo splash at the start (concat into final)
    if LOGO_PATH.exists():
        _add_logo_intro(final_no_intro, output, cfg)
    else:
        import shutil
        shutil.copy2(final_no_intro, output)

    log.info("✅ Final video assembled: %s", output)
    return output


# ════════════════════════════════════════════════════════════════════
# Ken Burns animation per scene
# ════════════════════════════════════════════════════════════════════

def _make_ken_burns_clip(
    image: Path, output: Path, duration_sec: float, cfg,
) -> None:
    """
    Animate a still image with slow zoom (Ken Burns effect).

    We use ffmpeg's zoompan filter for a smooth slow zoom-in.
    """
    if duration_sec < 0.5:
        duration_sec = 0.5

    frames = int(duration_sec * cfg.video_fps)
    zoom_increment = (cfg.ken_burns_zoom_pct / 100.0) / frames
    final_zoom = 1.0 + cfg.ken_burns_zoom_pct / 100.0

    # zoompan filter: scale up to 4K for smooth zoom, then zoom+pan
    filter_complex = (
        f"scale=3840:2160,"
        f"zoompan=z='min(zoom+{zoom_increment:.6f},{final_zoom})':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={cfg.video_width}x{cfg.video_height}:fps={cfg.video_fps}"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-loop", "1",
        "-i", str(image),
        "-vf", filter_complex,
        "-t", f"{duration_sec:.3f}",
        "-c:v", "libx264",
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-r", str(cfg.video_fps),
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Ken Burns failed: {r.stderr[:400]}")


# ════════════════════════════════════════════════════════════════════
# Concat with crossfade
# ════════════════════════════════════════════════════════════════════

def _concat_with_crossfade(clips: List[Path], output: Path, cfg) -> None:
    """
    Concatenate video clips with xfade transitions between them.

    Uses ffmpeg's xfade filter.
    """
    fade_sec = cfg.image_crossfade_ms / 1000.0

    # Get duration of each clip
    durations = [_get_duration_sec(c) for c in clips]

    # Build the filter graph for xfade between consecutive clips
    # Each clip gets a label [v0], [v1], ...
    inputs = []
    for c in clips:
        inputs.extend(["-i", str(c)])

    # Build the xfade chain
    filter_lines = []
    prev_label = "[0:v]"
    cumulative_offset = 0.0
    for i in range(1, len(clips)):
        # offset = sum(durations[0..i-1]) - fade_sec * i  ... simpler:
        cumulative_offset += durations[i - 1] - fade_sec
        out_label = f"[v{i}]" if i < len(clips) - 1 else "[vout]"
        filter_lines.append(
            f"{prev_label}[{i}:v]xfade=transition=fade:"
            f"duration={fade_sec:.3f}:offset={cumulative_offset:.3f}"
            f"{out_label}"
        )
        prev_label = out_label

    filter_complex = ";".join(filter_lines)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264",
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-r", str(cfg.video_fps),
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Xfade concat failed: {r.stderr[:400]}")


def _get_duration_sec(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"ffprobe failed: {r.stderr[:300]}")
    return float(r.stdout.strip())


# ════════════════════════════════════════════════════════════════════
# Logo overlay (watermark)
# ════════════════════════════════════════════════════════════════════

def _overlay_logo(src: Path, dst: Path, cfg) -> None:
    """Add a persistent logo watermark to every frame."""
    w = cfg.logo_overlay_width
    margin = cfg.logo_overlay_margin
    opacity = cfg.logo_overlay_opacity
    position = cfg.logo_overlay_position

    # Compute x,y based on position
    if position == "bottom_right":
        x_expr = f"W-w-{margin}"
        y_expr = f"H-h-{margin}"
    elif position == "top_right":
        x_expr = f"W-w-{margin}"
        y_expr = f"{margin}"
    elif position == "bottom_left":
        x_expr = f"{margin}"
        y_expr = f"H-h-{margin}"
    else:  # top_left
        x_expr = f"{margin}"
        y_expr = f"{margin}"

    filter_complex = (
        f"[1:v]scale={w}:-1,format=rgba,"
        f"colorchannelmixer=aa={opacity}[logo];"
        f"[0:v][logo]overlay=x={x_expr}:y={y_expr}"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(src),
        "-i", str(LOGO_PATH),
        "-filter_complex", filter_complex,
        "-c:v", "libx264",
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Logo overlay failed: {r.stderr[:400]}")


# ════════════════════════════════════════════════════════════════════
# Mux video + audio
# ════════════════════════════════════════════════════════════════════

def _mux_audio(video: Path, audio: Path, output: Path) -> None:
    """Combine video stream with audio stream."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(video), "-i", str(audio),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Mux failed: {r.stderr[:400]}")


# ════════════════════════════════════════════════════════════════════
# Logo splash at the start
# ════════════════════════════════════════════════════════════════════

def _add_logo_intro(src: Path, dst: Path, cfg) -> None:
    """
    Prepend a 2-second logo splash to the video.

    Creates a clip with the large logo centered on a dark backdrop,
    then concats it with the main video.
    """
    work_dir = TEMP_DIR / "video_assembly"
    splash_clip = work_dir / "logo_splash.mp4"

    w = cfg.video_width
    h = cfg.video_height
    logo_w = cfg.logo_intro_width
    duration = cfg.logo_intro_duration_sec

    # Build splash: dark background + centered logo with fade out
    filter_complex = (
        f"color=c=0x1a1a1a:s={w}x{h}:d={duration:.2f}:r={cfg.video_fps}[bg];"
        f"[1:v]scale={logo_w}:-1[lg];"
        f"[bg][lg]overlay=x=(W-w)/2:y=(H-h)/2:enable='between(t,0,{duration})',"
        f"fade=t=out:st={duration - 0.3:.2f}:d=0.3"
    )

    cmd_splash = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:d={duration}:r={cfg.video_fps}",
        "-i", str(LOGO_PATH),
        "-filter_complex", filter_complex,
        "-c:v", "libx264", "-preset", cfg.video_preset, "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-t", f"{duration}",
        "-an",  # no audio in splash
        str(splash_clip),
    ]
    r = subprocess.run(cmd_splash, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Splash gen failed: {r.stderr[:400]}")

    # Add silent audio to splash so concat works (a→v map)
    splash_with_audio = work_dir / "logo_splash_audio.mp4"
    cmd_aud = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(splash_clip),
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(splash_with_audio),
    ]
    r = subprocess.run(cmd_aud, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Splash audio mux failed: {r.stderr[:400]}")

    # Concat splash + main video (need to re-encode for safety)
    list_file = work_dir / "splash_concat.txt"
    list_file.write_text(
        f"file '{splash_with_audio.resolve()}'\n"
        f"file '{src.resolve()}'\n",
        encoding="utf-8",
    )
    cmd_concat = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:v", "libx264", "-preset", cfg.video_preset, "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(dst),
    ]
    r = subprocess.run(cmd_concat, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Splash concat failed: {r.stderr[:400]}")
