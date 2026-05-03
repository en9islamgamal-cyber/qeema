"""
tests/test_idempotency.py
==========================
Verifies key derivation determinism and checkpoint store correctness.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core.idempotency import (
    Checkpoint,
    CheckpointStore,
    IdempotencyKey,
    KEY_SCHEMA_VERSION,
    run_idempotent,
)


# ════════════════════════════════════════════════════════════════
# IdempotencyKey
# ════════════════════════════════════════════════════════════════
class TestIdempotencyKey:
    def test_deterministic(self) -> None:
        k1 = IdempotencyKey.derive(
            episode_number=7,
            pipeline_version="11.0.0",
            inputs={"a": 1, "b": "two"},
        )
        k2 = IdempotencyKey.derive(
            episode_number=7,
            pipeline_version="11.0.0",
            inputs={"a": 1, "b": "two"},
        )
        assert k1.value == k2.value

    def test_input_order_does_not_matter(self) -> None:
        """Dict ordering must not affect key derivation."""
        k1 = IdempotencyKey.derive(
            episode_number=1,
            pipeline_version="1.0",
            inputs={"a": 1, "b": 2, "c": 3},
        )
        k2 = IdempotencyKey.derive(
            episode_number=1,
            pipeline_version="1.0",
            inputs={"c": 3, "a": 1, "b": 2},
        )
        assert k1 == k2

    def test_different_inputs_produce_different_keys(self) -> None:
        k1 = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={"a": 1},
        )
        k2 = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={"a": 2},
        )
        assert k1 != k2

    def test_different_episode_produces_different_key(self) -> None:
        k1 = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={},
        )
        k2 = IdempotencyKey.derive(
            episode_number=2, pipeline_version="1.0", inputs={},
        )
        assert k1 != k2

    def test_different_version_produces_different_key(self) -> None:
        """Version bumps invalidate keys deliberately."""
        k1 = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={},
        )
        k2 = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0.1", inputs={},
        )
        assert k1 != k2

    def test_invalid_episode_rejected(self) -> None:
        with pytest.raises(ValueError):
            IdempotencyKey.derive(
                episode_number=0, pipeline_version="1.0", inputs={},
            )
        with pytest.raises(ValueError):
            IdempotencyKey.derive(
                episode_number=-1, pipeline_version="1.0", inputs={},
            )

    def test_empty_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            IdempotencyKey.derive(
                episode_number=1, pipeline_version="", inputs={},
            )

    def test_value_format(self) -> None:
        """Value is exactly 32 hex chars."""
        key = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={},
        )
        assert len(key.value) == 32
        assert all(c in "0123456789abcdef" for c in key.value)

    def test_construct_directly_validates(self) -> None:
        with pytest.raises(ValueError):
            IdempotencyKey("nothex" + "0" * 26)
        with pytest.raises(ValueError):
            IdempotencyKey("a" * 31)  # too short

    def test_floats_canonicalized(self) -> None:
        """Float repr nondeterminism doesn't leak into keys."""
        k1 = IdempotencyKey.derive(
            episode_number=1,
            pipeline_version="1.0",
            inputs={"x": 0.1 + 0.2},
        )
        k2 = IdempotencyKey.derive(
            episode_number=1,
            pipeline_version="1.0",
            inputs={"x": 0.30000000000000004},  # what 0.1+0.2 actually is
        )
        # Both round to "0.300000" → same key
        assert k1 == k2


