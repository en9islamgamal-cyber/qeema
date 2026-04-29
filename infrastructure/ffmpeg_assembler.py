"""
infrastructure/ffmpeg_assembler.py — VALUE / QEEMA v11.0 (Production)
=========================================================================
FFmpeg wrapper for video operations.

[Operations]
- get_duration   : ffprobe-based metadata read
- encode_segment : raw webm + audio → mp4 (single re-encode, GPU-friendly)
- concat         : stream-copy first, fallback to re-encode

[Optimization]
The original code was double-encoding: each scene was encoded with
preset=medium, then concat re-encoded with preset=slow. That doubles
CPU. Now we:
1. Encode each scene once (preset=medium)
2. Concat with stream-copy (no re-encode) when codecs match
3. Only fall back to re-encode if stream-copy fails
"""
from __future__ import annotations

import logging
import subprocess as sp
import uuid
from pathlib import Path
from typing import List

from core.config import VideoConfig
from core.exceptions import VideoAssemblyError
from core.interfaces import VideoAssembler
from infrastructure.audio_utils import get_audio_duration

logger = logging.getLogger(__name__)


class FFmpegAssembler(VideoAssembler):
    """Production FFmpeg wrapper."""

    def __init__(self, config: VideoConfig) -> None:
        self.cfg: VideoConfig = config

    # ───────────────────────────────────────────────────────────
    # Probing
    # ───────────────────────────────────────────────────────────
    def get_duration(self, path: str) -> float:
        return get_audio_duration(path)

    # ───────────────────────────────────────────────────────────
    # Per-segment encode
    # ───────────────────────────────────────────────────────────
    def encode_segment(
        self,
        webm_input: str,
        audio_input: str,
        output_path: str,
        max_duration: float,
    ) -> None:
        """
        Encode a single scene segment from raw webm + audio.

        [Decisions]
        - preset=medium: balance speed vs quality (final concat avoids re-encode)
        - shortest: clip to whichever stream ends first (audio < video by design)
        - -t bound: hard cap on duration
        """
        if max_duration <= 0:
            raise VideoAssemblyError(f"Invalid duration: {max_duration}")

        cmd = [
            "ffmpeg", "-y",
            "-i", webm_input,
            "-i", audio_input,
            "-c:v", self.cfg.codec,
            "-profile:v", self.cfg.profile,
            "-preset", "medium",
            "-crf", str(self.cfg.crf),
            "-pix_fmt", self.cfg.pix_fmt,
            "-r", str(self.cfg.fps),
            "-c:a", self.cfg.audio_codec,
            "-b:a", self.cfg.audio_bitrate,
            "-t", f"{max_duration:.3f}",
            "-shortest",
            output_path,
        ]
        try:
            result = sp.run(cmd, capture_output=True, text=True, timeout=300)
        except sp.TimeoutExpired as e:
            raise VideoAssemblyError(
                f"FFmpeg encode timeout: {output_path}", cause=e
            ) from e

        if result.returncode != 0:
            raise VideoAssemblyError(
                f"FFmpeg encode failed: {result.stderr[-500:]}",
                context={"output": output_path},
            )

    # ───────────────────────────────────────────────────────────
    # Concat
    # ───────────────────────────────────────────────────────────
    def concat(
        self,
        segments: List[str],
        output_path: str,
        *,
        re_encode: bool = False,
    ) -> str:
        if not segments:
            raise VideoAssemblyError("Empty segments list")

        # Validate inputs
        missing = [s for s in segments if not Path(s).exists()]
        if missing:
            raise VideoAssemblyError(
                f"Missing segment files: {missing[:3]}"
            )

        # Build concat list file
        list_file = (
            Path(output_path).parent / f"_concat_{uuid.uuid4().hex[:8]}.txt"
        )
        try:
            with open(list_file, "w", encoding="utf-8") as f:
                for s in segments:
                    f.write(f"file '{Path(s).absolute()}'\n")

            if re_encode:
                cmd = self._concat_reencode_cmd(list_file, output_path)
            else:
                cmd = self._concat_streamcopy_cmd(list_file, output_path)

            try:
                result = sp.run(cmd, capture_output=True, text=True, timeout=900)
            except sp.TimeoutExpired as e:
                raise VideoAssemblyError(
                    "FFmpeg concat timeout", cause=e
                ) from e

            if result.returncode != 0:
                # Stream-copy can fail if codecs differ; fall back to re-encode
                if not re_encode:
                    logger.warning(
                        "⚠️ Stream-copy concat failed; retrying with re-encode"
                    )
                    return self.concat(segments, output_path, re_encode=True)
                raise VideoAssemblyError(
                    f"FFmpeg concat failed: {result.stderr[-500:]}"
                )
            return output_path
        finally:
            try:
                list_file.unlink()
            except OSError:
                pass

    # ───────────────────────────────────────────────────────────
    # Command builders
    # ───────────────────────────────────────────────────────────
    def _concat_streamcopy_cmd(
        self, list_file: Path, output: str,
    ) -> List[str]:
        return [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            "-movflags", "+faststart",
            output,
        ]

    def _concat_reencode_cmd(
        self, list_file: Path, output: str,
    ) -> List[str]:
        return [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", self.cfg.codec,
            "-profile:v", self.cfg.profile,
            "-crf", str(self.cfg.crf),
            "-preset", self.cfg.preset,
            "-pix_fmt", self.cfg.pix_fmt,
            "-r", str(self.cfg.fps),
            "-c:a", self.cfg.audio_codec,
            "-b:a", self.cfg.audio_bitrate,
            "-movflags", "+faststart",
            output,
        ]
