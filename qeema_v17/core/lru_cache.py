"""
core/lru_cache.py — Bounded LRU file cache with TTL and size limits
======================================================================
Why this exists
---------------
The original cache implementation has three latent problems:

1. No size cap → disk fills up over time. CI runners die when full.
2. No eviction → cold entries stay forever; hot entries can starve.
3. Cache key has no schema version → changing the keying logic
   serves stale entries indefinitely.

This module implements a thread-safe, schema-versioned, bounded LRU
cache for files on disk. It is **observable** (records hits/misses/evictions)
and **defensive** (handles corruption, partial writes, missing files).

Properties
----------
- O(1) get/put using OrderedDict + size accounting.
- Atomic writes: write to .tmp then os.replace().
- Schema versioning: cache key includes a SCHEMA_VERSION constant.
- Eviction policy: LRU + max_size_bytes + max_age_seconds.
- Thread-safe via RLock.
- No background thread; eviction runs lazily on put().

Why a custom impl instead of `cachetools` or `diskcache`?
----------------------------------------------------------
- diskcache uses SQLite + locks; for our blob-heavy use case it adds
  overhead without buying us anything.
- cachetools is in-memory only; we need persistence.
- We need atomic file writes integrated with eviction; off-the-shelf
  doesn't give us that.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Tuple

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Schema versioning — bump when key derivation logic changes
# ════════════════════════════════════════════════════════════════
SCHEMA_VERSION: int = 2


@dataclass(slots=True)
class CacheStats:
    """Snapshot of cache health. Read-only by convention."""
    hits: int = 0
    misses: int = 0
    evictions_lru: int = 0
    evictions_age: int = 0
    evictions_size: int = 0
    bytes_in_use: int = 0
    entry_count: int = 0
    write_failures: int = 0
    read_failures: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 4),
            "evictions": {
                "lru": self.evictions_lru,
                "age": self.evictions_age,
                "size": self.evictions_size,
            },
            "bytes_in_use": self.bytes_in_use,
            "entry_count": self.entry_count,
            "write_failures": self.write_failures,
            "read_failures": self.read_failures,
        }


@dataclass(slots=True)
class _Entry:
    """Internal cache entry. Tracks size + last-access time."""
    path: Path
    size: int
    written_at: float


def make_cache_key(*parts: str) -> str:
    """
    Derive a cache key from arbitrary string parts.

    All callers should use this. SCHEMA_VERSION is automatically
    incorporated, so bumping it invalidates every existing entry
    on a coordinated rollout.

    Returns 32 hex chars (128 bits truncated SHA-256). Collision
    probability is ~10^-19 across 10^9 entries — irrelevant in practice.
    """
    h = hashlib.sha256()
    h.update(f"v{SCHEMA_VERSION}".encode("ascii"))
    for p in parts:
        h.update(b"\x00")
        h.update(p.encode("utf-8") if isinstance(p, str) else bytes(p))
    return h.hexdigest()[:32]


class BoundedLRUFileCache:
    """
    Thread-safe LRU file cache with size + age limits.

    Usage:
        cache = BoundedLRUFileCache(
            root=Path("/tmp/cache"),
            max_size_bytes=2 * 1024**3,   # 2 GiB
            max_age_seconds=14 * 86400,   # 2 weeks
        )

        if (hit := cache.get(key)) is not None:
            shutil.copy(hit, target)
        else:
            produce_file(target)
            cache.put(key, target)
    """

    def __init__(
        self,
        *,
        root: Path,
        max_size_bytes: int = 2 * 1024**3,
        max_age_seconds: int = 14 * 86400,
        suffix: str = ".bin",
    ) -> None:
        if max_size_bytes <= 0:
            raise ValueError("max_size_bytes must be positive")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

        self._root: Path = root
        self._root.mkdir(parents=True, exist_ok=True)

        self._max_size: int = max_size_bytes
        self._max_age: int = max_age_seconds
        self._suffix: str = suffix

        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._stats: CacheStats = CacheStats()
        self._lock: threading.RLock = threading.RLock()

        self._reload_index()

    # ───────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────
    def get(self, key: str) -> Optional[Path]:
        """
        Return cached file path if present and valid; else None.
        Marks the entry as recently used (LRU promotion).
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._stats.misses += 1
                return None

            # Validate: file still exists and isn't expired
            if not entry.path.exists():
                self._evict(key, reason="missing")
                self._stats.misses += 1
                self._stats.read_failures += 1
                return None

            if (time.time() - entry.written_at) > self._max_age:
                self._evict(key, reason="age")
                self._stats.misses += 1
                self._stats.evictions_age += 1
                return None

            # LRU promotion
            self._entries.move_to_end(key)
            self._stats.hits += 1
            return entry.path

    def put(self, key: str, source: Path) -> Optional[Path]:
        """
        Store `source` under `key`. Returns the destination path on
        success, or None on failure (logged but not raised).

        The source file is *copied*, not moved — caller retains ownership.
        """
        if not source.exists():
            self._stats.write_failures += 1
            logger.warning(f"cache.put: source missing: {source}")
            return None

        size = source.stat().st_size
        if size <= 0:
            self._stats.write_failures += 1
            logger.warning(f"cache.put: source is empty: {source}")
            return None

        if size > self._max_size:
            # Single entry larger than total cap — refuse rather than evict everything
            self._stats.write_failures += 1
            logger.warning(
                f"cache.put: file size {size} > max_size {self._max_size}; not caching"
            )
            return None

        dest = self._key_to_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")

        try:
            shutil.copy2(source, tmp)
            os.replace(tmp, dest)  # atomic
        except OSError as e:
            self._stats.write_failures += 1
            tmp.unlink(missing_ok=True)
            logger.warning(f"cache.put: write failed for {key}: {e}")
            return None

        with self._lock:
            # If overwriting, refund old size
            if old := self._entries.get(key):
                self._stats.bytes_in_use -= old.size

            self._entries[key] = _Entry(
                path=dest, size=size, written_at=time.time()
            )
            self._entries.move_to_end(key)
            self._stats.bytes_in_use += size
            self._stats.entry_count = len(self._entries)
            self._enforce_size_limit_locked()

        return dest

    def invalidate(self, key: str) -> bool:
        """Remove an entry. Returns True if the key existed."""
        with self._lock:
            if key in self._entries:
                self._evict(key, reason="manual")
                return True
            return False

    def clear(self) -> int:
        """Remove all entries. Returns count of entries removed."""
        with self._lock:
            count = len(self._entries)
            for key in list(self._entries.keys()):
                self._evict(key, reason="manual")
            return count

    def stats(self) -> CacheStats:
        """Return a snapshot of cache statistics."""
        with self._lock:
            # Return a copy by reconstructing — CacheStats is small
            return CacheStats(
                hits=self._stats.hits,
                misses=self._stats.misses,
                evictions_lru=self._stats.evictions_lru,
                evictions_age=self._stats.evictions_age,
                evictions_size=self._stats.evictions_size,
                bytes_in_use=self._stats.bytes_in_use,
                entry_count=self._stats.entry_count,
                write_failures=self._stats.write_failures,
                read_failures=self._stats.read_failures,
            )

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ───────────────────────────────────────────────────────────
    # Internals
    # ───────────────────────────────────────────────────────────
    def _key_to_path(self, key: str) -> Path:
        # Shard into 256 directories to avoid huge flat dirs
        return self._root / key[:2] / f"{key}{self._suffix}"

    def _evict(self, key: str, *, reason: str) -> None:
        """Caller must hold self._lock."""
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        try:
            entry.path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"cache._evict: unlink failed: {e}")
        self._stats.bytes_in_use = max(0, self._stats.bytes_in_use - entry.size)
        self._stats.entry_count = len(self._entries)

        if reason == "lru":
            self._stats.evictions_lru += 1
        elif reason == "age":
            self._stats.evictions_age += 1
        elif reason == "size":
            self._stats.evictions_size += 1

    def _enforce_size_limit_locked(self) -> None:
        """Caller must hold self._lock. Evicts LRU entries until under cap."""
        while (
            self._stats.bytes_in_use > self._max_size
            and self._entries
        ):
            # OrderedDict.popitem(last=False) gives us the least-recently-used
            oldest_key, _ = next(iter(self._entries.items()))
            self._evict(oldest_key, reason="size")

    def _reload_index(self) -> None:
        """
        Rebuild in-memory index from on-disk state on startup.

        Order is by mtime ascending so the oldest file becomes the
        LRU eviction target.
        """
        with self._lock:
            files: list[Tuple[float, Path]] = []
            for path in self._root.rglob(f"*{self._suffix}"):
                if not path.is_file():
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, path))

            files.sort(key=lambda t: t[0])

            now = time.time()
            for mtime, path in files:
                # Reverse-engineer key from filename (strip suffix)
                key = path.stem

                age = now - mtime
                if age > self._max_age:
                    path.unlink(missing_ok=True)
                    continue

                size = path.stat().st_size
                self._entries[key] = _Entry(path=path, size=size, written_at=mtime)
                self._stats.bytes_in_use += size

            self._stats.entry_count = len(self._entries)
            self._enforce_size_limit_locked()

            if self._entries:
                logger.info(
                    f"cache: loaded {len(self._entries)} entries "
                    f"({self._stats.bytes_in_use / 1024**2:.1f} MiB) from {self._root}"
                )