# ════════════════════════════════════════════════════════════════
# CheckpointStore
# ════════════════════════════════════════════════════════════════
class TestCheckpointStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> CheckpointStore:
        return CheckpointStore(root=tmp_path / "checkpoints")

    @pytest.fixture
    def key(self) -> IdempotencyKey:
        return IdempotencyKey.derive(
            episode_number=1, pipeline_version="test", inputs={},
        )

    def test_record_and_retrieve(
        self, store: CheckpointStore, key: IdempotencyKey,
    ) -> None:
        store.initialize(key, episode_number=1)
        store.record(key, stage="script", duration_ms=1234, output={"hash": "abc"})

        completed = store.list_completed(key)
        assert len(completed) == 1
        assert completed[0].stage == "script"
        assert completed[0].duration_ms == 1234
        assert completed[0].output == {"hash": "abc"}

    def test_is_completed(
        self, store: CheckpointStore, key: IdempotencyKey,
    ) -> None:
        assert store.is_completed(key, "script") is False
        store.record(key, stage="script", duration_ms=1)
        assert store.is_completed(key, "script") is True
        assert store.is_completed(key, "audio") is False

    def test_get_output_returns_most_recent(
        self, store: CheckpointStore, key: IdempotencyKey,
    ) -> None:
        store.record(key, stage="render", duration_ms=1, output={"v": 1})
        store.record(key, stage="render", duration_ms=1, output={"v": 2})
        # Most recent record wins
        assert store.get_output(key, "render") == {"v": 2}

    def test_initialize_idempotent(
        self, store: CheckpointStore, key: IdempotencyKey, tmp_path: Path,
    ) -> None:
        """initialize() called twice doesn't overwrite."""
        store.initialize(key, episode_number=1, metadata={"a": 1})
        store.initialize(key, episode_number=1, metadata={"a": 2})
        meta_path = tmp_path / "checkpoints" / key.value[:2] / key.value / "meta.json"
        meta = json.loads(meta_path.read_text())
        assert meta["extra"] == {"a": 1}  # first call wins

    def test_purge(
        self, store: CheckpointStore, key: IdempotencyKey,
    ) -> None:
        store.initialize(key, episode_number=1)
        store.record(key, stage="x", duration_ms=1)
        assert store.purge(key) is True
        assert store.purge(key) is False  # already gone
        assert store.list_completed(key) == []

    def test_corrupt_line_skipped(
        self, store: CheckpointStore, key: IdempotencyKey, tmp_path: Path,
    ) -> None:
        """A partially-written / corrupted JSONL line is skipped, not fatal."""
        store.initialize(key, episode_number=1)
        store.record(key, stage="ok1", duration_ms=1)

        # Append a corrupt line
        ckpt_path = (
            tmp_path / "checkpoints" / key.value[:2] / key.value / "checkpoints.jsonl"
        )
        with ckpt_path.open("a") as f:
            f.write("this is not valid json\n")

        store.record(key, stage="ok2", duration_ms=1)

        completed = store.list_completed(key)
        assert len(completed) == 2
        assert [c.stage for c in completed] == ["ok1", "ok2"]


# ════════════════════════════════════════════════════════════════
# run_idempotent decorator
# ════════════════════════════════════════════════════════════════
class TestRunIdempotent:
    def test_runs_once(self, tmp_path: Path) -> None:
        store = CheckpointStore(root=tmp_path / "c")
        key = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={},
        )
        store.initialize(key, episode_number=1)

        call_count = 0

        @run_idempotent(store, key, stage="my_stage")
        def my_stage() -> dict:
            nonlocal call_count
            call_count += 1
            return {"result": "ok", "count": call_count}

        first = my_stage()
        second = my_stage()
        third = my_stage()

        assert call_count == 1
        assert first == second == third
        assert first["result"] == "ok"

    def test_non_dict_return_rejected(self, tmp_path: Path) -> None:
        store = CheckpointStore(root=tmp_path / "c")
        key = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={},
        )

        @run_idempotent(store, key, stage="bad")
        def bad() -> str:    # type: ignore[return-value]
            return "not a dict"  # type: ignore[return-value]

        with pytest.raises(TypeError):
            bad()


# ════════════════════════════════════════════════════════════════
# Concurrent record() calls
# ════════════════════════════════════════════════════════════════
class TestConcurrentRecord:
    def test_concurrent_appends_no_corruption(self, tmp_path: Path) -> None:
        """Many threads recording stages must not produce malformed output."""
        store = CheckpointStore(root=tmp_path / "c")
        key = IdempotencyKey.derive(
            episode_number=1, pipeline_version="1.0", inputs={},
        )
        store.initialize(key, episode_number=1)

        def worker(idx: int) -> None:
            for i in range(10):
                store.record(
                    key,
                    stage=f"thread{idx}_step{i}",
                    duration_ms=i,
                    output={"thread": idx, "step": i},
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        completed = store.list_completed(key)
        assert len(completed) == 40
        # Every stage name must be unique
        stage_names = {c.stage for c in completed}
        assert len(stage_names) == 40
