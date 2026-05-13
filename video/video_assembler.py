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
    pre_outro = work_dir / "pre_outro.mp4"
    if LOGO_PATH.exists():
        _add_logo_intro(final_no_intro, pre_outro, cfg)
    else:
        import shutil
        shutil.copy2(final_no_intro, pre_outro)

    # 6) Append outro animation if available
    from core.config import OUTRO_VIDEO_PATH
    if OUTRO_VIDEO_PATH.exists():
        log.info("Appending outro animation from %s", OUTRO_VIDEO_PATH)
        _append_outro_animation(pre_outro, OUTRO_VIDEO_PATH, output, cfg)
    else:
        log.info("No outro animation found at %s; skipping append", OUTRO_VIDEO_PATH)
        import shutil
        shutil.copy2(pre_outro, output)

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


# ════════════════════════════════════════════════════════════════════
# Outro animation appending
# ════════════════════════════════════════════════════════════════════

def _append_outro_animation(
    main_video: Path,
    outro_video: Path,
    output: Path,
    cfg,
) -> None:
    """
    Append the channel outro animation to the end of the main video.

    Process:
      1. Convert the outro from its native aspect ratio (often 9:16 vertical)
         to 16:9 (1920×1080) using a blurred-background fill technique:
         - Take the outro frames
         - Scale up + crop them as a blurred background filling the canvas
         - Overlay the original outro at its native aspect, centered
      2. Crossfade the main video into the outro (smooth audio + video transition)

    The result: main video ends → 0.8s crossfade → outro animation begins
    """
    work_dir = TEMP_DIR / "video_assembly"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ─── Step 1: Convert outro to 16:9 with blurred background fill ────
    outro_16x9 = work_dir / "outro_16x9.mp4"
    _convert_outro_to_16x9(outro_video, outro_16x9, cfg)

    # ─── Step 2: Get durations for crossfade math ────────────────────
    main_duration = _get_duration_sec(main_video)
    outro_duration = _get_duration_sec(outro_16x9)
    crossfade_sec = 0.8  # ناعم بس مش طويل

    # The crossfade offset = when in the MAIN video the xfade should START
    xfade_offset = max(0.1, main_duration - crossfade_sec)

    log.info(
        "Outro append: main=%.1fs, outro=%.1fs, crossfade=%.1fs (offset=%.1fs)",
        main_duration, outro_duration, crossfade_sec, xfade_offset,
    )

    # ─── Step 3: Concatenate with crossfade on both video AND audio ──
    filter_complex = (
        f"[0:v][1:v]xfade=transition=fade:duration={crossfade_sec:.3f}:"
        f"offset={xfade_offset:.3f}[vout];"
        f"[0:a][1:a]acrossfade=d={crossfade_sec:.3f}[aout]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(main_video),
        "-i", str(outro_16x9),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264",
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(
            f"Outro append failed: {r.stderr[:600]}"
        )

    log.info("✓ Outro appended successfully: %s", output)


def _convert_outro_to_16x9(
    src: Path,
    dst: Path,
    cfg,
) -> None:
    """
    Convert a video (possibly 9:16 vertical) to 16:9 (1920×1080)
    using the blurred-background fill technique.

    If the source is already 16:9 or wider, just scale to 1920×1080.
    """
    src_w, src_h = _get_video_dimensions(src)
    target_w = cfg.video_width    # 1920
    target_h = cfg.video_height   # 1080
    target_aspect = target_w / target_h
    src_aspect = src_w / src_h

    log.info(
        "Converting outro %dx%d (aspect %.2f) to %dx%d (aspect %.2f)",
        src_w, src_h, src_aspect, target_w, target_h, target_aspect,
    )

    if abs(src_aspect - target_aspect) < 0.1:
        # Already roughly 16:9, just scale
        log.info("Outro is already ~16:9, scaling only")
        filter_complex = f"scale={target_w}:{target_h}"
    elif src_aspect < target_aspect:
        # Source is narrower (vertical) — blurred background fill
        log.info("Outro is vertical, applying blurred-background fill")
        filter_complex = (
            f"[0:v]split=2[orig][bg];"
            f"[bg]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},"
            f"boxblur=luma_radius=40:luma_power=2,"
            f"eq=brightness=-0.15[blurred];"
            f"[orig]scale=-1:{target_h}:force_original_aspect_ratio=decrease[fg];"
            f"[blurred][fg]overlay=(W-w)/2:0:format=auto"
        )
    else:
        # Source is wider than 16:9 — letterbox top/bottom
        log.info("Outro is ultra-wide, letterboxing")
        filter_complex = (
            f"[0:v]split=2[orig][bg];"
            f"[bg]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},"
            f"boxblur=luma_radius=40:luma_power=2,"
            f"eq=brightness=-0.15[blurred];"
            f"[orig]scale={target_w}:-1:force_original_aspect_ratio=decrease[fg];"
            f"[blurred][fg]overlay=0:(H-h)/2:format=auto"
        )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(src),
        "-filter_complex", filter_complex,
        "-c:v", "libx264",
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(
            f"Outro aspect conversion failed: {r.stderr[:600]}"
        )


def _get_video_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) of a video file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=,:p=0",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"ffprobe failed: {r.stderr[:300]}")
    try:
        parts = r.stdout.strip().split(",")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        raise VideoAssembleError(f"Bad ffprobe output: {r.stdout!r}")
