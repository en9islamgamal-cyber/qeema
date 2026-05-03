"""
infrastructure/ffmpeg_assembler.py — Production video assembler
========================================================================
Refactored to delegate argv construction to core.ffmpeg_args and process
management to ffmpeg_pro.run_ffmpeg. Public surface (encode_segment,
concat) is unchanged from v11 — this is a drop-in replacement.

What this fixes vs v11
----------------------
- The 256kk class of bugs is structurally impossible (Bitrate validates
  on construction).
- Timeouts scale with input duration instead of being hard-coded.
- ffmpeg processes are killed via process group on timeout (no zombie
  filter children).
- Output is validated with ffprobe instead of size > 1000 heuristic.
- Atomic writes throughout: encode to .partial, replace at the end.
- All temp files cleaned on both success and failure paths.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from core.config import VideoConfig
from core.exceptions import VideoAssemblyError
from core.ffmpeg_args import (
    AudioEncoderSpec,
    Bitrate,
    ConcatReencodeArgs,
    ConcatStreamCopyArgs,
    CRF,
    Duration,
    EncodeSegmentArgs,
    Framerate,
    Resolution,
    VideoEncoderSpec,
    write_concat_list,
)
from core.interfaces import VideoAssembler
from infrastructure.ffmpeg_pro import (
    FFmpegProgress,
    TimeoutPolicy,
    probe_duration,
    run_ffmpeg,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Config translation
# ════════════════════════════════════════════════════════════════
def _video_spec_from_config(cfg: VideoConfig) -> VideoEncoderSpec:
    """Translate project VideoConfig → typed encoder spec."""
    return VideoEncoderSpec(
        codec=getattr(cfg, "codec", "libx264"),
        preset=cfg.preset,
        crf=CRF(int(cfg.crf)),
        pix_fmt=getattr(cfg, "pix_fmt", "yuv420p"),
        profile=getattr(cfg, "profile", None),
    )


def _audio_spec_from_config(cfg: VideoConfig) -> AudioEncoderSpec:
    """
    Translate VideoConfig.audio_bitrate into a validated AudioEncoderSpec.

    THIS is the exact site of the historic 256kk bug. Bitrate(...)
    validates the format at construction time. The bug cannot reach
    production again without first failing this constructor.
    """
    return AudioEncoderSpec(
        codec=getattr(cfg, "audio_codec", "aac"),
        bitrate=Bitrate(cfg.audio_bitrate),
        sample_rate_hz=getattr(cfg, "audio_sample_rate", 44100),
    )


# ════════════════════════════════════════════════════════════════
# Assembler
# ════════════════════════════════════════════════════════════════
class FFmpegAssembler(VideoAssembler):
    """
    FFmpeg-based video assembler.

    Invariants
    ----------
    - encode_segment is deterministic for a given input + ffmpeg version.
    - Concat tries stream-copy first (fast); falls back to re-encode on
      codec mismatch.
    - All temp files are cleaned up; output paths are written atomically.
    - All ffmpeg invocations use kill-tree on timeout.
    """

    def __init__(
        self,
        video_cfg: VideoConfig,
        *,
        timeout_policy: Optional[TimeoutPolicy] = None,
    ) -> None:
        self._cfg: VideoConfig = video_cfg
        self._timeout: TimeoutPolicy = timeout_policy or TimeoutPolicy()

        # Eager validation: catch a misconfigured audio_bitrate at startup
        # rather than three minutes into rendering.
        Bitrate(video_cfg.audio_bitrate)

    # ───────────────────────────────────────────────────────────
    # get_duration: required by VideoAssembler interface
    # ───────────────────────────────────────────────────────────
    def get_duration(self, path: str) -> float:
        """
        Return duration of an audio/video file in seconds.

        Delegates to probe_duration (ffprobe-based). Raises
        VideoAssemblyError if the file does not exist or ffprobe fails.
        """
        p = Path(path)
        if not p.exists():
            raise VideoAssemblyError(f"get_duration: file not found: {path}")
        duration = probe_duration(p)
        if duration is None:
            raise VideoAssemblyError(
                f"get_duration: ffprobe could not read duration for: {path}"
            )
        return duration

    # ───────────────────────────────────────────────────────────
    # encode_segment: webm + audio → mp4 (single pass)
    # ───────────────────────────────────────────────────────────
    def encode_segment(
        self,
        webm_input: str,
        audio_input: str,
        output_path: str,
        max_duration: Optional[float] = None,
    ) -> str:
        webm_path = Path(webm_input)
        audio_path = Path(audio_input)
        out_path = Path(output_path)

        if not webm_path.exists():
            raise VideoAssemblyError(f"webm not found: {webm_input}")
        if not audio_path.exists():
            raise VideoAssemblyError(f"audio not found: {audio_input}")

        # Atomic write: encode to .partial, rename on success.
        tmp_out = out_path.with_suffix(out_path.suffix + ".partial")

        # Probe audio for timeout scaling. Fall back to max_duration if
        # ffprobe is unavailable.
        input_duration = probe_duration(audio_path) or max_duration

        args = EncodeSegmentArgs(
            video_input=webm_path,
            audio_input=audio_path,
            output=tmp_out,
            resolution=Resolution(self._cfg.width, self._cfg.height),
            framerate=Framerate(self._cfg.fps),
            video=_video_spec_from_config(self._cfg),
            audio=_audio_spec_from_config(self._cfg),
            max_duration=Duration(max_duration) if max_duration else None,
        )

        def _on_progress(p: FFmpegProgress) -> None:
            if p.finished or input_duration is None:
                return
            pct = p.percent(input_duration)
            if pct is not None:
                logger.debug(
                    f"encode {out_path.name}: {pct:.0f}% speed={p.speed:.2f}x"
                )

        try:
            run_ffmpeg(
                args.to_argv(),
                expected_output=tmp_out,
                input_duration_sec=input_duration,
                timeout_policy=self._timeout,
                progress_cb=_on_progress,
            )
            tmp_out.replace(out_path)
        except Exception:
            tmp_out.unlink(missing_ok=True)
            raise

        logger.info(f"✅ encoded: {out_path.name}")
        return str(out_path)

    # ───────────────────────────────────────────────────────────
    # concat: multiple mp4 → single mp4
    # ───────────────────────────────────────────────────────────
    def concat(
        self,
        input_paths: List[str],
        output_path: str,
        re_encode: bool = False,
    ) -> str:
        if not input_paths:
            raise VideoAssemblyError("concat called with empty input list")

        for p in input_paths:
            if not Path(p).exists():
                raise VideoAssemblyError(f"concat input missing: {p}")

        if re_encode:
            return self._concat_reencode(input_paths, output_path)

        try:
            return self._concat_streamcopy(input_paths, output_path)
        except VideoAssemblyError as e:
            logger.warning(
                f"⚠️ stream-copy failed ({e}); falling back to re-encode"
            )
            return self._concat_reencode(input_paths, output_path)

    # ───────────────────────────────────────────────────────────
    # Internal: stream-copy (fast)
    # ───────────────────────────────────────────────────────────
    def _concat_streamcopy(self, inputs: List[str], output: str) -> str:
        out_path = Path(output)
        tmp_out = out_path.with_suffix(out_path.suffix + ".partial")
        list_file = out_path.with_suffix(".concat-list.txt")

        try:
            write_concat_list([Path(p) for p in inputs], list_file)
            args = ConcatStreamCopyArgs(list_file=list_file, output=tmp_out)

            total_duration = sum(
                (probe_duration(Path(p)) or 30.0) for p in inputs
            )

            run_ffmpeg(
                args.to_argv(),
                expected_output=tmp_out,
                input_duration_sec=total_duration,
                timeout_policy=self._timeout,
            )
            tmp_out.replace(out_path)
        except Exception:
            tmp_out.unlink(missing_ok=True)
            raise
        finally:
            list_file.unlink(missing_ok=True)

        logger.info(f"✅ concat (stream-copy): {len(inputs)} → {out_path.name}")
        return str(out_path)

    # ───────────────────────────────────────────────────────────
    # Internal: re-encode (always works)
    # ───────────────────────────────────────────────────────────
    def _concat_reencode(self, inputs: List[str], output: str) -> str:
        out_path = Path(output)
        tmp_out = out_path.with_suffix(out_path.suffix + ".partial")
        list_file = out_path.with_suffix(".concat-list.txt")

        try:
            write_concat_list([Path(p) for p in inputs], list_file)
            args = ConcatReencodeArgs(
                list_file=list_file,
                output=tmp_out,
                video=_video_spec_from_config(self._cfg),
                audio=_audio_spec_from_config(self._cfg),
            )

            total_duration = sum(
                (probe_duration(Path(p)) or 30.0) for p in inputs
            )

            run_ffmpeg(
                args.to_argv(),
                expected_output=tmp_out,
                input_duration_sec=total_duration,
                timeout_policy=self._timeout,
            )
            tmp_out.replace(out_path)
        except Exception:
            tmp_out.unlink(missing_ok=True)
            raise
        finally:
            list_file.unlink(missing_ok=True)

        logger.info(f"✅ concat (re-encode): {len(inputs)} → {out_path.name}")
        return str(out_path)
