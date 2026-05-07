"""
core/idempotency.py — VALUE / QEEMA v22.5 — idempotency keys + checkpoint store
==============================================================
Why this exists
---------------
Without idempotency, a partial failure produces *exactly* this scenario:

    Episode 7:
      script ✅
      audio  ✅
      render ✅
      upload ❌ (HTTP 503 mid-stream)
    → retry kicks in → upload again → 2 copies on YouTube.

Or worse:

    Episode 7:
      script   ✅ → cached
      audio    ✅ → cached
      render   ✅ → cached
      upload   ✅ → video_id stored
      cleanup  ❌ (process killed mid-cleanup)
    → next run sees status='completed', skips
    → operator manually re-runs to "fix"
    → script regenerates with different LLM seed
    → uploaded again, different content

This module provides two primitives:

1. `IdempotencyKey` — a deterministic, content-addressed key for an
   episode run. Same inputs → same key, every time. Different LLM seeds,
   different prompts, different versions all produce different keys.

2. `CheckpointStore` — a small, durable per-episode log of
   "stage X completed with output Y". Stages are skipped on replay
   if their checkpoint exists.

Both are designed to be embarrassingly simple and operate on the
local filesystem. Persisting to Supabase is a separate concern
(implemented as a `RemoteCheckpointStore` adapter).

Defensive design choices
------------------------
- All writes are atomic (write to .tmp, rename).
- Reads tolerate partially-written files (treat as missing).
- Keys are versioned: changing the keying scheme bumps `KEY_SCHEMA_VERSION`
  to force a clean slate.
- Checkpoint timestamps are monotonic-friendly (use UTC ISO8601).
- Never raises on read failure; returns None and logs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


KEY_SCHEMA_VERSION: int = 1


# ════════════════════════════════════════════════════════════════
# Idempotency key generation
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """
    A content-addressed idempotency key.

    Contract:
      Same inputs → identical key, byte for byte.
      Any input change → different key.
      Pipeline version change → different key (forces re-derivation).

    `value` is 32 hex chars (128-bit truncated SHA-256).
    """
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 32 or not all(c in "0123456789abcdef" for c in self.value):
            raise ValueError(f"Invalid IdempotencyKey: {self.value!r}")

    def __str__(self) -> str:
        return self.value

    @classmethod
    def derive(
        cls,
        *,
        episode_number: int,
        pipeline_version: str,
        inputs: Mapping[str, Any],
    ) -> "IdempotencyKey":
        """
        Derive a key from episode + version + arbitrary inputs.

        `inputs` is canonicalized via JSON sort_keys + separators;
        floats are formatted to 6 decimals to avoid representation
        nondeterminism.

        Example:
            key = IdempotencyKey.derive(
                episode_number=7,
                pipeline_version="11.0.3",
                inputs={
                    "script_prompt_hash": "abc...",
                    "voice_id": "UR97...",
                    "video_resolution": "1920x1080",
                },
            )
        """
        if episode_number <= 0:
            raise ValueError(f"episode_number must be positive, got {episode_number}")
        if not pipeline_version:
            raise ValueError("pipeline_version must be non-empty")

        canonical = _canonical_json({
            "v": KEY_SCHEMA_VERSION,
            "ep": episode_number,
            "pv": pipeline_version,
            "in": dict(sorted(inputs.items())),
        })

        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return cls(value=digest)


def _canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, deterministic floats."""
    def _norm(x: Any) -> Any:
        if isinstance(x, float):
            # 6 decimals is enough for our use cases; avoids
            # platform-dependent repr differences.
            return f"{x:.6f}"
        if isinstance(x, dict):
            return {k: _norm(v) for k, v in sorted(x.items())}
        if isinstance(x, (list, tuple)):
            return [_norm(v) for v in x]
        return x

    return json.dumps(_norm(obj), sort_keys=True, separators=(",", ":"))


