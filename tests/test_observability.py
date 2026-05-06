"""
tests/test_observability.py
=============================
Verifies span lifecycle, parent-child relationships, exception capture,
metric atomicity, and JSONL emission.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core.observability import (
    Counter,
    Histogram,
    MetricsRegistry,
    Span,
    SpanEmitter,
    SpanStatus,
    configure_emitter,
    current_span,
    current_trace_id,
    get_emitter,
    get_registry,
    new_span_id,
    new_trace_id,
    traced,
)


# ════════════════════════════════════════════════════════════════
# Trace ID / Span ID generation
# ════════════════════════════════════════════════════════════════
class TestIds:
    def test_trace_id_format(self) -> None:
        tid = new_trace_id()
        assert len(tid) == 32
        assert all(c in "0123456789abcdef" for c in tid)

    def test_span_id_format(self) -> None:
        sid = new_span_id()
        assert len(sid) == 16
        assert all(c in "0123456789abcdef" for c in sid)

    def test_unique(self) -> None:
        ids = {new_trace_id() for _ in range(100)}
        assert len(ids) == 100   # collision astronomically unlikely


# ════════════════════════════════════════════════════════════════
# Span lifecycle
# ════════════════════════════════════════════════════════════════
class TestSpanLifecycle:
    def test_basic_span(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)
        with emitter.span("my_op", episode=7) as s:
            s.set("provider", "gemini-1")
            s.add_event("started")

        # JSONL written
        assert (tmp_path / "spans.jsonl").exists()
        line = (tmp_path / "spans.jsonl").read_text().strip()
        record = json.loads(line)
        assert record["name"] == "my_op"
        assert record["status"] == "ok"
        assert record["attributes"]["episode"] == 7
        assert record["attributes"]["provider"] == "gemini-1"
        assert len(record["events"]) == 1
        assert record["events"][0]["name"] == "started"
        assert record["duration_ms"] is not None
        assert record["duration_ms"] >= 0

    def test_exception_recorded_and_reraised(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)
        with pytest.raises(RuntimeError, match="boom"):
            with emitter.span("failing_op") as s:
                s.set("attempt", 1)
                raise RuntimeError("boom")

        record = json.loads((tmp_path / "spans.jsonl").read_text().strip())
        assert record["status"] == "error"
        assert record["exception"]["type"] == "RuntimeError"
        assert record["exception"]["message"] == "boom"
        assert "RuntimeError" in record["exception"]["traceback"]

    def test_parent_child_relationship(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)
        with emitter.span("parent") as p:
            parent_span_id = p.span_id
            parent_trace_id = p.trace_id
            with emitter.span("child") as c:
                assert c.parent_span_id == parent_span_id
                assert c.trace_id == parent_trace_id   # same trace

        lines = [
            json.loads(l) for l in (tmp_path / "spans.jsonl").read_text().splitlines()
        ]
        # Child completes first (closes inner context), so it's emitted first
        child = next(r for r in lines if r["name"] == "child")
        parent = next(r for r in lines if r["name"] == "parent")
        assert child["parent_span_id"] == parent["span_id"]
        assert child["trace_id"] == parent["trace_id"]

    def test_current_span_in_context(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)
        assert current_span() is None
        assert current_trace_id() is None

        with emitter.span("outer") as outer:
            assert current_span() is outer
            assert current_trace_id() == outer.trace_id

        # Reset after exit
        assert current_span() is None
        assert current_trace_id() is None

    def test_set_handles_unserializable(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)

        class NotSerializable:
            def __repr__(self) -> str:
                return "<sentinel>"

        with emitter.span("ser_test") as s:
            s.set("obj", NotSerializable())   # falls back to str()

        record = json.loads((tmp_path / "spans.jsonl").read_text().strip())
        assert record["attributes"]["obj"] == "<sentinel>"


# ════════════════════════════════════════════════════════════════
# @traced decorator
# ════════════════════════════════════════════════════════════════
class TestTracedDecorator:
    def test_basic_trace(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)
        configure_emitter(emitter)
        try:
            @traced("my_func")
            def f(x: int) -> int:
                return x * 2

            assert f(5) == 10

            record = json.loads((tmp_path / "spans.jsonl").read_text().strip())
            assert record["name"] == "my_func"
            assert record["status"] == "ok"
        finally:
            configure_emitter(SpanEmitter())   # reset

    def test_capture_args(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)
        configure_emitter(emitter)
        try:
            @traced("my_func", capture_args=True)
            def f(x: int, *, y: str = "default") -> str:
                return f"{x}:{y}"

            f(5, y="hello")

            record = json.loads((tmp_path / "spans.jsonl").read_text().strip())
            assert "args_repr" in record["attributes"]
            assert "5" in record["attributes"]["args_repr"]
            assert "hello" in record["attributes"]["kwargs_repr"]
        finally:
            configure_emitter(SpanEmitter())

    def test_default_name(self, tmp_path: Path) -> None:
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)
        configure_emitter(emitter)
        try:
            @traced()
            def my_traced_function() -> None:
                pass

            my_traced_function()
            record = json.loads((tmp_path / "spans.jsonl").read_text().strip())
            assert "my_traced_function" in record["name"]
        finally:
            configure_emitter(SpanEmitter())


# ════════════════════════════════════════════════════════════════
# Counter
# ════════════════════════════════════════════════════════════════
class TestCounter:
    def test_basic(self) -> None:
        c = Counter("test")
        c.inc()
        c.inc(by=5)
        assert c.snapshot()[""] == 6

    def test_labels(self) -> None:
        c = Counter("requests")
        c.inc(labels={"provider": "gemini"})
        c.inc(labels={"provider": "gemini"})
        c.inc(labels={"provider": "groq"})
        snap = c.snapshot()
        assert snap["provider=gemini"] == 2
        assert snap["provider=groq"] == 1

    def test_negative_inc_rejected(self) -> None:
        c = Counter("test")
        with pytest.raises(ValueError):
            c.inc(by=-1)

    def test_atomic_under_concurrency(self) -> None:
        """1000 threads × 10 increments = exactly 10000."""
        c = Counter("test")

        def worker() -> None:
            for _ in range(10):
                c.inc()

        threads = [threading.Thread(target=worker) for _ in range(1000)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert c.snapshot()[""] == 10000


# ════════════════════════════════════════════════════════════════
# Histogram
# ════════════════════════════════════════════════════════════════
class TestHistogram:
    def test_basic(self) -> None:
        h = Histogram("latency")
        for v in [10, 20, 30, 40, 50]:
            h.record(v)
        snap = h.snapshot()[""]
        assert snap["count"] == 5
        assert snap["sum"] == 150
        assert snap["min"] == 10
        assert snap["max"] == 50
        assert snap["avg"] == 30

    def test_labels(self) -> None:
        h = Histogram("duration")
        h.record(100, labels={"stage": "render"})
        h.record(200, labels={"stage": "render"})
        h.record(50, labels={"stage": "encode"})
        snap = h.snapshot()
        assert snap["stage=render"]["count"] == 2
        assert snap["stage=render"]["avg"] == 150
        assert snap["stage=encode"]["count"] == 1

    def test_atomic_under_concurrency(self) -> None:
        h = Histogram("test")

        def worker(val: float) -> None:
            for _ in range(100):
                h.record(val)

        threads = [
            threading.Thread(target=worker, args=(float(i),))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = h.snapshot()[""]
        assert snap["count"] == 2000


# ════════════════════════════════════════════════════════════════
# MetricsRegistry
# ════════════════════════════════════════════════════════════════
class TestMetricsRegistry:
    def test_returns_same_counter(self) -> None:
        r = MetricsRegistry()
        c1 = r.counter("requests")
        c2 = r.counter("requests")
        assert c1 is c2

    def test_returns_same_histogram(self) -> None:
        r = MetricsRegistry()
        h1 = r.histogram("latency")
        h2 = r.histogram("latency")
        assert h1 is h2

    def test_snapshot_structure(self) -> None:
        r = MetricsRegistry()
        r.counter("c1").inc()
        r.histogram("h1").record(42)
        snap = r.snapshot()
        assert "counters" in snap
        assert "histograms" in snap
        assert "c1" in snap["counters"]
        assert "h1" in snap["histograms"]

    def test_write_snapshot_atomic(self, tmp_path: Path) -> None:
        r = MetricsRegistry()
        r.counter("test").inc(by=42)
        path = tmp_path / "metrics.json"
        r.write_snapshot(path)

        loaded = json.loads(path.read_text())
        assert loaded["counters"]["test"][""] == 42

    def test_global_registry_singleton(self) -> None:
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2


# ════════════════════════════════════════════════════════════════
# Concurrent span emission
# ════════════════════════════════════════════════════════════════
class TestConcurrentSpans:
    def test_concurrent_threads_no_corruption(self, tmp_path: Path) -> None:
        """N threads each open a span; all spans must be valid JSON."""
        emitter = SpanEmitter(jsonl_path=tmp_path / "spans.jsonl", also_log=False)

        def worker(tid: int) -> None:
            with emitter.span(f"thread_{tid}") as s:
                s.set("tid", tid)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = (tmp_path / "spans.jsonl").read_text().splitlines()
        assert len(lines) == 50
        records = [json.loads(l) for l in lines]
        names = {r["name"] for r in records}
        assert len(names) == 50

        # Every record has its own trace_id (no shared parent context)
        # because workers ran without an outer span
        trace_ids = {r["trace_id"] for r in records}
        assert len(trace_ids) == 50
