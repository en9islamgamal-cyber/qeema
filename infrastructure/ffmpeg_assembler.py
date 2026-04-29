"""
infrastructure/ffmpeg_assembler.py — VALUE / QEEMA v11.1 (Fixed & Enhanced)
=======================================================================
FFmpeg video assembly: encode segments + concat + duration detection.

[Fix]
- Implemented missing abstract method: get_duration()

[Enhancements]
- Robust ffprobe handling
- Better logging
- Safer subprocess execution
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


class FFmpegAssembler(VideoAssembler):

    def __init__(self, video_cfg: VideoConfig) -> None:
        self._cfg: VideoConfig = video_cfg

    # ───────────────────────────────────────────────────────────
    # ✅ FIX: get_duration (CRITICAL)
    # ───────────────────────────────────────────────────────────
    def get_duration(self, media_path: str) -> float:
        """
        Get media duration using ffprobe.
        Required by abstract base class.
        """
        if not Path(media_path).exists():
            raise VideoAssemblyError(f"file not found: {media_path}")

        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_path,
        ]

        try:
            result = sp.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20
            )

            if result.returncode != 0:
                raise VideoAssemblyError(
                    f"ffprobe failed: {result.stderr[-200:]}"
                )

            duration = float(result.stdout.strip())

            if duration <= 0:
                raise VideoAssemblyError("invalid duration detected")

            return duration

        except FileNotFoundError:
            raise VideoAssemblyError(
                "ffprobe not installed on system"
            )

        except Exception as e:
            raise VideoAssemblyError(
                f"failed to get duration: {str(e)}"
            )

    # ───────────────────────────────────────────────────────────
    # encode_segment
    # ───────────────────────────────────────────────────────────
    def encode_segment(
        self,
        webm_input: str,
        audio_input: str,
        output_path: str,
        max_duration: Optional[float] = None,
    ) -> str:

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

        result = sp.run(cmd, capture_output=True, text=True, timeout=180)

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
    # concat
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
            logger.warning(f"⚠️ stream-copy failed ({e}); fallback to re-encode")
            return self._concat_reencode(input_paths, output_path)

    # ───────────────────────────────────────────────────────────
    def _concat_streamcopy(self, inputs: List[str], output: str) -> str:

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
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
    def _concat_reencode(self, inputs: List[str], output: str) -> str:

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
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
