# QEEMA — Integration Guide for the v12 Hardening

This document tells you **exactly** how to wire the new modules into
the existing codebase. No magic. Each integration is small and
independent — apply them one at a time, run tests, deploy.

---

## What's in this commit

```
core/
  ffmpeg_args.py            ← type-safe FFmpeg argv (kills 256kk bug)
  lru_cache.py              ← bounded LRU file cache
  idempotency.py            ← keys + checkpoint store
  observability.py          ← spans, traces, metrics
  resilience_v2.py          ← thread-safe ProviderPool

infrastructure/
  ffmpeg_pro.py             ← run_ffmpeg with kill-tree, progress
  ffmpeg_assembler.py       ← refactored to use the above
  parallel_quran.py         ← concurrent Quran fetcher

tests/
  test_ffmpeg_args.py       ← 58 tests
  test_lru_cache.py         ← 15 tests
  test_idempotency.py       ← 19 tests
  test_resilience_v2.py     ← 15 tests
  test_observability.py     ← 24 tests
                            = 131 tests total, 100% passing

AUDIT.md                    ← honest senior-engineering audit
```

---

## Order of operations

Apply in this order — earlier steps unblock later ones:

1. **The FFmpeg fix is already applied** in `infrastructure/ffmpeg_assembler.py`.
   The pipeline will now run end-to-end. Verify with one CI run.

2. **Wire `ParallelQuranFetcher` into `voice_engine.py`** (15 minutes).

3. **Swap `ProviderPool` → `ProviderPoolV2`** in voice and script
   engines (15 minutes; API-compatible).

4. **Migrate caches to `BoundedLRUFileCache`** (1 hour; touches a few
   call sites).

5. **Add `SpanEmitter` to `main.py`** and wrap pipeline stages
   (30 minutes; pure addition, no behavioral change).

6. **Add `IdempotencyKey` + `CheckpointStore`** to the orchestrator
   (2 hours; this is the largest change).

---

## 1. ParallelQuranFetcher → voice_engine.py

**File:** `engines/voice_engine.py`, around line 463.

**Before:**
```python
# ── Quran fetches (sequential — they're already CDN-fast & cached)
for scene in script.ayah_scenes:
    sid = f"ayah_{scene.scene_id}"
    p = str(ep_dir / f"{sid}_recitation.mp3")
    self.fetch_quran(scene.ayah.surah, scene.ayah.number, p)
    audio_map[f"{sid}_ayah"] = p
    scene.ayah_audio = p
```

**After:**
```python
from infrastructure.parallel_quran import ParallelQuranFetcher
from core.interfaces import QuranAudioRequest

# In __init__, after self._reciter is created:
self._parallel_fetcher = ParallelQuranFetcher(
    fetch_fn=self._reciter.fetch,
    max_workers=self._engine_cfg.quran_parallel_workers,    # add to config; default 8
    fail_fast=False,
)

# In generate_episode_audio, replace the loop:
quran_requests = []
quran_request_to_scene = {}
for scene in script.ayah_scenes:
    sid = f"ayah_{scene.scene_id}"
    p = str(ep_dir / f"{sid}_recitation.mp3")
    req = QuranAudioRequest(
        surah=scene.ayah.surah,
        ayah=scene.ayah.number,
        output_path=p,
        reciter="alafasy",
    )
    quran_requests.append(req)
    quran_request_to_scene[p] = (sid, scene)

batch = self._parallel_fetcher.fetch_batch(quran_requests)
if batch.failures:
    failure_summary = "; ".join(
        f"{p}: {type(e).__name__}: {e}"
        for p, e in batch.failures.items()
    )
    raise QuranFetchError(
        surah=0, ayah=0,
        sources_tried=[],
        cause=Exception(f"{len(batch.failures)} ayahs failed: {failure_summary}")
    )

for path in batch.successes:
    sid, scene = quran_request_to_scene[path]
    audio_map[f"{sid}_ayah"] = path
    scene.ayah_audio = path
```

**Expected gain:** for سورة البقرة (286 ayahs), wall-clock for this stage
drops from ~3 min to ~25 seconds with 8 workers.

