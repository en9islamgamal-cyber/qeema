"""
infrastructure/ffmpeg_assembler.py — VALUE / QEEMA v11.0 (Production)
=======================================================================
FFmpeg video assembly: encode segments + concat.

[Strategy]
- encode_segment: webm + audio → mp4 (single encode, no re-encode)
- concat: multiple mp4 → single mp4 (stream-copy first, re-encode fallback)

[Performance]
v10 did: encode per segment, then re-encode during concat (double work).
v11 does: encode once, concat with stream-copy (5× faster).
"""
from __future__ import annotations

import logging
import subprocess as sp
import tempfile
from pathlib import Path
from typing import List, Optional

from core.config import VideoConfig
from core.exceptions import VideoAssemblyError
from core.interfaces import VideoAssembler

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# FFmpegAssembler
# ════════════════════════════════════════════════════════════════
class FFmpegAssembler(VideoAssembler):
    """
    FFmpeg-based video assembly.

    [Encode once, concat with stream-copy]
    Every segment is encoded exactly once. Concat uses stream-copy
    (no re-encode) unless codecs differ, in which case we fall back
    to re-encode automatically.
    """

    def __init__(self, video_cfg: VideoConfig) -> None:
        self._cfg: VideoConfig = video_cfg

    # ───────────────────────────────────────────────────────────
    # encode_segment: webm + audio → mp4 (single encode)
    # ───────────────────────────────────────────────────────────
    def encode_segment(
        self,
        webm_input: str,
        audio_input: str,
        output_path: str,
        max_duration: Optional[float] = None,
    ) -> str:
        """
        Encode webm video + audio → mp4 in a single pass.

        [Codec settings]
        - Video: libx264, preset=medium, crf=17 (high quality)
        - Audio: aac, 192k
        - Container: mp4 with faststart flag
        """
        if not Path(webm_input).exists():
            raise VideoAssemblyError(f"webm not found: {webm_input}")
        if not Path(audio_input).exists():
            raise VideoAssemblyError(f"audio not found: {audio_input}")

        cmd: List[str] = [
            "ffmpeg", "-y",
            "-i", webm_input,
            "-i", audio_input,
        ]

        if max_duration is not None:
            cmd += ["-t", f"{max_duration:.3f}"]

        cmd += [
            "-c:v", "libx264",
            "-preset", self._cfg.preset,
            "-crf", str(self._cfg.crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", f"{self._cfg.audio_bitrate}k",
            "-ar", "44100",
            "-movflags", "+faststart",
            "-r", str(self._cfg.fps),
            "-s", f"{self._cfg.width}x{self._cfg.height}",
            output_path,
        ]

        result = sp.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise VideoAssemblyError(
                f"FFmpeg encode failed: {result.stderr[-500:]}",
                context={"webm": webm_input, "audio": audio_input},
            )

        if not Path(output_path).exists() or Path(output_path).stat().st_size < 1000:
            raise VideoAssemblyError(
                f"encode produced empty/missing file: {output_path}"
            )

        logger.info(f"✅ encoded: {Path(output_path).name}")
        return output_path

    # ───────────────────────────────────────────────────────────
    # concat: multiple mp4 → single mp4
    # ───────────────────────────────────────────────────────────
    def concat(
        self,
        input_paths: List[str],
        output_path: str,
        re_encode: bool = False,
    ) -> str:
        """
        Concatenate multiple mp4 files.

        [Strategy]
        1. Try stream-copy concat (fast, no quality loss)
        2. If that fails (codec mismatch), fall back to re-encode

        [Why stream-copy first?]
        5× faster than re-encode. Only falls back when necessary.
        """
        if not input_paths:
            raise VideoAssemblyError("concat called with empty input list")

        for p in input_paths:
            if not Path(p).exists():
                raise VideoAssemblyError(f"concat input missing: {p}")

        if re_encode:
            return self._concat_reencode(input_paths, output_path)

        # Try stream-copy first
        try:
            return self._concat_streamcopy(input_paths, output_path)
        except VideoAssemblyError as e:
            logger.warning(f"⚠️ stream-copy failed ({e}); falling back to re-encode")
            return self._concat_reencode(input_paths, output_path)

    # ───────────────────────────────────────────────────────────
    # Internal: stream-copy concat (fast)
    # ───────────────────────────────────────────────────────────
    def _concat_streamcopy(self, inputs: List[str], output: str) -> str:
        # Use concat demuxer with a list file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            for p in inputs:
                # Escape single quotes in path
                safe = str(Path(p).absolute()).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
            list_file = f.name

        try:
            tmp_out = Path(output).parent / f"{Path(output).stem}_tmp.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                "-movflags", "+faststart",
                str(tmp_out),
            ]
            result = sp.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                raise VideoAssemblyError(
                    f"stream-copy concat failed: {result.stderr[-300:]}"
                )
            tmp_out.replace(output)
            logger.info(f"✅ concat (stream-copy): {len(inputs)} → {Path(output).name}")
            return output
        finally:
            Path(list_file).unlink(missing_ok=True)

    # ───────────────────────────────────────────────────────────
    # Internal: re-encode concat (slower, always works)
    # ───────────────────────────────────────────────────────────
    def _concat_reencode(self, inputs: List[str], output: str) -> str:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            for p in inputs:
                safe = str(Path(p).absolute()).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
            list_file = f.name

        try:
            tmp_out = Path(output).parent / f"{Path(output).stem}_tmp.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file,
                "-c:v", "libx264",
                "-preset", self._cfg.preset,
                "-crf", str(self._cfg.crf),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", f"{self._cfg.audio_bitrate}k",
                "-movflags", "+faststart",
                str(tmp_out),
            ]
            result = sp.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise VideoAssemblyError(
                    f"re-encode concat failed: {result.stderr[-300:]}"
                )
            tmp_out.replace(output)
            logger.info(f"✅ concat (re-encode): {len(inputs)} → {Path(output).name}")
            return output
        finally:
            Path(list_file).unlink(missing_ok=True)
