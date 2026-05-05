"""
infrastructure/bgm_mixer.py — VALUE / QEEMA v14.0 (NEW)
=========================================================
Background music mixing + video post-processing effects.

[Responsibilities]
1. BGM mixing: overlay low-volume nasheed on the final episode audio
2. Crossfade transitions: apply xfade between scene segments
3. Subtitle burning: embed ASS subtitle file into video
4. Final polish pass: color grading + loudnorm mastering

[BGM Strategy]
- BGM plays at ~-22dB relative to narrator voice
- Fades in over first 3 seconds (from silence)
- Fades out over last 4 seconds (to silence)
- Loops seamlessly if episode is longer than BGM file
- Uses FFmpeg's [0:a][1:a]amix with volume weights

[Crossfade Strategy]
- Duration: 0.5s between scenes (configurable)
- Uses FFmpeg xfade filter: fade/fadeblack/smoothleft
- Applied during concat stage (not per-scene render)
- Falls back to hard cut if segment count > 30 (performance)

[Usage in orchestrator]
    bgm = BGMMixer(paths=paths, video_cfg=video_cfg)
    
    # After wrap_branded:
    polished = bgm.apply_bgm(branded_video, output_path)
    
    # Optional subtitles:
    with_subs = bgm.burn_subtitles(polished, ass_path, output_path)
"""
from __future__ import annotations

import logging
import subprocess as sp
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default BGM volume: -22dB relative (narrator stays dominant)
DEFAULT_BGM_VOLUME = 0.08   # linear: 0.08 ≈ -22dB
DEFAULT_FADE_IN_SEC = 3.0
DEFAULT_FADE_OUT_SEC = 4.0


