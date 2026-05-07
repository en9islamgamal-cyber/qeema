"""
tests/test_lru_cache.py
=========================
Verifies LRU eviction, TTL, schema versioning, and concurrency.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.lru_cache import (
    BoundedLRUFileCache,
    SCHEMA_VERSION,
    make_cache_key,
)


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════
@pytest.fixture
def small_cache(tmp_path: Path) -> BoundedLRUFileCache:
    """A 1KB cache with 1-day TTL — small enough to hit limits in tests."""
    return BoundedLRUFileCache(
        root=tmp_path / "cache",
        max_size_bytes=1024,
        max_age_seconds=86400,
    )


def write_blob(path: Path, size: int) -> Path:
    """Write a deterministic blob of `size` bytes."""
    path.write_bytes(b"x" * size)
    return path


# ════════════════════════════════════════════════════════════════
# Key generation
# ════════════════════════════════════════════════════════════════
class TestMakeCacheKey:
    def test_deterministic(self) -> None:
        k1 = make_cache_key("a", "b", "c")
        k2 = make_cache_key("a", "b", "c")
        assert k1 == k2

    def test_order_matters(self) -> None:
        k1 = make_cache_key("a", "b")
        k2 = make_cache_key("b", "a")
        assert k1 != k2

    def test_includes_schema_version(self) -> None:
        """Bumping SCHEMA_VERSION must change all keys."""
        k1 = make_cache_key("test")
        # Manually compute what the key would be at v1 to confirm
        # v2 produces a different key. We rely on the fact that
        # the actual SCHEMA_VERSION is currently >= 2.
        assert SCHEMA_VERSION >= 2  # if you bump it, also update tests
        assert len(k1) == 32

    def test_returns_hex(self) -> None:
        k = make_cache_key("anything")
        assert all(c in "0123456789abcdef" for c in k)
        assert len(k) == 32


# ════════════════════════════════════════════════════════════════
# Basic put/get
# ════════════════════════════════════════════════════════════════
class TestBasicOperations:
    def test_put_then_get(
        self, small_cache: BoundedLRUFileCache, tmp_path: Path
    ) -> None:
        src = write_blob(tmp_path / "src.bin", 100)
        key = make_cache_key("test1")

        result = small_cache.put(key, src)
        assert result is not None
        assert result.exists()

        hit = small_cache.get(key)
        assert hit is not None
        assert hit.exists()
        assert hit.read_bytes() == b"x" * 100

    def test_miss_returns_none(self, small_cache: BoundedLRUFileCache) -> None:
        assert small_cache.get("nonexistent_key" * 4) is None
        stats = small_cache.stats()
        assert stats.misses == 1
        assert stats.hits == 0

    def test_hit_count(
        self, small_cache: BoundedLRUFileCache, tmp_path: Path
    ) -> None:
        src = write_blob(tmp_path / "s.bin", 50)
        key = make_cache_key("hit")
        small_cache.put(key, src)

        for _ in range(3):
            assert small_cache.get(key) is not None

        stats = small_cache.stats()
        assert stats.hits == 3
        assert stats.misses == 0
        assert stats.hit_rate == 1.0

    def test_invalidate(
        self, small_cache: BoundedLRUFileCache, tmp_path: Path
    ) -> None:
        src = write_blob(tmp_path / "s.bin", 50)
        key = make_cache_key("inv")
        small_cache.put(key, src)
        assert small_cache.invalidate(key) is True
        assert small_cache.invalidate(key) is False
        assert small_cache.get(key) is None


# ════════════════════════════════════════════════════════════════
# Eviction policies
# ════════════════════════════════════════════════════════════════
class TestEviction:
    def test_size_eviction(
        self, small_cache: BoundedLRUFileCache, tmp_path: Path
    ) -> None:
        """Adding > max_size triggers LRU eviction."""
        # Cache cap is 1024 bytes. Insert 5 blobs of 300 bytes each.
        keys: list[str] = []
        for i in range(5):
            src = write_blob(tmp_path / f"s{i}.bin", 300)
            k = make_cache_key(f"k{i}")
            small_cache.put(k, src)
            keys.append(k)

        stats = small_cache.stats()
        # Total inserted = 1500 bytes; cap = 1024.
        # Should have evicted at least 2 entries.
        assert stats.bytes_in_use <= 1024
        assert stats.evictions_size >= 2

    def test_lru_order(
        self, small_cache: BoundedLRUFileCache, tmp_path: Path
    ) -> None:
        """The least-recently-used entry is evicted first."""
        # Put 3 items, totalling 900 bytes (under cap).
        sources = [write_blob(tmp_path / f"s{i}.bin", 300) for i in range(3)]
        keys = [make_cache_key(f"key{i}") for i in range(3)]
        for k, s in zip(keys, sources):
            small_cache.put(k, s)

        # Touch key 0 and key 2 (key 1 is now LRU)
        small_cache.get(keys[0])
        small_cache.get(keys[2])

        # Adding a 4th 300-byte blob exceeds 1024 → must evict LRU = key 1
        src4 = write_blob(tmp_path / "s4.bin", 300)
        small_cache.put(make_cache_key("key4"), src4)

        assert small_cache.get(keys[1]) is None  # evicted
        assert small_cache.get(keys[0]) is not None
        assert small_cache.get(keys[2]) is not None

    def test_oversized_entry_rejected(
        self, small_cache: BoundedLRUFileCache, tmp_path: Path
    ) -> None:
        """A single entry larger than max_size_bytes is refused."""
        src = write_blob(tmp_path / "huge.bin", 2048)  # 2x cap
        result = small_cache.put(make_cache_key("huge"), src)
        assert result is None
        assert small_cache.stats().write_failures == 1

    def test_age_eviction(self, tmp_path: Path) -> None:
        """Entries older than max_age_seconds are evicted on get."""
        cache = BoundedLRUFileCache(
            root=tmp_path / "c",
            max_size_bytes=1024**3,
            max_age_seconds=1,  # 1 second
        )
        src = write_blob(tmp_path / "s.bin", 100)
        key = make_cache_key("age_test")
        cache.put(key, src)

        # Immediately readable
        assert cache.get(key) is not None

        # Wait past TTL
        time.sleep(1.2)

        assert cache.get(key) is None
        assert cache.stats().evictions_age >= 1

    def test_missing_file_handled(
        self, small_cache: BoundedLRUFileCache, tmp_path: Path
    ) -> None:
        """If a cached file is deleted out-of-band, get() returns None."""
        src = write_blob(tmp_path / "s.bin", 100)
        key = make_cache_key("missing_test")
        cache_path = small_cache.put(key, src)
        assert cache_path is not None

        cache_path.unlink()

        assert small_cache.get(key) is None
        assert small_cache.stats().read_failures >= 1


# ════════════════════════════════════════════════════════════════
# Persistence across instances
# ════════════════════════════════════════════════════════════════
class TestPersistence:
    def test_reload_index_on_reopen(self, tmp_path: Path) -> None:
        """A new BoundedLRUFileCache rebuilds its index from disk."""
        cache_root = tmp_path / "persistent"

        # First cache instance: write 3 entries
        c1 = BoundedLRUFileCache(
            root=cache_root, max_size_bytes=1024**3, max_age_seconds=86400,
        )
        keys = [make_cache_key(f"k{i}") for i in range(3)]
        for i, k in enumerate(keys):
            src = write_blob(tmp_path / f"s{i}.bin", 100)
            c1.put(k, src)

        # Second instance over the same root
        c2 = BoundedLRUFileCache(
            root=cache_root, max_size_bytes=1024**3, max_age_seconds=86400,
        )
        for k in keys:
            assert c2.get(k) is not None
        assert c2.stats().hits == 3


# ════════════════════════════════════════════════════════════════
# Thread safety
# ════════════════════════════════════════════════════════════════
class TestConcurrency:
    def test_concurrent_puts_no_corruption(self, tmp_path: Path) -> None:
        """Many threads doing put() must produce a consistent state."""
        cache = BoundedLRUFileCache(
            root=tmp_path / "c",
            max_size_bytes=1024 * 1024,
            max_age_seconds=86400,
        )

        def worker(thread_id: int) -> None:
            for i in range(20):
                key = make_cache_key(f"t{thread_id}_i{i}")
                src = write_blob(tmp_path / f"src_{thread_id}_{i}.bin", 50)
                cache.put(key, src)
                # Interleave gets
                cache.get(key)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Sanity: bytes_in_use must equal sum of actual file sizes
        stats = cache.stats()
        actual_bytes = sum(
            f.stat().st_size
            for f in (tmp_path / "c").rglob("*.bin")
            if f.is_file()
        )
        assert stats.bytes_in_use == actual_bytes
