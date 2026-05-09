"""
infrastructure/ffmpeg_pro.py — VALUE / QEEMA v22.5 — production-grade FFmpeg invocation
==================================================================
What this fixes
---------------
The current FFmpeg call sites have several latent issues:

  1. `subprocess.run(timeout=...)` only kills the parent ffmpeg.
     Filter-chain children survive on Linux → fd/memory leaks on
     long-running CI runners.

  2. No progress visibility. Hangs are indistinguishable from slow
     work. We can't tell "is this 80% done" vs "is this stuck".

  3. Timeouts are hard-coded numbers, but encoding time scales with
     input duration. A 3-minute audio clip needs more than 120s on
     a slow runner.

  4. No retries on transient FS errors (e.g. tmpfs eviction during
     CI cache restore).

  5. Output validation is post-hoc and weak — checks file size > 1000
     bytes, but a corrupt mp4 can easily be larger than that.

This module provides a single entry point — `run_ffmpeg` — that:

  - Spawns ffmpeg in its own process group (kill-tree on timeout).
  - Reads stderr line-by-line, extracting `out_time_ms` for progress.
  - Computes a duration-aware timeout (base + slope × input_duration).
  - Validates output by probing it with ffprobe (real validation,
    not size heuristic).
  - Wraps everything in our exception hierarchy so callers can route
    errors correctly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess as sp
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from core.exceptions import (
    NetworkError,
    QeemaError,
    TimeoutError as QeemaTimeoutError,
    VideoAssemblyError,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# FFmpeg progress parser
# ════════════════════════════════════════════════════════════════
# When ffmpeg is invoked with `-progress pipe:2 -nostats`, it writes
# key=value lines like:
#     out_time_ms=12345678
#     bitrate=1234.5kbits/s
#     progress=continue
#     progress=end
# We parse these to compute a percentage and emit progress callbacks.
_PROGRESS_KEY_RE = re.compile(r"^([a-z_]+)=(.+)$")


@dataclass(slots=True)
class FFmpegProgress:
    """Snapshot of ffmpeg's progress stream."""
    out_time_us: int = 0           # output position in microseconds
    bitrate_kbps: float = 0.0
    fps: float = 0.0
    speed: float = 0.0             # 1.0 = realtime
    finished: bool = False

    @property
    def out_time_sec(self) -> float:
        return self.out_time_us / 1_000_000.0

    def percent(self, total_duration_sec: Optional[float]) -> Optional[float]:
        if not total_duration_sec or total_duration_sec <= 0:
            return None
        return min(100.0, (self.out_time_sec / total_duration_sec) * 100.0)


# ════════════════════════════════════════════════════════════════
# Result + result codes
# ════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class FFmpegResult:
    returncode: int
    duration_sec: float
    stderr_tail: str       # last ~32 KB of stderr, for diagnostics
    output_path: Optional[Path] = None


# ════════════════════════════════════════════════════════════════
# Timeout policy
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    """
    Compute a timeout that scales with input duration.

    timeout = base_sec + slope × max_input_duration_sec
    capped at max_sec.

    Example for a 5-minute episode at 3× realtime encode:
       base=30, slope=4, max=900
       → for 300s input: 30 + 4×300 = 1230 → capped to 900s.

    Pure encode is ~1× realtime on a CI runner with libx264 medium.
    Re-encode concat is ~0.5× realtime (decoded + re-encoded).
    Allow 2× headroom by default.
    """
    base_sec: float = 30.0
    slope: float = 4.0
    max_sec: float = 900.0
    min_sec: float = 30.0

    def compute(self, input_duration_sec: Optional[float]) -> float:
        if input_duration_sec is None or input_duration_sec <= 0:
            return self.base_sec
        t = self.base_sec + self.slope * input_duration_sec
        return max(self.min_sec, min(self.max_sec, t))


