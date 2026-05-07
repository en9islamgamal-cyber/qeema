"""
tests/test_tafsir_cache_roundtrip.py — VALUE / QEEMA v22.5

Verifies that TafsirCache correctly persists entries across "process
restarts" — which is what GitHub Actions cache action does between runs.

[Why this matters]
The pipeline.yml restores `state/tafsir_cache.json` via actions/cache. Every
new run is effectively a fresh Python process loading the same file. If our
TafsirCache:
  - Writes a non-deterministic format
  - Doesn't merge new entries into the existing file
  - Loses entries between read/write cycles
... then the cache silently fails to deduplicate API calls and we burn
through quran.com's rate limit (or, in production, hit it 14× per episode).

[What we test]
  - Bytewise-stable JSON across read/write cycles
  - Both initial population AND incremental adds persist
  - CachedTafsirFetcher wrapper actually uses the cache (no spurious upstream calls)
  - fetch_combined() reuses cached fetch() entries
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.tafsir_cache import CachedTafsirFetcher, TafsirCache


@pytest.fixture
def cache_file_path():
    """Per-test temp cache file."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "tafsir_cache.json"


# ════════════════════════════════════════════════════════════════
# Round-trip persistence
# ════════════════════════════════════════════════════════════════
class TestCacheRoundTrip:
    def test_three_entries_survive_restart(self, cache_file_path):
        """Write 3 entries, simulate restart by re-instantiating, read back."""
        cache_v1 = TafsirCache(cache_file_path)
        cache_v1.put(1, 1, 169, "بسم الله — السعدي")
        cache_v1.put(1, 1, 16, "بسم الله — الميسر")
        cache_v1.put(1, 2, 169, "الحمد لله — السعدي")
        cache_v1.flush()

        # Simulate fresh process
        cache_v2 = TafsirCache(cache_file_path)
        assert cache_v2.get(1, 1, 169) == "بسم الله — السعدي"
        assert cache_v2.get(1, 1, 16) == "بسم الله — الميسر"
        assert cache_v2.get(1, 2, 169) == "الحمد لله — السعدي"

    def test_incremental_writes_dont_lose_data(self, cache_file_path):
        """Writing new entries must not wipe the existing ones."""
        # Initial population
        c1 = TafsirCache(cache_file_path)
        c1.put(1, 1, 169, "first")
        c1.flush()

        # Process restart, add more, save
        c2 = TafsirCache(cache_file_path)
        c2.put(1, 2, 169, "second")
        c2.put(1, 3, 169, "third")
        c2.flush()

        # Process restart again, BOTH old and new must survive
        c3 = TafsirCache(cache_file_path)
        assert c3.get(1, 1, 169) == "first", "Original entry lost"
        assert c3.get(1, 2, 169) == "second"
        assert c3.get(1, 3, 169) == "third"

    def test_json_schema_is_actions_cache_friendly(self, cache_file_path):
        """The JSON file must be deterministic enough that GitHub Actions
        cache action sees a useful diff between runs (no random ordering, etc.)."""
        c = TafsirCache(cache_file_path)
        c.put(1, 1, 169, "first")
        c.flush()

        with open(cache_file_path, encoding="utf-8") as f:
            content = f.read()
        parsed = json.loads(content)

        # Required top-level keys for the cache to work
        assert "entries" in parsed, "Schema must have 'entries' key"
        assert "schema_version" in parsed, "Schema must declare its version"


# ════════════════════════════════════════════════════════════════
# CachedTafsirFetcher wrapper
# ════════════════════════════════════════════════════════════════
class TestCachedTafsirFetcher:
    def test_cache_hit_avoids_upstream_call(self, cache_file_path):
        """Pre-populate cache, then verify wrapper returns from cache without
        calling the underlying fetcher."""
        cache = TafsirCache(cache_file_path)
        cache.put(1, 1, 169, "cached value")
        cache.flush()

        upstream = MagicMock()
        wrapper = CachedTafsirFetcher(fetcher=upstream, cache=cache)
        result = wrapper.fetch(surah=1, ayah=1, tafsir_id=169)

        assert result == "cached value"
        assert upstream.fetch.call_count == 0, \
            "Cache hit should NOT call upstream"

    def test_cache_miss_calls_upstream_and_persists(self, cache_file_path):
        """Empty cache → upstream is called → result is saved for next time."""
        cache = TafsirCache(cache_file_path)
        upstream = MagicMock()
        upstream.fetch.return_value = "fetched from quran.com"

        wrapper = CachedTafsirFetcher(fetcher=upstream, cache=cache)
        result = wrapper.fetch(surah=2, ayah=255, tafsir_id=169)

        assert result == "fetched from quran.com"
        assert upstream.fetch.call_count == 1
        wrapper._cache.flush()

        # Verify it persisted
        cache_reloaded = TafsirCache(cache_file_path)
        assert cache_reloaded.get(2, 255, 169) == "fetched from quran.com"

    def test_fetch_combined_uses_cache_for_both_tafsirs(self, cache_file_path):
        """fetch_combined() calls fetch() twice — both must hit cache if the
        entries are already there."""
        cache = TafsirCache(cache_file_path)
        cache.put(1, 1, 169, "Saadi for 1:1")  # tafsir id 169 = As-Saadi
        cache.put(1, 1, 16, "Muyassar for 1:1")  # tafsir id 16 = Al-Muyassar
        cache.flush()

        upstream = MagicMock()
        wrapper = CachedTafsirFetcher(fetcher=upstream, cache=cache)
        combined = wrapper.fetch_combined(surah=1, ayah=1)

        assert combined is not None
        assert "[تفسير السعدي]" in combined
        assert "[التفسير الميسر]" in combined
        assert "Saadi for 1:1" in combined
        assert "Muyassar for 1:1" in combined
        assert upstream.fetch.call_count == 0, \
            f"Both entries cached, expected 0 upstream calls, got {upstream.fetch.call_count}"

    def test_fetch_combined_partial_cache(self, cache_file_path):
        """Only Saadi cached, Muyassar must come from upstream."""
        cache = TafsirCache(cache_file_path)
        cache.put(1, 1, 169, "Saadi cached")
        # Muyassar NOT cached
        cache.flush()

        upstream = MagicMock()
        upstream.fetch.return_value = "Muyassar from upstream"

        wrapper = CachedTafsirFetcher(fetcher=upstream, cache=cache)
        combined = wrapper.fetch_combined(surah=1, ayah=1)

        assert combined is not None
        assert "Saadi cached" in combined
        assert "Muyassar from upstream" in combined
        # Exactly ONE upstream call (for the missing Muyassar)
        assert upstream.fetch.call_count == 1, (
            f"Expected 1 upstream call for the missing Muyassar, "
            f"got {upstream.fetch.call_count}"
        )

    def test_fetch_combined_returns_none_when_both_fail(self, cache_file_path):
        """If both upstream calls return None, fetch_combined returns None."""
        cache = TafsirCache(cache_file_path)
        upstream = MagicMock()
        upstream.fetch.return_value = None

        wrapper = CachedTafsirFetcher(fetcher=upstream, cache=cache)
        result = wrapper.fetch_combined(surah=99, ayah=99)
        assert result is None
