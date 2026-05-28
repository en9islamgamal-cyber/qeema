"""
video/video_assembler.py
====================================================================
Assembles the final 1080p MP4.

Process:
  1. For each visual scene, create an animated video segment
     (still image → slow zoom/pan → MP4 with target duration)
  2. Concatenate scenes with crossfade transitions
  3. Overlay the persistent logo watermark
  4. Mux with the final mixed audio
  5. Add 2-second logo splash at the start (intro animation)

Key design:
  - Each scene's exact duration is computed from the audio timeline.
  - Motion now varies by scene type instead of always using
    the same center zoom.
  - Intro / recitation scenes stay calm.
  - Explanation scenes get more directional movement.
  - Outro scenes can end with a full-board zoom out.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from core.config import (
    TEMP_DIR,
    LOGO_PATH,
    get_pipeline_config,
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
    motion_hint: Optional[str] = None


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

    scene_clips: List[Path] = []
    for i, sc in enumerate(scenes):
        clip = work_dir / f"scene_{i:02d}_{_safe_label(sc.label)}.mp4"
        _make_motion_clip(
            image=sc.image_path,
            output=clip,
            duration_sec=sc.duration_sec,
            cfg=cfg,
            scene_index=i,
            label=sc.label,
            motion_hint=sc.motion_hint,
        )
        scene_clips.append(clip)

    concat_with_xfade = work_dir / "scenes_concat.mp4"
    if len(scene_clips) == 1:
        import shutil
        shutil.copy2(scene_clips[0], concat_with_xfade)
    else:
        _concat_with_crossfade(scene_clips, concat_with_xfade, cfg)

    with_logo = work_dir / "scenes_with_logo.mp4"
    if LOGO_PATH.exists() and cfg.logo_overlay_enabled:
        _overlay_logo(concat_with_xfade, with_logo, cfg)
    else:
        log.warning("Logo not found at %s; skipping watermark", LOGO_PATH)
        import shutil
        shutil.copy2(concat_with_xfade, with_logo)

    final_no_intro = work_dir / "with_audio.mp4"
    _mux_audio(with_logo, audio_path, final_no_intro)

    if LOGO_PATH.exists():
        _add_logo_intro(final_no_intro, output, cfg)
    else:
        import shutil
        shutil.copy2(final_no_intro, output)

    log.info("✅ Final video assembled: %s", output)
    return output


# ════════════════════════════════════════════════════════════════════
# Motion clip per scene
# ════════════════════════════════════════════════════════════════════

def _make_motion_clip(
    image: Path,
    output: Path,
    duration_sec: float,
    cfg,
    scene_index: int,
    label: str = "",
    motion_hint: Optional[str] = None,
) -> None:
    """
    Animate a still image with a scene-aware motion preset.

    Unlike the old implementation, this does NOT always do a center
    zoom-in. It chooses a motion preset based on scene type and
    (optionally) motion_hint.
    """
    if duration_sec < 0.5:
        duration_sec = 0.5

    frames = max(1, int(duration_sec * cfg.video_fps))
    scene_kind = _classify_scene_kind(label, motion_hint)
    motion = _pick_motion_preset(scene_index, scene_kind, motion_hint)
    filter_complex = _build_motion_filter(
        cfg=cfg,
        frames=frames,
        motion=motion,
        scene_kind=scene_kind,
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
        raise VideoAssembleError(f"Motion clip failed: {r.stderr[:400]}")


def _classify_scene_kind(label: str = "", motion_hint: Optional[str] = None) -> str:
    """
    Determine the semantic type of the scene from its label and/or hint.
    """
    text = f"{label or ''} {motion_hint or ''}".strip().lower()

    quran_markers = [
        "quran", "tilawah", "recitation", "verse", "ayah", "تلاوة", "قرآن", "آية"
    ]
    hook_markers = ["hook", "opening", "افتتاح", "هوك"]
    intro_markers = ["intro", "opening recitation", "مقدمة"]
    outro_markers = ["outro", "closing", "end", "خاتمة", "نهاية"]
    thumb_markers = ["thumbnail", "thumb"]

    if any(m in text for m in thumb_markers):
        return "thumbnail"
    if any(m in text for m in outro_markers):
        return "outro"
    if any(m in text for m in hook_markers):
        return "hook"
    if any(m in text for m in intro_markers):
        return "intro"
    if any(m in text for m in quran_markers):
        return "quran"
    return "explain"


def _pick_motion_preset(
    scene_index: int,
    scene_kind: str,
    motion_hint: Optional[str] = None,
) -> str:
    """
    Select a motion preset.

    We stay conservative here:
    - quran / intro → calmer motions
    - hook → slightly stronger entry movement
    - explain → varied movement for storytelling
    - outro → clear pullback to reveal the full board
    """
    hint = (motion_hint or "").lower()

    if "zoom out" in hint or "pull back" in hint or "full-board" in hint:
        return "outro_pullback"

    if scene_kind == "thumbnail":
        return "still_soft"

    if scene_kind == "quran":
        presets = ["gentle_push_in", "still_soft", "gentle_push_in"]

    elif scene_kind == "intro":
        presets = ["gentle_reveal", "gentle_push_in"]

    elif scene_kind == "hook":
        presets = ["push_in", "pan_left", "pan_right"]

    elif scene_kind == "outro":
        presets = ["outro_pullback"]

    else:  # explain
        presets = ["push_in", "push_out", "pan_left", "pan_right", "gentle_reveal"]

    return presets[scene_index % len(presets)]


def _build_motion_filter(
    cfg,
    frames: int,
    motion: str,
    scene_kind: str,
) -> str:
    """
    Build an ffmpeg zoompan filter.

    Motion presets:
      - push_in
      - push_out
      - pan_left
      - pan_right
      - gentle_push_in
      - gentle_reveal
      - still_soft
      - outro_pullback
    """
    base_zoom_pct = float(getattr(cfg, "ken_burns_zoom_pct", 15.0))
    if base_zoom_pct < 2.0:
        base_zoom_pct = 15.0

    if scene_kind in {"quran", "intro"}:
        zoom_pct = min(base_zoom_pct, 8.0)
    elif scene_kind == "hook":
        zoom_pct = min(max(base_zoom_pct, 16.0), 24.0)
    elif scene_kind == "outro":
        zoom_pct = min(max(base_zoom_pct, 12.0), 20.0)
    elif scene_kind == "thumbnail":
        zoom_pct = 3.0
    else:
        zoom_pct = base_zoom_pct

    zoom_delta = zoom_pct / 100.0
    final_zoom = 1.0 + zoom_delta
    zoom_step = zoom_delta / max(frames, 1)

    if motion == "push_in":
        z_expr = f"min(zoom+{zoom_step:.6f},{final_zoom:.4f})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    elif motion == "push_out":
        start_zoom = final_zoom
        z_expr = f"max({1.0:.4f},{start_zoom:.4f}-on*{zoom_step:.6f})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    elif motion == "pan_left":
        z_expr = f"{final_zoom:.4f}"
        x_expr = f"(iw-iw/zoom)*(1-on/{max(frames,1)})"
        y_expr = "ih/2-(ih/zoom/2)"

    elif motion == "pan_right":
        z_expr = f"{final_zoom:.4f}"
        x_expr = f"(iw-iw/zoom)*(on/{max(frames,1)})"
        y_expr = "ih/2-(ih/zoom/2)"

    elif motion == "gentle_push_in":
        soft_delta = (zoom_pct * 0.5) / 100.0
        soft_final = 1.0 + soft_delta
        soft_step = soft_delta / max(frames, 1)
        z_expr = f"min(zoom+{soft_step:.6f},{soft_final:.4f})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    elif motion == "gentle_reveal":
        soft_delta = (zoom_pct * 0.4) / 100.0
        start_zoom = 1.0 + soft_delta
        soft_step = soft_delta / max(frames, 1)
        z_expr = f"max({1.0:.4f},{start_zoom:.4f}-on*{soft_step:.6f})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    elif motion == "outro_pullback":
        start_zoom = 1.0 + zoom_delta
        z_expr = f"max({1.0:.4f},{start_zoom:.4f}-on*{zoom_step:.6f})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    else:  # still_soft
        soft_delta = 0.015
        soft_final = 1.0 + soft_delta
        soft_step = soft_delta / max(frames, 1)
        z_expr = f"min(zoom+{soft_step:.6f},{soft_final:.4f})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    return (
        f"scale=3840:2160,"
        f"zoompan="
        f"z='{z_expr}':"
        f"x='{x_expr}':"
        f"y='{y_expr}':"
        f"d={frames}:"
        f"s={cfg.video_width}x{cfg.video_height}:"
        f"fps={cfg.video_fps}"
    )


# ════════════════════════════════════════════════════════════════════
# Concat with crossfade
# ════════════════════════════════════════════════════════════════════

def _concat_with_crossfade(clips: List[Path], output: Path, cfg) -> None:
    """
    Concatenate video clips with xfade transitions between them.
    """
    fade_sec = cfg.image_crossfade_ms / 1000.0
    durations = [_get_duration_sec(c) for c in clips]

    inputs = []
    for c in clips:
        inputs.extend(["-i", str(c)])

    filter_lines = []
    prev_label = "[0:v]"
    cumulative_offset = 0.0

    for i in range(1, len(clips)):
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

    if position == "bottom_right":
        x_expr = f"W-w-{margin}"
        y_expr = f"H-h-{margin}"
    elif position == "top_right":
        x_expr = f"W-w-{margin}"
        y_expr = f"{margin}"
    elif position == "bottom_left":
        x_expr = f"{margin}"
        y_expr = f"H-h-{margin}"
    else:
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
    """
    work_dir = TEMP_DIR / "video_assembly"
    splash_clip = work_dir / "logo_splash.mp4"

    w = cfg.video_width
    h = cfg.video_height
    logo_w = cfg.logo_intro_width
    duration = cfg.logo_intro_duration_sec

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
        "-c:v", "libx264",
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-t", f"{duration}",
        "-an",
        str(splash_clip),
    ]
    r = subprocess.run(cmd_splash, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Splash gen failed: {r.stderr[:400]}")

    splash_with_audio = work_dir / "logo_splash_audio.mp4"
    cmd_aud = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(splash_clip),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(splash_with_audio),
    ]
    r = subprocess.run(cmd_aud, capture_output=True, text=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Splash audio mux failed: {r.stderr[:400]}")

    list_file = work_dir / "splash_concat.txt"
    list_file.write_text(
        f"file '{splash_with_audio.resolve()}'
"
        f"file '{src.resolve()}'
",
        encoding="utf-8",
    )

    cmd_concat = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:v", "libx264",
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(dst),
    ]
    r = subprocess.run(cmd_concat, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if r.returncode != 0:
        raise VideoAssembleError(f"Splash concat failed: {r.stderr[:400]}")


def _safe_label(label: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in (label or "").strip())
    return cleaned or "x"