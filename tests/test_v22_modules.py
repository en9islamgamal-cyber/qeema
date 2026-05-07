"""Tests for v22 modules: tafsir_cache, stage_retry."""
import json
import pytest
import time
from pathlib import Path
from unittest.mock import MagicMock

from core.tafsir_cache import (
    TafsirCache, CachedTafsirFetcher, CacheEntry, CACHE_SCHEMA_VERSION,
)
from core.stage_retry import (
    RetryPolicy, run_with_retry, get_policy, should_retry,
    POLICY_NO_RETRY, POLICY_DEFAULT,
)
from core.exceptions import (
    TransientError, PermanentError, QualityGateError,
)


# ════════════════════════════════════════════════════════════════
# TafsirCache
# ════════════════════════════════════════════════════════════════
class TestTafsirCache:
    def test_starts_empty_no_file(self, tmp_path):
        cache = TafsirCache(tmp_path / "cache.json")
        assert cache.get(1, 1, 16) is None
        assert cache.stats()["entries"] == 0

    def test_put_and_get(self, tmp_path):
        cache = TafsirCache(tmp_path / "cache.json")
        cache.put(1, 1, 16, "البسملة آية واحدة")
        assert cache.get(1, 1, 16) == "البسملة آية واحدة"

    def test_get_different_keys(self, tmp_path):
        cache = TafsirCache(tmp_path / "cache.json")
        cache.put(1, 1, 16, "saadi text")
        cache.put(1, 1, 169, "muyassar text")
        assert cache.get(1, 1, 16) == "saadi text"
        assert cache.get(1, 1, 169) == "muyassar text"
        assert cache.get(1, 2, 16) is None  # different ayah

    def test_empty_text_not_cached(self, tmp_path):
        cache = TafsirCache(tmp_path / "cache.json")
        cache.put(1, 1, 16, "")
        cache.put(1, 1, 169, "   ")
        assert cache.get(1, 1, 16) is None
        assert cache.get(1, 1, 169) is None

    def test_persistence_across_instances(self, tmp_path):
        path = tmp_path / "cache.json"
        cache1 = TafsirCache(path)
        cache1.put(1, 1, 16, "saved text")
        cache1.flush()

        cache2 = TafsirCache(path)
        assert cache2.get(1, 1, 16) == "saved text"

    def test_atomic_write(self, tmp_path):
        path = tmp_path / "cache.json"
        cache = TafsirCache(path)
        cache.put(2, 1, 16, "البقرة 1")
        cache.flush()

        # Verify file is valid JSON
        with path.open() as f:
            data = json.load(f)
        assert data["schema_version"] == CACHE_SCHEMA_VERSION
        assert "2:1:16" in data["entries"]

    def test_corrupt_file_recovers(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text("not valid json {{{")
        cache = TafsirCache(path)
        assert cache.stats()["entries"] == 0
        # Should still work
        cache.put(1, 1, 16, "fresh start")
        assert cache.get(1, 1, 16) == "fresh start"

    def test_schema_version_mismatch_resets(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text(json.dumps({
            "schema_version": 999,
            "entries": {"1:1:16": {"text": "old", "fetched_at": time.time()}},
        }))
        cache = TafsirCache(path)
        assert cache.stats()["entries"] == 0
        assert cache.get(1, 1, 16) is None

    def test_ttl_expiration(self, tmp_path):
        path = tmp_path / "cache.json"
        # Create entry with old timestamp
        old_data = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "entries": {
                "1:1:16": {
                    "text": "old text",
                    "fetched_at": time.time() - 1000000,  # very old
                }
            }
        }
        path.write_text(json.dumps(old_data))
        cache = TafsirCache(path, ttl_seconds=86400)  # 1 day
        assert cache.get(1, 1, 16) is None  # expired
        assert cache.stats()["entries"] == 0

    def test_lazy_expiration_on_get(self, tmp_path):
        cache = TafsirCache(tmp_path / "cache.json", ttl_seconds=1)
        cache.put(1, 1, 16, "soon-to-expire")
        # Manually set old timestamp
        with cache._lock:
            cache._entries["1:1:16"] = CacheEntry(
                text="soon-to-expire",
                fetched_at=time.time() - 100,
            )
        assert cache.get(1, 1, 16) is None
        assert cache.stats()["entries"] == 0

    def test_reset(self, tmp_path):
        path = tmp_path / "cache.json"
        cache = TafsirCache(path)
        cache.put(1, 1, 16, "x")
        cache.put(2, 1, 16, "y")
        cache.reset()
        assert cache.stats()["entries"] == 0
        # Verify reset persisted
        cache2 = TafsirCache(path)
        assert cache2.stats()["entries"] == 0

    def test_prune_expired(self, tmp_path):
        cache = TafsirCache(tmp_path / "cache.json", ttl_seconds=1)
        # Add a fresh + a stale entry
        cache.put(1, 1, 16, "fresh")
        with cache._lock:
            cache._entries["2:1:16"] = CacheEntry(
                text="stale", fetched_at=time.time() - 100,
            )
        removed = cache.prune_expired()
        assert removed == 1
        assert cache.get(1, 1, 16) == "fresh"
        assert cache.get(2, 1, 16) is None

    def test_stats_structure(self, tmp_path):
        cache = TafsirCache(tmp_path / "cache.json")
        cache.put(1, 1, 16, "test")
        cache.flush()
        stats = cache.stats()
        assert stats["entries"] == 1
        assert stats["size_kb"] >= 0
        assert "oldest_age_days" in stats


class TestCachedTafsirFetcher:
    def test_passthrough_on_miss(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch.return_value = "fetched text"
        cache = TafsirCache(tmp_path / "cache.json")
        cached_fetcher = CachedTafsirFetcher(fetcher, cache)
        
        result = cached_fetcher.fetch(surah=1, ayah=1, tafsir_id=16)
        assert result == "fetched text"
        fetcher.fetch.assert_called_once()

    def test_returns_cached_on_hit(self, tmp_path):
        fetcher = MagicMock()
        cache = TafsirCache(tmp_path / "cache.json")
        cache.put(1, 1, 16, "cached value")
        cached_fetcher = CachedTafsirFetcher(fetcher, cache)
        
        result = cached_fetcher.fetch(surah=1, ayah=1, tafsir_id=16)
        assert result == "cached value"
        fetcher.fetch.assert_not_called()  # No upstream call

    def test_caches_after_miss(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch.return_value = "new text"
        cache = TafsirCache(tmp_path / "cache.json")
        cached_fetcher = CachedTafsirFetcher(fetcher, cache)
        
        cached_fetcher.fetch(surah=1, ayah=1, tafsir_id=16)
        cached_fetcher.fetch(surah=1, ayah=1, tafsir_id=16)  # second call
        # Upstream called only once
        assert fetcher.fetch.call_count == 1


# ════════════════════════════════════════════════════════════════
# Stage Retry
# ════════════════════════════════════════════════════════════════
class TestRetryPolicy:
    def test_compute_delay_first_attempt_zero(self):
        policy = RetryPolicy(max_attempts=3, base_delay_sec=2.0)
        assert policy.compute_delay(1) == 0.0

    def test_compute_delay_exponential(self):
        policy = RetryPolicy(
            max_attempts=5, base_delay_sec=1.0,
            max_delay_sec=100.0, jitter_pct=0.0,  # no jitter for determinism
        )
        assert policy.compute_delay(2) == 1.0   # 1 * 2^0
        assert policy.compute_delay(3) == 2.0   # 1 * 2^1
        assert policy.compute_delay(4) == 4.0   # 1 * 2^2
        assert policy.compute_delay(5) == 8.0   # 1 * 2^3

    def test_compute_delay_max_cap(self):
        policy = RetryPolicy(
            max_attempts=10, base_delay_sec=1.0,
            max_delay_sec=5.0, jitter_pct=0.0,
        )
        # Without cap would be 32.0 at attempt 7
        assert policy.compute_delay(7) <= 5.0


class TestShouldRetry:
    def test_quality_gate_never_retries(self):
        exc = QualityGateError(
            "test", critiques=[], episode_number=1, stage="test",
        )
        assert should_retry(exc) is False

    def test_permanent_never_retries(self):
        assert should_retry(PermanentError("permanent", episode_number=1)) is False

    def test_value_error_never_retries(self):
        assert should_retry(ValueError("bad input")) is False

    def test_type_error_never_retries(self):
        assert should_retry(TypeError("wrong type")) is False

    def test_transient_does_retry(self):
        assert should_retry(TransientError("temporary", episode_number=1)) is True

    def test_timeout_does_retry(self):
        assert should_retry(TimeoutError("timeout")) is True

    def test_connection_error_does_retry(self):
        assert should_retry(ConnectionError("network")) is True

    def test_generic_exception_retries_by_default(self):
        assert should_retry(Exception("unknown")) is True


class TestRunWithRetry:
    def test_succeeds_first_attempt(self):
        attempts = []
        def fn():
            attempts.append(1)
            return "ok"
        result = run_with_retry(fn, stage_name="test", policy=POLICY_DEFAULT)
        assert result == "ok"
        assert len(attempts) == 1

    def test_succeeds_after_retry(self):
        attempts = []
        def fn():
            attempts.append(1)
            if len(attempts) < 2:
                raise TransientError("temp", episode_number=1)
            return "ok"
        # Use no-delay policy for speed
        policy = RetryPolicy(max_attempts=3, base_delay_sec=0.01, jitter_pct=0)
        result = run_with_retry(fn, stage_name="test", policy=policy)
        assert result == "ok"
        assert len(attempts) == 2

    def test_exhausts_attempts(self):
        attempts = []
        def fn():
            attempts.append(1)
            raise TransientError("always fails", episode_number=1)
        policy = RetryPolicy(max_attempts=3, base_delay_sec=0.01, jitter_pct=0)
        with pytest.raises(TransientError):
            run_with_retry(fn, stage_name="test", policy=policy)
        assert len(attempts) == 3

    def test_no_retry_on_quality_gate(self):
        attempts = []
        def fn():
            attempts.append(1)
            raise QualityGateError(
                "bad quality", critiques=[],
                episode_number=1, stage="test",
            )
        with pytest.raises(QualityGateError):
            run_with_retry(fn, stage_name="test", policy=POLICY_DEFAULT)
        assert len(attempts) == 1  # No retries

    def test_no_retry_on_permanent(self):
        attempts = []
        def fn():
            attempts.append(1)
            raise PermanentError("bad", episode_number=1)
        with pytest.raises(PermanentError):
            run_with_retry(fn, stage_name="test", policy=POLICY_DEFAULT)
        assert len(attempts) == 1

    def test_on_retry_callback(self):
        callback_calls = []
        attempts = []
        def fn():
            attempts.append(1)
            if len(attempts) < 2:
                raise TransientError("temp", episode_number=1)
            return "ok"
        def on_retry(attempt, exc):
            callback_calls.append((attempt, type(exc).__name__))
        policy = RetryPolicy(max_attempts=3, base_delay_sec=0.01, jitter_pct=0)
        run_with_retry(fn, stage_name="test", policy=policy, on_retry=on_retry)
        assert callback_calls == [(1, "TransientError")]


class TestStagePolicies:
    def test_known_stages_have_policies(self):
        for stage in ["script", "audio", "ai_images", "render_scenes"]:
            policy = get_policy(stage)
            assert isinstance(policy, RetryPolicy)
            assert policy.max_attempts >= 1

    def test_unknown_stage_returns_default(self):
        policy = get_policy("nonexistent_stage")
        assert policy == POLICY_DEFAULT

    def test_subtitles_no_retry(self):
        policy = get_policy("subtitles")
        assert policy == POLICY_NO_RETRY
        assert policy.max_attempts == 1