# ════════════════════════════════════════════════════════════════
# Checkpoint store
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A single stage completion record. Immutable once written."""
    stage: str
    completed_at: str       # ISO8601 UTC
    duration_ms: int
    output: Dict[str, Any] = field(default_factory=dict)


class CheckpointStore:
    """
    Per-key checkpoint log on local filesystem.

    Layout:
        root/
          ab/
            ab1234.../
              checkpoints.jsonl   (one Checkpoint per line)
              meta.json           (key, episode, created_at)

    JSONL format chosen for:
      - O(1) append (no full-file rewrite)
      - Crash-safe: a partially-written line just gets skipped on read
      - Human-debuggable: `cat checkpoints.jsonl`

    Thread-safe via per-instance RLock. For multi-process safety,
    use a remote store (see RemoteCheckpointStore in repository_supabase).
    """

    _CHECKPOINTS_FILE = "checkpoints.jsonl"
    _META_FILE = "meta.json"

    def __init__(self, root: Path) -> None:
        self._root: Path = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock: threading.RLock = threading.RLock()

    # ───────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────
    def initialize(
        self,
        key: IdempotencyKey,
        *,
        episode_number: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Create the directory + meta.json if they don't already exist.
        Idempotent: safe to call multiple times.
        """
        with self._lock:
            self._dir(key).mkdir(parents=True, exist_ok=True)
            meta_path = self._meta_path(key)
            if meta_path.exists():
                return
            meta = {
                "key": key.value,
                "episode_number": episode_number,
                "created_at": _now_iso(),
                "extra": metadata or {},
            }
            _atomic_write_json(meta_path, meta)

    def record(
        self,
        key: IdempotencyKey,
        *,
        stage: str,
        duration_ms: int,
        output: Optional[Dict[str, Any]] = None,
    ) -> Checkpoint:
        """
        Append a checkpoint. Returns the recorded value.

        Append is *not* idempotent on its own — caller is expected to
        check `is_completed(key, stage)` first if duplicate avoidance
        is needed (see `run_idempotent` helper below).
        """
        ckpt = Checkpoint(
            stage=stage,
            completed_at=_now_iso(),
            duration_ms=duration_ms,
            output=output or {},
        )
        line = json.dumps(asdict(ckpt), separators=(",", ":")) + "\n"
        path = self._checkpoints_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            # Append is atomic on POSIX for writes < PIPE_BUF (4096 bytes).
            # Our lines are well below that.
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return ckpt

    def list_completed(self, key: IdempotencyKey) -> List[Checkpoint]:
        """Return all checkpoints for a key, in order written."""
        path = self._checkpoints_path(key)
        if not path.exists():
            return []
        results: List[Checkpoint] = []
        with self._lock:
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            results.append(Checkpoint(**data))
                        except (json.JSONDecodeError, TypeError) as e:
                            # Tolerate partial / corrupted lines
                            logger.warning(f"checkpoint: skipping malformed line: {e}")
                            continue
            except OSError as e:
                logger.warning(f"checkpoint: read failed: {e}")
                return []
        return results

    def is_completed(self, key: IdempotencyKey, stage: str) -> bool:
        """True if a checkpoint with this stage name exists for this key."""
        return any(c.stage == stage for c in self.list_completed(key))

    def get_output(
        self, key: IdempotencyKey, stage: str
    ) -> Optional[Dict[str, Any]]:
        """Return the recorded output for the *most recent* matching stage."""
        for c in reversed(self.list_completed(key)):
            if c.stage == stage:
                return dict(c.output)
        return None

    def purge(self, key: IdempotencyKey) -> bool:
        """Remove all state for a key. Returns True if anything existed."""
        d = self._dir(key)
        if not d.exists():
            return False
        with self._lock:
            for child in d.rglob("*"):
                if child.is_file():
                    child.unlink(missing_ok=True)
            try:
                d.rmdir()
            except OSError:
                pass
        return True

    # ───────────────────────────────────────────────────────────
    # Helpers
    # ───────────────────────────────────────────────────────────
    def _dir(self, key: IdempotencyKey) -> Path:
        return self._root / key.value[:2] / key.value

    def _checkpoints_path(self, key: IdempotencyKey) -> Path:
        return self._dir(key) / self._CHECKPOINTS_FILE

    def _meta_path(self, key: IdempotencyKey) -> Path:
        return self._dir(key) / self._META_FILE


# ════════════════════════════════════════════════════════════════
# Convenience: run_idempotent decorator
# ════════════════════════════════════════════════════════════════
def run_idempotent(
    store: CheckpointStore,
    key: IdempotencyKey,
    *,
    stage: str,
):
    """
    Decorator factory: skip a stage if its checkpoint already exists.

    Usage:
        @run_idempotent(store, key, stage="render_scenes")
        def render():
            ...
            return {"output_path": "..."}

        result = render()  # runs first time; returns cached output thereafter

    The wrapped function should return a JSON-serializable dict;
    that dict is recorded as the checkpoint output.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            existing = store.get_output(key, stage)
            if existing is not None:
                logger.info(
                    f"⏭️  {stage}: checkpoint exists (key={key.value[:8]}...), skipping"
                )
                return existing

            t0 = time.monotonic()
            result = func(*args, **kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)

            if not isinstance(result, dict):
                raise TypeError(
                    f"@run_idempotent expects dict return, got {type(result).__name__}"
                )

            store.record(key, stage=stage, duration_ms=duration_ms, output=result)
            logger.info(
                f"✅ {stage}: completed in {duration_ms}ms (key={key.value[:8]}...)"
            )
            return result
        return wrapper
    return decorator


# ════════════════════════════════════════════════════════════════
# Internal utilities
# ════════════════════════════════════════════════════════════════
def _now_iso() -> str:
    """UTC ISO8601 with millisecond precision."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically via tempfile + os.replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