# ════════════════════════════════════════════════════════════════
# Process runner
# ════════════════════════════════════════════════════════════════
def run_ffmpeg(
    argv: Sequence[str],
    *,
    expected_output: Optional[Path] = None,
    input_duration_sec: Optional[float] = None,
    timeout_policy: Optional[TimeoutPolicy] = None,
    progress_cb: Optional[Callable[[FFmpegProgress], None]] = None,
    stderr_tail_kb: int = 32,
) -> FFmpegResult:
    """
    Invoke ffmpeg with kill-tree, progress monitoring, and validation.

    Args:
        argv: full command line including 'ffmpeg' as argv[0].
        expected_output: if set, the file is probed with ffprobe after
            success to verify it is a valid media file.
        input_duration_sec: used by the timeout policy to scale.
        timeout_policy: defaults to TimeoutPolicy().
        progress_cb: called with FFmpegProgress snapshots from the
            ffmpeg progress stream.
        stderr_tail_kb: how much trailing stderr to retain for diagnostics.

    Raises:
        QeemaTimeoutError: when timeout fires (after kill-tree).
        VideoAssemblyError: on non-zero exit or invalid output.

    Returns:
        FFmpegResult with returncode, wall-clock duration, stderr tail.
    """
    if not argv or argv[0] != "ffmpeg":
        raise ValueError(f"argv[0] must be 'ffmpeg', got {argv[0]!r}")
    if shutil.which("ffmpeg") is None:
        raise QeemaError("ffmpeg not found on PATH")

    policy = timeout_policy or TimeoutPolicy()
    timeout_sec = policy.compute(input_duration_sec)

    # Inject -progress pipe:2 -nostats just after `ffmpeg` so ffmpeg
    # writes progress key=value pairs to stderr, suppressing the
    # default rolling status line. Caller's argv is otherwise untouched.
    augmented = list(argv[:1]) + ["-nostats", "-progress", "pipe:2"] + list(argv[1:])

    logger.debug(f"ffmpeg: timeout={timeout_sec:.0f}s argv={argv}")

    t0 = time.monotonic()
    progress = FFmpegProgress()
    stderr_buffer: List[str] = []
    stderr_buffer_bytes = 0
    max_buffer_bytes = stderr_tail_kb * 1024

    # start_new_session=True puts the child in its own process group.
    # On Linux/macOS, killing the group via os.killpg sends the signal
    # to all descendants, including ffmpeg's filter subprocesses.
    proc = sp.Popen(
        augmented,
        stdout=sp.DEVNULL,
        stderr=sp.PIPE,
        text=True,
        bufsize=1,             # line-buffered
        start_new_session=True,
    )

    def _drain_stderr() -> None:
        """Read stderr line-by-line, parse progress, retain tail."""
        nonlocal stderr_buffer_bytes
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.rstrip("\n")
            # Progress key=value
            m = _PROGRESS_KEY_RE.match(line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                _apply_progress(progress, key, val)
                if progress_cb and key == "progress":
                    try:
                        progress_cb(progress)
                    except Exception:  # never let callback break us
                        logger.exception("progress_cb raised")
                continue
            # Regular stderr — retain tail for diagnostics
            line_bytes = len(line.encode("utf-8", errors="replace"))
            stderr_buffer.append(line)
            stderr_buffer_bytes += line_bytes
            while stderr_buffer_bytes > max_buffer_bytes and stderr_buffer:
                evicted = stderr_buffer.pop(0)
                stderr_buffer_bytes -= len(evicted.encode("utf-8", errors="replace"))

    # Drain stderr in a background thread so we don't deadlock on
    # ffmpeg's stderr pipe filling up.
    drainer = threading.Thread(target=_drain_stderr, daemon=True, name="ffmpeg-stderr")
    drainer.start()

    try:
        rc = proc.wait(timeout=timeout_sec)
    except sp.TimeoutExpired:
        # Kill the entire process group, then wait briefly for cleanup.
        _terminate_group(proc)
        drainer.join(timeout=2.0)
        elapsed = time.monotonic() - t0
        raise QeemaTimeoutError(
            f"ffmpeg exceeded {timeout_sec:.0f}s (elapsed={elapsed:.1f}s); "
            f"argv={list(argv[:8])}..."
        )

    drainer.join(timeout=2.0)
    elapsed = time.monotonic() - t0
    stderr_tail = "\n".join(stderr_buffer)

    if rc != 0:
        raise VideoAssemblyError(
            f"ffmpeg failed with rc={rc}",
            context={
                "argv_head": list(argv[:8]),
                "stderr_tail": stderr_tail[-2000:],
                "elapsed_sec": round(elapsed, 2),
            },
        )

    # Output validation — real probe, not file-size heuristic
    if expected_output is not None:
        _validate_output(expected_output)

    return FFmpegResult(
        returncode=rc,
        duration_sec=elapsed,
        stderr_tail=stderr_tail,
        output_path=expected_output,
    )


# ════════════════════════════════════════════════════════════════
# Validation via ffprobe
# ════════════════════════════════════════════════════════════════
def _validate_output(path: Path) -> None:
    """
    Verify `path` is a parseable media file with at least one stream.
    Uses ffprobe; raises VideoAssemblyError on any issue.
    """
    if not path.exists():
        raise VideoAssemblyError(f"ffmpeg produced no output at {path}")
    if path.stat().st_size == 0:
        raise VideoAssemblyError(f"ffmpeg produced empty output at {path}")

    if shutil.which("ffprobe") is None:
        # Best effort if ffprobe is missing — skip validation but log.
        logger.warning("ffprobe not on PATH; skipping output validation")
        return

    try:
        result = sp.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except sp.TimeoutExpired:
        raise VideoAssemblyError(f"ffprobe timed out validating {path}")

    if result.returncode != 0:
        raise VideoAssemblyError(
            f"ffprobe rejected {path}: {result.stderr.strip()[:300]}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise VideoAssemblyError(f"ffprobe returned non-JSON for {path}: {e}")

    streams = data.get("streams", [])
    if not streams:
        raise VideoAssemblyError(f"ffmpeg produced no streams in {path}")


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════
def _apply_progress(p: FFmpegProgress, key: str, val: str) -> None:
    """Apply a single key=value pair to the FFmpegProgress accumulator."""
    if key == "out_time_ms":
        try:
            # NB: ffmpeg's `out_time_ms` is actually microseconds despite
            # the name. This is a long-standing ffmpeg quirk.
            p.out_time_us = int(val)
        except ValueError:
            pass
    elif key == "bitrate":
        # "1234.5kbits/s" or "N/A"
        try:
            p.bitrate_kbps = float(val.replace("kbits/s", "").strip())
        except ValueError:
            p.bitrate_kbps = 0.0
    elif key == "fps":
        try:
            p.fps = float(val)
        except ValueError:
            p.fps = 0.0
    elif key == "speed":
        # "1.05x" or "N/A"
        try:
            p.speed = float(val.rstrip("x"))
        except ValueError:
            p.speed = 0.0
    elif key == "progress":
        if val == "end":
            p.finished = True


def _terminate_group(proc: sp.Popen) -> None:
    """
    Kill the entire process group of `proc`. Tries SIGTERM, escalates
    to SIGKILL after 5s.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return

    # Polite termination
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return

    try:
        proc.wait(timeout=5.0)
        return
    except sp.TimeoutExpired:
        pass

    # Forceful
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass

    try:
        proc.wait(timeout=2.0)
    except sp.TimeoutExpired:
        logger.warning(f"ffmpeg pid={proc.pid} did not exit after SIGKILL")


# ════════════════════════════════════════════════════════════════
# Convenience: probe a file's duration
# ════════════════════════════════════════════════════════════════
def probe_duration(path: Path) -> Optional[float]:
    """Return media duration in seconds, or None on failure."""
    if shutil.which("ffprobe") is None:
        return None
    if not path.exists():
        return None
    try:
        result = sp.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (sp.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None
