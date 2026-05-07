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
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from infrastructure.bgm_director import VolumeSegment

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

    def apply_bgm_with_curve(
        self,
        video_path: str,
        output_path: str,
        volume_curve: List["VolumeSegment"],
        bgm_path: Optional[str] = None,
    ) -> str:
        """v22.2: Mix BGM with per-scene volume automation curve.

        Where apply_bgm() uses a single fixed volume, this method uses a
        piecewise constant curve that ducks during Quran recitation, raises
        during hooks, etc.

        Args:
            video_path: input video (already mixed with narrator audio)
            output_path: where to write the BGM-mixed video
            volume_curve: list of VolumeSegment from BGMDirector
            bgm_path: BGM source file (defaults to configured BGM)

        Returns:
            output_path on success, video_path on graceful skip.
        """
        if not volume_curve:
            logger.info("ℹ️ Empty volume curve — falling back to fixed BGM")
            return self.apply_bgm(video_path, output_path, bgm_path)

        bgm = bgm_path or str(self._paths.bgm_file)
        if not Path(bgm).exists():
            logger.warning(
                f"⚠️ BGM file not found: {bgm} — skipping BGM mix"
            )
            return video_path

        vid = Path(video_path)
        if not vid.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        out = Path(output_path)
        tmp = out.with_suffix(".partial.mp4")

        duration = self._probe_duration(str(vid))
        if duration is None:
            logger.warning("⚠️ Could not probe — falling back to fixed BGM")
            return self.apply_bgm(video_path, output_path, bgm_path)

        fade_out_start = max(
            duration - self._fade_out_sec, self._fade_in_sec + 1.0,
        )

        # Build volume expression (piecewise constant)
        # if(lt(t,end1),v1,if(lt(t,end2),v2,...vN))
        if len(volume_curve) == 1:
            vol_expr = f"{volume_curve[0].volume:.4f}"
        else:
            vol_expr = f"{volume_curve[-1].volume:.4f}"
            for seg in reversed(volume_curve[:-1]):
                vol_expr = (
                    f"if(lt(t\\,{seg.end_sec:.3f})\\,"
                    f"{seg.volume:.4f}\\,{vol_expr})"
                )

        # Filter chain: fade in/out on BGM + apply curve via volume=
        bgm_filter = (
            f"[1:a]"
            f"aloop=loop=-1:size=2e+09,"
            f"atrim=0:{duration:.3f},"
            f"afade=t=in:st=0:d={self._fade_in_sec:.1f},"
            f"afade=t=out:st={fade_out_start:.3f}:d={self._fade_out_sec:.1f},"
            f"volume='{vol_expr}':eval=frame"
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
            "-b:a", "192k",
            "-shortest",
            str(tmp),
        ]

        try:
            import subprocess
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                logger.error(
                    f"❌ BGM curve mix failed (rc={result.returncode}):\n"
                    f"   stderr: {result.stderr[-500:]}\n"
                    f"   Falling back to fixed BGM"
                )
                tmp.unlink(missing_ok=True)
                return self.apply_bgm(video_path, output_path, bgm_path)

            tmp.replace(out)
            avg_vol = (
                sum(s.volume * s.duration for s in volume_curve)
                / sum(s.duration for s in volume_curve)
            )
            logger.info(
                f"✅ BGM with curve: {out.name} "
                f"(avg vol={avg_vol:.3f}, "
                f"{len(volume_curve)} segments)"
            )
            return str(out)
        except Exception as e:
            logger.error(f"❌ BGM curve mix error: {e}")
            tmp.unlink(missing_ok=True)
            return self.apply_bgm(video_path, output_path, bgm_path)

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
        per_transition_durations: Optional[List[float]] = None,
        per_transition_types: Optional[List[str]] = None,
    ) -> str:
        """
        Concatenate video segments with smooth crossfade transitions.

        For episode with N segments, applies (N-1) xfade transitions.
        Falls back to simple concat if >20 segments (performance).

        v22.2 — per-transition control:
            If `per_transition_durations` is provided, each i-th transition
            uses durations[i] instead of the scalar fallback. Same for types.
            Backward compatible — pass nothing for fixed transitions.

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

        # v22.2: Resolve per-transition data (fall back to scalars)
        n_transitions = len(segments) - 1
        if per_transition_durations is not None:
            if len(per_transition_durations) != n_transitions:
                logger.warning(
                    f"⚠️ per_transition_durations length "
                    f"{len(per_transition_durations)} != {n_transitions}. "
                    f"Falling back to scalar."
                )
                durations_list = [transition_duration] * n_transitions
            else:
                durations_list = list(per_transition_durations)
        else:
            durations_list = [transition_duration] * n_transitions

        if per_transition_types is not None:
            if len(per_transition_types) != n_transitions:
                logger.warning(
                    f"⚠️ per_transition_types length mismatch — "
                    f"using scalar"
                )
                types_list = [transition_type] * n_transitions
            else:
                types_list = list(per_transition_types)
        else:
            types_list = [transition_type] * n_transitions

        # Map our internal types to ffmpeg xfade types
        TYPE_MAP = {
            "cut": "fade",          # Use fast fade as approximation
            "dip": "fadeblack",     # Quick dip to black
            "fade": "fade",
            "dissolve": "fade",     # ffmpeg's smooth fade
            "sacred": "fade",       # Same filter, longer duration carries the feel
        }
        ffmpeg_types = [TYPE_MAP.get(t, "fade") for t in types_list]

        # Build FFmpeg xfade filter chain
        out = Path(output_path)
        tmp = out.with_suffix(".partial.mp4")

        # Probe all durations needed for offset calculation
        seg_durations = [self._probe_duration(s) or 5.0 for s in segments]

        inputs = []
        for seg in segments:
            inputs.extend(["-i", seg])

        # Chain xfade filters with per-transition durations
        vfilter_parts = []
        afilter_parts = []
        current_duration = seg_durations[0]

        prev_v = "[0:v]"
        prev_a = "[0:a]"

        for i in range(1, len(segments)):
            t_dur = durations_list[i - 1]
            t_type = ffmpeg_types[i - 1]
            # Cut transitions (very short) need a minimum duration
            t_dur = max(0.05, t_dur)

            offset = current_duration - t_dur
            next_v = f"[v{i}]" if i < len(segments) - 1 else "[vout]"
            next_a = f"[a{i}]" if i < len(segments) - 1 else "[aout]"

            vfilter_parts.append(
                f"{prev_v}[{i}:v]xfade=transition={t_type}"
                f":duration={t_dur}:offset={offset:.3f}{next_v}"
            )
            afilter_parts.append(
                f"{prev_a}[{i}:a]acrossfade=d={t_dur}{next_a}"
            )

            prev_v = next_v
            prev_a = next_a
            current_duration += seg_durations[i] - t_dur

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