---

## 2. ProviderPoolV2 — drop-in swap

**Files:** `engines/script_engine.py`, `engines/voice_engine.py`,
the `_QuranFetcher` class.

The v2 API is identical to v1. Change one import line.

**Before:**
```python
from core.resilience import ProviderPool, CircuitBreakerConfig
```

**After:**
```python
from core.resilience_v2 import ProviderPoolV2 as ProviderPool
from core.resilience import CircuitBreakerConfig
```

**Important:** v2's `register()` parameter is `rate_limiter=` (an instance),
not `rate_limit=(rate, burst)` (a tuple). One call site needs updating
(in `voice_engine.py` line ~228):

**Before:**
```python
self._tts_pool.register(
    p.name,
    breaker_config=CircuitBreakerConfig(failure_threshold=4, recovery_timeout_sec=60.0),
    rate_limit=(1.5, 5),
)
```

**After:**
```python
from core.resilience import TokenBucketRateLimiter
self._tts_pool.register(
    p.name,
    breaker_config=CircuitBreakerConfig(failure_threshold=4, recovery_timeout_sec=60.0),
    rate_limiter=TokenBucketRateLimiter(1.5, 5),
)
```

After this swap:
- Concurrent `synthesize_batch` calls have correct success_rate metrics.
- `KeyError`/`AttributeError`/`TypeError` propagate immediately, exposing
  programmer bugs that v1 silently masked.
- Round-robin distribution is provably fair.

---

## 3. BoundedLRUFileCache — replace ad-hoc caches

**Files:** `engines/voice_engine.py` (TTS cache), `engines/visual_render_engine.py`
(scene cache).

**Pattern (TTS cache):**

```python
from core.lru_cache import BoundedLRUFileCache, make_cache_key

# In __init__:
self._tts_cache = BoundedLRUFileCache(
    root=paths.tts_cache,
    max_size_bytes=2 * 1024**3,        # 2 GiB
    max_age_seconds=14 * 86400,        # 2 weeks
    suffix=".mp3",
)

# In synthesize:
cache_key = make_cache_key(primary_voice, normalized_text)

if self._engine_cfg.voice_enable_cache:
    if (hit := self._tts_cache.get(cache_key)) is not None:
        shutil.copy(hit, output_path)
        return TTSResult(
            output_path=output_path,
            duration_sec=get_audio_duration(output_path),
            provider="cache",
            voice_id=primary_voice,
            cached=True,
        )

# ... synthesize ...

if self._engine_cfg.voice_enable_cache:
    self._tts_cache.put(cache_key, Path(output_path))
```

The cache will:
- Auto-evict LRU when over 2 GiB.
- Auto-evict entries older than 2 weeks.
- Persist across pipeline runs (rebuilds index on startup).
- Survive partial writes (atomic `.tmp → final`).

---

## 4. Observability — wire SpanEmitter into main.py

**File:** `main.py`, near the top of the run.

```python
from pathlib import Path
from core.observability import (
    SpanEmitter, configure_emitter, get_emitter, get_registry,
)

def _setup_observability(paths) -> SpanEmitter:
    emitter = SpanEmitter(
        jsonl_path=paths.logs / "spans.jsonl",
        also_log=True,
    )
    configure_emitter(emitter)
    return emitter

# In your main() or run():
emitter = _setup_observability(paths)

with emitter.span("episode.run", episode_number=ep_num):
    with emitter.span("script.generate"):
        script = script_engine.generate(...)
    with emitter.span("audio.generate"):
        audio_map = voice_engine.generate_episode_audio(...)
    with emitter.span("video.render"):
        renderer.render_all(...)
    # etc.

# At the end of the run, write metrics snapshot:
get_registry().write_snapshot(paths.logs / "metrics.json")
```

In CI, upload `logs/spans.jsonl` and `logs/metrics.json` as artifacts.
You now have full forensics for every run, even silent successes.

---

## 5. Idempotency — orchestrator changes

**File:** `orchestrator.py` (or wherever the per-episode run lives).

The minimal integration:

