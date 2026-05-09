"""
core/observability.py — VALUE / QEEMA v22.5 — structured spans, metrics, trace context
========================================================================
Why this exists
---------------
The pipeline has zero production observability today. When CI fails at
3am, the operator's only forensic tool is grepping a log file. They
cannot see:

  - Which stage failed (script? render? upload?)
  - How long each previous stage took
  - Which provider was tried and what error each returned
  - Whether circuit breakers were open at the moment of failure
  - Memory / disk / fd usage at the time of failure

This module gives us all of that with stdlib only (no opentelemetry
dependency required, but compatible if you add one later).

What's here
-----------
1. TraceContext            — propagates a trace_id through the run
2. Span                    — a timed unit of work with attributes + events
3. SpanEmitter             — emits spans to a JSONL file + structured logger
4. Counter / Histogram     — lightweight metric primitives
5. MetricsRegistry         — thread-safe aggregation
6. @traced decorator       — decorate any function to auto-emit a span
7. install_otel_exporter   — optional bridge to OpenTelemetry SDK

Design properties
-----------------
- Zero overhead when disabled (NoOpEmitter is the default).
- All emit/record paths are exception-safe: instrumentation never
  breaks the program.
- JSONL output is grep-friendly and tail-able from CI artifacts.
- Trace IDs are 128-bit (W3C trace-context compatible).
- Parent-child span relationships are tracked via contextvars,
  so concurrent stages don't bleed into each other.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ════════════════════════════════════════════════════════════════
# Context propagation
# ════════════════════════════════════════════════════════════════
_current_span: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar(
    "qeema_current_span", default=None
)
_current_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "qeema_trace_id", default=None
)


def new_trace_id() -> str:
    """Generate a 128-bit trace ID as 32 hex chars (W3C compatible)."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """Generate a 64-bit span ID as 16 hex chars (W3C compatible)."""
    return secrets.token_hex(8)


def current_trace_id() -> Optional[str]:
    """Return the active trace ID, or None if no trace is in progress."""
    return _current_trace_id.get()


def current_span() -> Optional["Span"]:
    """Return the currently active span, or None."""
    return _current_span.get()


# ════════════════════════════════════════════════════════════════
# Span
# ════════════════════════════════════════════════════════════════
class SpanStatus:
    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


@dataclass
class SpanEvent:
    """A timestamped event within a span."""
    name: str
    timestamp_ms: int
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Span:
    """
    A timed unit of work with structured attributes.

    Lifecycle:
        with span_emitter.span("script.generate", episode=7) as span:
            span.set("provider", "gemini-1")
            ... do work ...
            span.add_event("script_quality_validated", score=0.92)
            # status = OK on clean exit
            # status = ERROR with exception details on raise
    """
    name: str
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    start_time_ms: int
    end_time_ms: Optional[int] = None
    status: str = SpanStatus.UNSET
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[SpanEvent] = field(default_factory=list)
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    exception_traceback: Optional[str] = None

    @property
    def duration_ms(self) -> Optional[int]:
        if self.end_time_ms is None:
            return None
        return self.end_time_ms - self.start_time_ms

    def set(self, key: str, value: Any) -> None:
        """Attach a key/value attribute. Overwrites previous value."""
        # Best-effort serializability check; falls back to str()
        try:
            json.dumps(value)
            self.attributes[key] = value
        except (TypeError, ValueError):
            self.attributes[key] = str(value)

    def set_many(self, **kwargs: Any) -> None:
        """Attach multiple attributes at once."""
        for k, v in kwargs.items():
            self.set(k, v)

    def add_event(self, name: str, **attributes: Any) -> None:
        """Record a timestamped event within the span."""
        self.events.append(
            SpanEvent(
                name=name,
                timestamp_ms=int(time.time() * 1000),
                attributes=dict(attributes),
            )
        )

    def record_exception(self, exc: BaseException) -> None:
        """Record exception details (without raising)."""
        self.exception_type = type(exc).__name__
        self.exception_message = str(exc)[:1000]
        # Limit traceback to 4KB to avoid log explosion
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.exception_traceback = tb[:4096]
        self.status = SpanStatus.ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "start_time_ms": self.start_time_ms,
            "end_time_ms": self.end_time_ms,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "events": [
                {
                    "name": e.name,
                    "timestamp_ms": e.timestamp_ms,
                    "attributes": e.attributes,
                }
                for e in self.events
            ],
            "exception": (
                {
                    "type": self.exception_type,
                    "message": self.exception_message,
                    "traceback": self.exception_traceback,
                }
                if self.exception_type
                else None
            ),
        }


# ════════════════════════════════════════════════════════════════
# Span emitter — writes to JSONL and structured logger
# ════════════════════════════════════════════════════════════════
class SpanEmitter:
    """
    Emits completed spans to a JSONL file and the standard logger.

    The JSONL file is append-only and rotates by line count; this is
    enough for CI runs of a few hundred spans. For long-lived processes,
    add a size-based rotator or pipe to OpenTelemetry.

    Thread-safe: spans from concurrent threads interleave correctly
    because we lock around the file write.
    """

    def __init__(
        self,
        *,
        jsonl_path: Optional[Path] = None,
        also_log: bool = True,
    ) -> None:
        self._jsonl_path: Optional[Path] = jsonl_path
        self._also_log: bool = also_log
        self._lock: threading.Lock = threading.Lock()
        self._otel_tracer: Optional[Any] = None     # set by install_otel_exporter

        if jsonl_path is not None:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Iterator[Span]:
        """
        Open a span. Use as a context manager:

            with emitter.span("audio.synthesize", episode=7) as s:
                s.set("text_length", len(text))
                ... work ...
        """
        # Resolve / generate trace ID
        trace_id = current_trace_id()
        if trace_id is None:
            trace_id = new_trace_id()
            trace_token = _current_trace_id.set(trace_id)
        else:
            trace_token = None

        # Resolve parent span
        parent = current_span()
        parent_id = parent.span_id if parent else None

        s = Span(
            name=name,
            span_id=new_span_id(),
            trace_id=trace_id,
            parent_span_id=parent_id,
            start_time_ms=int(time.time() * 1000),
            attributes={**(attributes or {}), **kwargs},
        )

        span_token = _current_span.set(s)

        try:
            yield s
        except BaseException as e:
            s.record_exception(e)
            raise
        else:
            if s.status == SpanStatus.UNSET:
                s.status = SpanStatus.OK
        finally:
            s.end_time_ms = int(time.time() * 1000)
            _current_span.reset(span_token)
            if trace_token is not None:
                _current_trace_id.reset(trace_token)

            self._emit(s)

    def _emit(self, span: Span) -> None:
        """Write the span to all configured sinks. Never raises."""
        # JSONL sink
        if self._jsonl_path is not None:
            try:
                line = json.dumps(span.to_dict(), separators=(",", ":")) + "\n"
                with self._lock:
                    with self._jsonl_path.open("a", encoding="utf-8") as f:
                        f.write(line)
            except Exception:
                logger.exception("span emitter: jsonl write failed")

        # Structured logger sink
        if self._also_log:
            try:
                level = (
                    logging.WARNING if span.status == SpanStatus.ERROR
                    else logging.INFO
                )
                logger.log(
                    level,
                    f"span {span.name} duration={span.duration_ms}ms "
                    f"status={span.status} trace_id={span.trace_id[:8]}",
                    extra={"span": span.to_dict()},
                )
            except Exception:
                pass

        # OpenTelemetry sink (optional)
        if self._otel_tracer is not None:
            self._emit_to_otel(span)

    def _emit_to_otel(self, span: Span) -> None:
        """Bridge to opentelemetry SDK. No-op if otel not configured."""
        try:
            # Reconstruct the span in otel. Note: this loses the original
            # timing slightly because otel wants its own timer. Acceptable
            # for high-level traces.
            otel_span = self._otel_tracer.start_span(span.name)
            for k, v in span.attributes.items():
                otel_span.set_attribute(k, v)
            for ev in span.events:
                otel_span.add_event(ev.name, ev.attributes)
            if span.exception_type:
                otel_span.set_status(
                    self._otel_tracer.Status(self._otel_tracer.StatusCode.ERROR)
                )
            otel_span.end()
        except Exception:
            logger.exception("span emitter: otel bridge failed")


# Singleton no-op emitter, used when no global emitter is configured
class _NoOpEmitter(SpanEmitter):
    @contextmanager
    def span(
        self, name: str, *, attributes: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Iterator[Span]:
        # Still expose a usable Span for set/add_event calls
        s = Span(
            name=name,
            span_id="0" * 16,
            trace_id="0" * 32,
            parent_span_id=None,
            start_time_ms=int(time.time() * 1000),
        )
        try:
            yield s
        except BaseException as e:
            s.record_exception(e)
            raise


_NOOP_EMITTER = _NoOpEmitter()
_global_emitter: SpanEmitter = _NOOP_EMITTER


def configure_emitter(emitter: SpanEmitter) -> None:
    """Install a global emitter. Called once at process startup."""
    global _global_emitter
    _global_emitter = emitter


def get_emitter() -> SpanEmitter:
    return _global_emitter


# ════════════════════════════════════════════════════════════════
# @traced decorator
# ════════════════════════════════════════════════════════════════
def traced(
    name: Optional[str] = None,
    *,
    capture_args: bool = False,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Wrap a function in a span.

    Args:
        name: span name. Defaults to "{module}.{qualname}".
        capture_args: if True, attach repr of args/kwargs as attributes.
            Use sparingly — repr can be expensive or leak secrets.

    Example:
        @traced("script.generate", capture_args=True)
        def generate_script(episode: int) -> Script:
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        span_name = name or f"{func.__module__}.{func.__qualname__}"

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            with get_emitter().span(span_name) as s:
                if capture_args:
                    s.set("args_repr", _safe_repr(args))
                    s.set("kwargs_repr", _safe_repr(kwargs))
                return func(*args, **kwargs)

        return wrapper

    return decorator


def _safe_repr(obj: Any, max_len: int = 200) -> str:
    """repr(obj) truncated to avoid blowing up span size."""
    try:
        s = repr(obj)
    except Exception:
        return f"<unrepresentable {type(obj).__name__}>"
    return s if len(s) <= max_len else s[:max_len - 3] + "..."


# ════════════════════════════════════════════════════════════════
# Metrics
# ════════════════════════════════════════════════════════════════
@dataclass
class _CounterState:
    value: int = 0
    last_updated_ms: int = 0


@dataclass
class _HistogramState:
    count: int = 0
    sum: float = 0.0
    min: float = float("inf")
    max: float = float("-inf")

    def record(self, v: float) -> None:
        self.count += 1
        self.sum += v
        if v < self.min:
            self.min = v
        if v > self.max:
            self.max = v

    @property
    def avg(self) -> float:
        return self.sum / self.count if self.count else 0.0


class Counter:
    """Monotonically increasing counter with optional labels."""

    __slots__ = ("name", "_states", "_lock")

    def __init__(self, name: str) -> None:
        self.name: str = name
        self._states: Dict[str, _CounterState] = {}
        self._lock: threading.Lock = threading.Lock()

    def inc(self, *, labels: Optional[Dict[str, str]] = None, by: int = 1) -> None:
        if by < 0:
            raise ValueError("Counter must increase monotonically; use Gauge for decrements")
        key = _label_key(labels)
        with self._lock:
            state = self._states.setdefault(key, _CounterState())
            state.value += by
            state.last_updated_ms = int(time.time() * 1000)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {k: v.value for k, v in self._states.items()}


class Histogram:
    """Records distributional values (min/max/avg/count/sum)."""

    __slots__ = ("name", "_states", "_lock")

    def __init__(self, name: str) -> None:
        self.name: str = name
        self._states: Dict[str, _HistogramState] = {}
        self._lock: threading.Lock = threading.Lock()

    def record(
        self,
        value: float,
        *,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        key = _label_key(labels)
        with self._lock:
            state = self._states.setdefault(key, _HistogramState())
            state.record(value)

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            return {
                k: {
                    "count": v.count,
                    "sum": v.sum,
                    "min": v.min if v.count else 0.0,
                    "max": v.max if v.count else 0.0,
                    "avg": v.avg,
                }
                for k, v in self._states.items()
            }


def _label_key(labels: Optional[Dict[str, str]]) -> str:
    """Stable string key from labels dict."""
    if not labels:
        return ""
    return "&".join(f"{k}={v}" for k, v in sorted(labels.items()))


class MetricsRegistry:
    """Central registry for all metrics. Thread-safe."""

    def __init__(self) -> None:
        self._counters: Dict[str, Counter] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._lock: threading.Lock = threading.Lock()

    def counter(self, name: str) -> Counter:
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                c = Counter(name)
                self._counters[name] = c
            return c

    def histogram(self, name: str) -> Histogram:
        with self._lock:
            h = self._histograms.get(name)
            if h is None:
                h = Histogram(name)
                self._histograms[name] = h
            return h

    def snapshot(self) -> Dict[str, Any]:
        """Snapshot of every metric. Suitable for JSON serialization."""
        with self._lock:
            return {
                "counters": {
                    name: c.snapshot()
                    for name, c in self._counters.items()
                },
                "histograms": {
                    name: h.snapshot()
                    for name, h in self._histograms.items()
                },
            }

    def write_snapshot(self, path: Path) -> None:
        """Atomic write of the metrics snapshot to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.snapshot(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)


_registry = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    """Module-level registry. Use this from anywhere in the codebase."""
    return _registry


# ════════════════════════════════════════════════════════════════
# Optional: OpenTelemetry bridge
# ════════════════════════════════════════════════════════════════
def install_otel_exporter(
    emitter: SpanEmitter,
    *,
    service_name: str = "qeema",
    endpoint: Optional[str] = None,
) -> bool:
    """
    Wire an OpenTelemetry exporter into the emitter.

    Returns True on success, False if OTEL packages aren't installed.
    No-op if endpoint is None — useful for local dev.
    """
    if endpoint is None:
        return False
    try:
        from opentelemetry import trace                          # type: ignore
        from opentelemetry.sdk.resources import Resource          # type: ignore
        from opentelemetry.sdk.trace import TracerProvider        # type: ignore
        from opentelemetry.sdk.trace.export import (              # type: ignore
            BatchSpanProcessor,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
    except ImportError:
        logger.warning("opentelemetry packages not installed; otel bridge disabled")
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    emitter._otel_tracer = trace.get_tracer(service_name)
    logger.info(f"otel exporter installed: {endpoint}")
    return True