class BGMMixer:
    """
    Background music mixer + video post-processor for QEEMA episodes.
    All operations are atomic (write to .partial, rename on success).
    """

    def __init__(
        self,
        *,
        paths,
        bgm_volume: float = DEFAULT_BGM_VOLUME,
        fade_in_sec: float = DEFAULT_FADE_IN_SEC,
        fade_out_sec: float = DEFAULT_FADE_OUT_SEC,
    ) -> None:
        self._paths = paths
        self._bgm_volume = bgm_volume
        self._fade_in_sec = fade_in_sec
        self._fade_out_sec = fade_out_sec

    # ─── Public API ──────────────────────────────────────────────
    def apply_bgm(
        self,
        video_path: str,
        output_path: str,
        bgm_path: Optional[str] = None,
    ) -> str:
        """
        Mix background nasheed into the episode video.
        If bgm_path is None, uses paths.bgm_file.
        If BGM file doesn't exist, returns video_path unchanged (graceful).
        """
        bgm = bgm_path or str(self._paths.bgm_file)
        if not Path(bgm).exists():
            logger.warning(f"⚠️ BGM file not found: {bgm} — skipping BGM mix")
            return video_path

        vid = Path(video_path)
        if not vid.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        out = Path(output_path)
        tmp = out.with_suffix(".partial.mp4")

        # Get video duration for fade-out timing
        duration = self._probe_duration(str(vid))
        if duration is None:
            logger.warning("⚠️ Could not probe video duration — skipping BGM")
            return video_path

        fade_out_start = max(duration - self._fade_out_sec, self._fade_in_sec + 1.0)

        # FFmpeg filter:
        # [1:a] = BGM stream, looped to match video duration
        # afade in + afade out on BGM
        # amix with narrator voice dominant
        bgm_filter = (
            f"[1:a]"
            f"aloop=loop=-1:size=2e+09,"          # loop BGM forever
            f"atrim=0:{duration:.3f},"             # trim to video duration
            f"afade=t=in:st=0:d={self._fade_in_sec:.1f},"
            f"afade=t=out:st={fade_out_start:.3f}:d={self._fade_out_sec:.1f},"
            f"volume={self._bgm_volume:.4f}"
            f"[bgm];"
            f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=3[aout]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(vid),
            "-i", bgm,
            "-filter_complex", bgm_filter,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            "-movflags", "+faststart",
            str(tmp),
        ]

        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error(f"❌ BGM mix failed:\n{result.stderr[-400:]}")
                return video_path
            tmp.replace(out)
            logger.info(f"✅ BGM mixed: {out.name} (bgm@{self._bgm_volume:.2f}x volume)")
            return str(out)
        except sp.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            logger.warning("⚠️ BGM mix timeout — skipping")
            return video_path
        except Exception as e:
            tmp.unlink(missing_ok=True)
            logger.error(f"❌ BGM mix error: {e}")
            return video_path

    def burn_subtitles(
        self,
        video_path: str,
        ass_path: str,
        output_path: str,
    ) -> str:
        """
        Burn ASS subtitles into the video using FFmpeg ass filter.
        Atomic write. Returns output_path on success, video_path on failure.
        """
        if not Path(ass_path).exists():
            logger.warning(f"⚠️ ASS file not found: {ass_path} — skipping subtitles")
            return video_path

        out = Path(output_path)
        tmp = out.with_suffix(".partial.mp4")

        # Escape path for FFmpeg filter (Windows/Linux compat)
        safe_ass = str(Path(ass_path).absolute()).replace("\\", "/").replace(":", "\\:")

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", f"ass={safe_ass}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(tmp),
        ]

        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                logger.error(f"❌ Subtitle burn failed:\n{result.stderr[-400:]}")
                return video_path
            tmp.replace(out)
            logger.info(f"✅ Subtitles burned: {out.name}")
            return str(out)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            logger.error(f"❌ Subtitle burn error: {e}")
            return video_path

    def concat_with_crossfades(
        self,
        segments: List[str],
        output_path: str,
        *,
        transition_duration: float = 0.5,
        transition_type: str = "fade",
        assembler=None,
    ) -> str:
        """
        Concatenate video segments with smooth crossfade transitions.
        
        For episode with N segments, applies (N-1) xfade transitions.
        Falls back to simple concat if >20 segments (performance).
        
        transition_type: fade | fadeblack | wipeleft | smoothleft | circlecrop
        """
        if not segments:
            raise ValueError("No segments to concatenate")

        if len(segments) == 1:
            import shutil
            shutil.copy(segments[0], output_path)
            return output_path

        # For large episode segment counts, use simple concat (faster)
        if len(segments) > 20:
            logger.info(f"ℹ️ {len(segments)} segments > 20 — using simple concat (no xfade)")
            if assembler:
                return assembler.concat(segments, output_path, re_encode=True)
            return self._simple_concat(segments, output_path)

        # Build FFmpeg xfade filter chain
        out = Path(output_path)
        tmp = out.with_suffix(".partial.mp4")

        # Probe all durations needed for offset calculation
        durations = [self._probe_duration(s) or 5.0 for s in segments]

        # Build complex filter
        # Input streams labeled [0:v][1:v]...[N:v] and [0:a][1:a]...
        inputs = []
        for seg in segments:
            inputs.extend(["-i", seg])

        # Chain xfade filters:
        # [0:v][1:v]xfade=...,offset=d0-t[v01]; [v01][2:v]xfade=...,offset=d0+d1-2t[v012]; ...
        vfilter_parts = []
        afilter_parts = []
        current_duration = durations[0]

        prev_v = "[0:v]"
        prev_a = "[0:a]"

        for i in range(1, len(segments)):
            offset = current_duration - transition_duration
            next_v = f"[v{i}]" if i < len(segments) - 1 else "[vout]"
            next_a = f"[a{i}]" if i < len(segments) - 1 else "[aout]"

            vfilter_parts.append(
                f"{prev_v}[{i}:v]xfade=transition={transition_type}"
                f":duration={transition_duration}:offset={offset:.3f}{next_v}"
            )
            afilter_parts.append(
                f"{prev_a}[{i}:a]acrossfade=d={transition_duration}{next_a}"
            )

            prev_v = next_v
            prev_a = next_a
            current_duration += durations[i] - transition_duration

        filter_complex = ";".join(vfilter_parts + afilter_parts)

        cmd = (
            ["ffmpeg", "-y"]
            + inputs
            + [
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "[aout]",
                "-c:v", "libx264",
                "-preset", "slow",
                "-crf", "17",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "256k",
                "-movflags", "+faststart",
                str(tmp),
            ]
        )

        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=900)
            if result.returncode != 0:
                logger.warning(
                    f"⚠️ Crossfade concat failed, falling back to simple concat:\n"
                    f"{result.stderr[-300:]}"
                )
                tmp.unlink(missing_ok=True)
                return self._simple_concat(segments, output_path)
            tmp.replace(out)
            logger.info(
                f"✅ Crossfade concat: {len(segments)} segments → {out.name} "
                f"({transition_type}, {transition_duration}s)"
            )
            return str(out)
        except sp.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            logger.warning("⚠️ Crossfade timeout — falling back to simple concat")
            return self._simple_concat(segments, output_path)

    def apply_color_grade(
        self,
        video_path: str,
        output_path: str,
        *,
        warmth: float = 1.05,
        saturation: float = 1.08,
        brightness: float = 1.02,
    ) -> str:
        """
        Apply a subtle warm color grade to the final video.
        Default values give a slight warmth/saturation boost for children's content.
        """
        out = Path(output_path)
        tmp = out.with_suffix(".partial.mp4")

        # eq filter: brightness/contrast/saturation
        # colorchannelmixer: subtle warm tint (slight red/green boost)
        vf = (
            f"eq=brightness={brightness - 1:.3f}:saturation={saturation:.2f},"
            f"colorchannelmixer=rr={warmth:.3f}:gg=1.0:bb={2.0 - warmth:.3f}"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "17",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(tmp),
        ]

        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                logger.warning("⚠️ Color grade failed — skipping")
                return video_path
            tmp.replace(out)
            logger.info(f"✅ Color grade applied: warmth={warmth}, sat={saturation}")
            return str(out)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            logger.warning(f"⚠️ Color grade error: {e}")
            return video_path

    # ─── Helpers ─────────────────────────────────────────────────
    @staticmethod
    def _probe_duration(path: str) -> Optional[float]:
        """Get video duration via ffprobe."""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception:
            pass
        return None

    @staticmethod
    def _simple_concat(segments: List[str], output_path: str) -> str:
        """Simple FFmpeg concat demuxer (no re-encode)."""
        out = Path(output_path)
        tmp = out.with_suffix(".partial.mp4")
        list_file = out.with_suffix(".list.txt")

        try:
            list_content = "\n".join(
                f"file '{Path(s).absolute()}'" for s in segments
            )
            list_file.write_text(list_content, encoding="utf-8")
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(tmp),
            ]
            result = sp.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                tmp.replace(out)
                return str(out)
            raise RuntimeError(result.stderr[-200:])
        finally:
            list_file.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