```python
from core.idempotency import (
    CheckpointStore, IdempotencyKey, run_idempotent,
)

# Setup
ckpt_store = CheckpointStore(root=paths.state / "checkpoints")

# Per episode
def run_episode(episode_number: int) -> None:
    # Compute key BEFORE any side effects
    key = IdempotencyKey.derive(
        episode_number=episode_number,
        pipeline_version=PIPELINE_VERSION,
        inputs={
            "voice_id": api_keys.elevenlabs_voice_id,
            "script_prompt_version": SCRIPT_PROMPT_VERSION,
            "video_resolution": f"{video_cfg.width}x{video_cfg.height}",
            "audio_bitrate": video_cfg.audio_bitrate,
        },
    )
    ckpt_store.initialize(key, episode_number=episode_number)

    @run_idempotent(ckpt_store, key, stage="script")
    def _script() -> dict:
        s = script_engine.generate(...)
        # Persist intermediate state somewhere durable (Supabase or disk)
        return {"script_id": s.id, "script_path": str(s.path)}

    @run_idempotent(ckpt_store, key, stage="audio")
    def _audio() -> dict:
        m = voice_engine.generate_episode_audio(...)
        return {"audio_map": m}

    @run_idempotent(ckpt_store, key, stage="render")
    def _render() -> dict:
        return {"video_path": str(renderer.render_all(...))}

    @run_idempotent(ckpt_store, key, stage="upload")
    def _upload() -> dict:
        result = uploader.upload(...)
        return {"video_id": result.video_id, "url": result.video_url}

    _script()
    _audio()
    _render()
    _upload()
```

After this:
- A pipeline crash mid-run, on restart, **skips** completed stages.
- A YouTube 5xx → retry will not produce duplicate uploads (the
  upload result is recorded after success).
- Operators can `cat checkpoints/ab/abc.../checkpoints.jsonl` to see
  exactly what was completed.

---

## What was deliberately NOT shipped in this commit

- **Full async migration.** Mixing sync and async is worse than either
  one alone. This needs to be its own 1–2 week effort with a clear
  migration plan (probably `asyncio` + `httpx` everywhere).

- **AI video generation (Runway/Flux/Kling).** The previous review's
  `ai_visual_engine.py` was a stub that raised `NotImplementedError`.
  Adding stubs is dishonest. Real integration requires:
  - A budget plan (Runway: $95/cycle × 52 cycles = $4,940/year).
  - A working procedural fallback (the current Three.js renderer is
    that fallback — improve it before adding AI providers).
  - A quality gate that rejects unusable AI output.

- **Auto-subtitles.** Word-level alignment requires either real
  ElevenLabs alignment API responses (which the project's TTS adapter
  doesn't currently parse) or `whisper` (requires GPU or paid API).
  Estimating timing from word counts produces visibly broken subtitles
  — worse than no subtitles.

- **Distributed tracing exporter.** The `install_otel_exporter()`
  function is in place but requires you to actually configure an
  OTLP endpoint (Jaeger, Tempo, or a hosted service). I left the
  bridge ready but didn't pick a backend for you.

---

## Verification

After applying any subset of these integrations:

```bash
# All tests must pass:
python -m pytest tests/ -v

# Quick smoke test of the pipeline:
python main.py --episode 1 --dry-run

# Inspect the trace from the dry run:
cat logs/spans.jsonl | jq .

# Inspect metrics:
cat logs/metrics.json | jq .
```

---

## Timeline estimate (realistic)

- Step 1 (FFmpeg fix): **already applied**, run 1 CI cycle.
- Step 2 (parallel Quran): **30 min** + 1 CI cycle to verify.
- Step 3 (ProviderPoolV2): **15 min** + 1 CI cycle.
- Step 4 (LRU caches): **1 hour** + careful integration testing.
- Step 5 (observability): **30 min**, additive only.
- Step 6 (idempotency): **2 hours** including testing replay scenarios.

**Total:** ~5 hours of focused work to apply everything.

You can ship Steps 1–3 today and Steps 4–6 incrementally over the
next week. Each step is independently valuable.
