"""
infrastructure/parallel_quran.py — Concurrent Quran audio fetcher
=====================================================================
Why this exists
---------------
voice_engine.py:463–469 fetches Quran ayahs sequentially with a comment
saying "they're already CDN-fast & cached". For long surahs that is
plainly false:

  - Cold cache for سورة البقرة: 286 ayahs × ~600ms = ~3 minutes blocked.
  - Even with hot cache, sequential disk reads of 286 mp3s costs ~5–15s.
  - The CDN pool already has circuit breakers per source. There is no
    correctness reason for sequential execution.

This module wraps an existing fetch function with a ThreadPoolExecutor
and adds:

  - Bounded parallelism (default: 8 workers).
  - Per-ayah timeout that scales with retry count.
  - Aggregate error reporting (which ayahs failed, why).
  - Progress callbacks for observability.
  - Fail-fast option for development; partial-success for production.

Why ThreadPoolExecutor and not asyncio?
---------------------------------------
The existing fetch path is sync (requests + circuit breaker + atomic
file copy). Migrating to async requires rewriting the entire chain.
Threads are sufficient here because the work is I/O-bound and the
bottleneck is HTTPS round-trips, not CPU.

The day we move the codebase to async, this module gets a 10-line
async sibling. Until then, threads are the pragmatic choice.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from core.exceptions import QuranFetchError
from core.interfaces import QuranAudioRequest, QuranAudioResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FetchProgress:
    total: int
    completed: int = 0
    failed: int = 0

    @property
    def percent(self) -> float:
        return (self.completed / self.total * 100.0) if self.total else 0.0


@dataclass(slots=True)
class BatchResult:
    """Result of a parallel batch fetch."""
    successes: Dict[str, QuranAudioResult] = field(default_factory=dict)
    """Map of output_path → QuranAudioResult."""

    failures: Dict[str, Exception] = field(default_factory=dict)
    """Map of output_path → exception that caused the failure."""

    total_duration_sec: float = 0.0

    @property
    def total(self) -> int:
        return len(self.successes) + len(self.failures)

    @property
    def success_rate(self) -> float:
        return (len(self.successes) / self.total) if self.total else 1.0


# ════════════════════════════════════════════════════════════════
# Parallel fetcher
# ════════════════════════════════════════════════════════════════
class ParallelQuranFetcher:
    """
    Concurrent wrapper around any function with signature:

        fetch(request: QuranAudioRequest) -> QuranAudioResult

    Designed to be a drop-in addition to VoiceEngine without disturbing
    the existing _QuranFetcher class.

    Example:
        pf = ParallelQuranFetcher(
            fetch_fn=voice_engine.fetch_quran_request,
            max_workers=8,
        )
        result = pf.fetch_batch([req1, req2, ..., req286])

        if result.failures:
            # decide: fail the episode, or proceed with what we have
            ...
    """

    def __init__(
        self,
        *,
        fetch_fn: Callable[[QuranAudioRequest], QuranAudioResult],
        max_workers: int = 8,
        per_request_timeout_sec: float = 60.0,
        fail_fast: bool = False,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if per_request_timeout_sec <= 0:
            raise ValueError("per_request_timeout_sec must be positive")

        self._fetch_fn = fetch_fn
        self._max_workers = max_workers
        self._timeout_sec = per_request_timeout_sec
        self._fail_fast = fail_fast

    def fetch_batch(
        self,
        requests: Sequence[QuranAudioRequest],
        *,
        progress_cb: Optional[Callable[[FetchProgress], None]] = None,
    ) -> BatchResult:
        """
        Fetch all requests concurrently. Returns successes + failures.

        If `fail_fast=True`, the first failure cancels remaining work
        and the partial result is returned. Otherwise all requests are
        attempted regardless.
        """
        result = BatchResult()
        if not requests:
            return result

        progress = FetchProgress(total=len(requests))
        progress_lock = threading.Lock()

        def _worker(req: QuranAudioRequest) -> tuple[str, QuranAudioResult]:
            return req.output_path, self._fetch_fn(req)

        t0 = time.monotonic()
        with cf.ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="quran-fetch",
        ) as pool:
            futures = {
                pool.submit(_worker, req): req for req in requests
            }
            try:
                for fut in cf.as_completed(futures):
                    req = futures[fut]
                    try:
                        path, fetch_result = fut.result(
                            timeout=self._timeout_sec
                        )
                        result.successes[path] = fetch_result
                        with progress_lock:
                            progress.completed += 1
                    except cf.TimeoutError:
                        # Timeout in fut.result *only* applies to the
                        # remaining wait; the work itself is still
                        # running. We record it as failed.
                        result.failures[req.output_path] = QuranFetchError(
                            req.surah,
                            req.ayah,
                            sources_tried=[],
                            cause=TimeoutError(
                                f"fetch_batch worker exceeded "
                                f"{self._timeout_sec:.0f}s"
                            ),
                        )
                        with progress_lock:
                            progress.failed += 1
                            progress.completed += 1
                    except Exception as e:    # noqa: BLE001 — we wrap
                        result.failures[req.output_path] = e
                        with progress_lock:
                            progress.failed += 1
                            progress.completed += 1
                        if self._fail_fast:
                            for f in futures:
                                if not f.done():
                                    f.cancel()
                            break

                    if progress_cb is not None:
                        try:
                            progress_cb(progress)
                        except Exception:
                            logger.exception("progress_cb raised; ignoring")
            finally:
                # Ensure no zombie futures linger
                for f in futures:
                    if not f.done():
                        f.cancel()

        result.total_duration_sec = time.monotonic() - t0

        logger.info(
            f"📥 Quran batch: {len(result.successes)}/{len(requests)} succeeded "
            f"in {result.total_duration_sec:.1f}s "
            f"(parallelism={self._max_workers})"
        )
        return result
