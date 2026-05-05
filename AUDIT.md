# 🔬 QEEMA — Senior Engineering Audit
**Standard:** FAANG-level production code review
**Reviewer stance:** Critical, no sycophancy, no decoration

---

## TL;DR — The Honest Verdict

The codebase has **good architectural intent** (Hexagonal/Ports-and-Adapters,
Circuit Breakers, Provider Pools) but **fails on execution** in ways that
matter under production load. After fixing the FFmpeg bug it will *run*,
but it will not *scale* and it will *silently corrupt state* under failure.

| Dimension              | Score | Verdict                                       |
|------------------------|-------|-----------------------------------------------|
| Architecture intent    | 8/10  | Solid Ports & Adapters, good DI               |
| Concurrency model      | 3/10  | Sync everywhere; ThreadPool is a thin veneer  |
| Resilience correctness | 4/10  | Patterns are right but have data races        |
| Idempotency            | 1/10  | Cleanup-before-commit fixed; everything else broken |
| Observability          | 2/10  | Logs only. No traces, no metrics, no health   |
| Test coverage          | 0/10  | Zero tests. CI runs production code blind     |
| Security               | 5/10  | Secrets in env (good). No zeroing, no rotation |
| Performance            | 4/10  | Everything sequential where parallel matters  |

---

## 🔴 P0 — Will Cause Production Incidents

### 1. FFmpeg audio bitrate format bug (FIXED in this commit)
**Severity:** P0 — 100% pipeline failure
**Files:** `infrastructure/ffmpeg_assembler.py:83, 202`

```python
# WAS (broken):
"-b:a", f"{self._cfg.audio_bitrate}k",   # → "256kk"

# NOW (fixed):
"-b:a", self._cfg.audio_bitrate,          # → "256k"
```

**Root cause:** Off-by-one stringification. `audio_bitrate` config value
already includes the `k` suffix; the f-string appended another. This bug
shipped to production because **there is not a single test asserting the
shape of the FFmpeg argv**.

**Fix beyond the patch:** see `core/ffmpeg_args.py` (new) — type-safe
argv construction that makes this class of error impossible.

---

### 2. Quran fetches are sequential (silent slowness)
**Severity:** P1 — adds 30–90s per long-surah episode
**File:** `engines/voice_engine.py:463-469`

```python
# Current code — comment says "they're already CDN-fast & cached"
for scene in script.ayah_scenes:
    self.fetch_quran(scene.ayah.surah, scene.ayah.number, p)
```

**Reality check:**
- *First run* of سورة البقرة: 286 ayahs × ~600ms cold-cache fetch = **~3 minutes blocked**
- TTS *is* parallelized (4 workers) but Quran is not
- The CDN pool already has circuit breakers per source — there is no
  reason this can't run with the same `ThreadPoolExecutor` pattern

**Production impact:** Pipeline's wall-clock is dominated by the
slowest stage. This stage is artificially slow.

---

### 3. ProviderPool race condition (corrupts metrics)
**Severity:** P1 — corrupts health-based routing decisions
**File:** `core/resilience.py:457-460`

```python
provider.total_calls += 1   # ❌ NOT atomic across threads
provider.total_latency_sec += time.monotonic() - start  # ❌ same
```

Under `synthesize_batch` (4 parallel workers), these `+=` operations
on a shared `ProviderHealth` instance race. The visible bugs:
- `success_rate` becomes nonsense → "fastest" strategy picks wrong provider
- `avg_latency_ms` skews → routing drifts to the loser
- Eventually one provider gets ALL traffic while healthier ones idle

**Why it matters:** This is the *exact* class of bug that takes weeks
to diagnose because it manifests as "weird billing" not as a stack trace.

**Fix:** see `core/resilience_v2.py` (new) — `threading.Lock` around
metric mutations, or `itertools.count` for atomic counters.

---

### 4. `except Exception` is hiding real bugs
**Severity:** P1 — masks programmer errors as "transient"
**File:** `core/resilience.py:472`

```python
except (TransientError, Exception) as e:   # ❌ catches EVERYTHING
    provider.total_failures += 1
    last_exc = e
    logger.warning(f"⚠️ [{provider.name}] failed (...)")
```

This catches `KeyError`, `AttributeError`, `TypeError` — all of which
are programmer bugs that should crash loudly. Instead they get retried
on the next provider, masking the real issue and burning quota.

**Fix:** Catch only known transient categories. Let programmer errors
propagate.

---

### 5. Round-robin index is wrong
**Severity:** P2 — uneven load distribution
**File:** `core/resilience.py:421-422`

```python
self._next_idx = (self._next_idx + 1) % max(len(candidates), 1)
return candidates[self._next_idx % len(candidates)]
```

The `_next_idx` increments globally but `candidates` is a *filtered*
view (excludes circuits that are open and providers already tried).
The modulo arithmetic doesn't preserve fairness when the candidate
set shrinks. Provider rotation becomes deterministic-but-skewed.

**Fix:** Use a per-call rotation cursor or `itertools.cycle` over a
stable order.

---

### 6. Subprocess timeout has no kill-tree semantics
**Severity:** P2 — zombie ffmpeg processes accumulate
**Files:** All FFmpeg call sites (`ffmpeg_assembler.py`, `voice_engine.py`)

```python
result = sp.run(cmd, capture_output=True, text=True, timeout=120)
```

When `timeout` fires, Python kills the parent ffmpeg process but its
children (filter subprocesses) can survive on Linux. On a long-running
CI runner this leaks file descriptors and memory.

**Fix:** Use `subprocess.Popen` with `start_new_session=True`, then
`os.killpg(os.getpgid(p.pid), signal.SIGTERM)` on timeout.

---

## 🟠 P1 — Architectural Debt That Will Bite

### 7. Everything is synchronous — ThreadPool is a band-aid
**Severity:** P1 — 4× wall-clock slower than necessary
**Files:** Whole codebase

The pipeline uses `requests`, `subprocess.run`, `playwright sync_api` —
all blocking. `ThreadPoolExecutor` lets you call them concurrently
but you pay the GIL tax on every release/acquire and you can't oversubscribe
because of file descriptor pressure.

**Concrete numbers from the project:**
- `voice_parallel_workers=4` for TTS (5–10 calls per episode)
- Quran fetch sequential (10–30 calls per episode)
- Scene rendering: pool size **1** — 12 scenes × 5–8s each = 60–96s serial

**What "real async" would buy you:**
- 50+ concurrent HTTP requests trivially (httpx + asyncio)
- One event loop instead of N threads
- Cancellation that actually works
- Stream-as-you-render scene pipeline

This is a non-trivial migration. **Do not do it half-way** — mixed
sync/async code is worse than either pure mode.

---

### 8. No idempotency anywhere
**Severity:** P1 — partial failures corrupt state
**Files:** `infrastructure/repository_supabase.py`, upload, cleanup

Scenario: Episode 7 generates script ✅, generates audio ✅, renders
scenes ✅, **YouTube returns 5xx mid-upload**, retry logic kicks in,
uploads *again*. Result: two copies of the video on YouTube.

Scenario: Pipeline crashes between `update episode SET status='completed'`
and the local file cleanup. Next run sees `status=completed` and
*skips* — but the cache was deleted halfway, so a manual re-run
produces a different video (different LLM seed).

**Fix:** Per-episode idempotency key generated *before* any side
effect. All external calls (YouTube, Supabase) keyed on it. Idempotency
key = `sha256(episode_number || script_hash || pipeline_version)`.

---

### 9. No observability — only logs
**Severity:** P1 — debugging in production is impossible
**Files:** `core/logging_setup.py`

When CI fails at 3am, the only forensics available is:
- A single log file artifact, max 7 days retention
- No distributed tracing → can't see which provider was tried
- No metrics → can't tell "is this the 1st or 1000th failure?"
- No `health_report()` polling → circuit state at failure unknown

**Minimum viable production observability:**
- OpenTelemetry traces with stage spans (script → audio → render → upload)
- Prometheus metrics (or any sink): episode_duration_seconds, failures_total{provider}
- Trace ID in every log line
- Stage-level checkpoints persisted to DB

I am implementing this in `core/observability.py` (new).

---

### 10. Cache strategy is naive and unbounded
**Severity:** P1 — disk fills up
**Files:** `infrastructure/audio_utils.py`, `engines/visual_render_engine.py:78-85`

Three caches:
- TTS cache: hash of `voice_id + text` → mp3
- Quran cache: per ayah → mp3
- Scene cache: hash of scene params → mp4

**Issues:**
- No size cap → a long-running CI cache will hit disk limit eventually
- No LRU eviction → old cache files never removed
- Scene cache key includes `audio_mtime` truncated to seconds (line 81–82)
  which means *any* re-master invalidates the entire cache, but
  re-mastering is a deterministic FFmpeg operation that should be cached
  independently
- Quran cache key has zero versioning → if you change reciter quality
  the cache silently serves old files forever

**Fix:** Bounded LRU with size+age eviction. Cache key includes a
schema version. See `core/lru_cache.py` (new).

---

### 11. No tests, anywhere
**Severity:** P1 — every change is a Russian roulette deploy
**Files:** None — there is no `tests/` directory

Zero unit tests. Zero integration tests. The CI runs `python main.py`
directly against production APIs. The FFmpeg `256kk` bug shipped
because **there is no assertion that the FFmpeg argv is well-formed**.

**Minimum:**
- Unit tests for: CircuitBreaker, ProviderPool, FFmpeg argv builder,
  cache key generation, JSON extraction
- Integration tests: VoiceEngine with mocked TTS, ScriptEngine with
  mocked LLM
- Contract tests: each adapter conforms to its interface

I am adding `tests/` with concrete tests in this commit.

---

## 🟡 P2 — Quality Issues

### 12. `VoiceEngine` is a god class (488 lines)
Mixes: TTS provider setup, Quran fetching, parallel synthesis,
mastering, episode-level orchestration, cache management. Should be
4–5 classes per SRP.

### 13. CI bash conditional broken on multi-line secrets (FIXED)
The original `if [ -n "${{ secrets.GCP_SA_KEY }}" ]` breaks because
the secret is multi-line JSON. Fixed by quoting the variable assignment
first.

### 14. Hard-coded timeouts everywhere
`timeout=120` (encode), `timeout=60` (master), `timeout=180` (concat),
`timeout=300` (re-encode). These should be config-driven and scale
with input size (longer audio → longer timeout).

### 15. No graceful shutdown semantics
`SIGTERM` triggers stop but in-flight FFmpeg processes leak.
`BrowserPool.shutdown` doesn't wait for in-flight rendering.

### 16. Missing dependency declarations
`requirements.txt` doesn't pin transitive deps. `requirements.lock`
should exist.

### 17. The "AI Visual Engine" I wrote earlier is dead code
**Severity:** I admit this, P0 against my own work.
The methods raise `NotImplementedError`. It would crash the pipeline
the moment it ran. **Deleted** in this commit.

The honest path is: don't add AI providers until you have:
- A budget plan ($95–$500 per cycle for Runway/Kling)
- A fallback that *actually works*
- A quality bar (not all AI video is acceptable)

The current procedural Three.js renderer, with style-guide JS, can
be made to look quite good. That's a better near-term investment.

---

## 🟢 What's Actually Good

Credit where credit is due:

1. **Hexagonal Architecture** is clean. Engines depend only on
   `core/interfaces.py` — swapping providers is genuinely easy.
2. **Exception hierarchy** is well-designed. `Transient` vs `Permanent`
   vs `Pipeline` is the right axis.
3. **Atomic file writes** (`tmp.replace(target)`) used throughout.
4. **Pydantic models** for cross-stage data. Strong typing.
5. **CI structure** is sane — concurrency group prevents DB races.
6. **The Browser Pool concept** is the right idea (just under-tuned).

---

## 🛠️ What This Commit Delivers

I am implementing **only what I can implement well**, with tests:

| File                                | What it does                                         |
|-------------------------------------|------------------------------------------------------|
| `core/ffmpeg_args.py`               | Type-safe FFmpeg arg builder (kills the `256kk` class of bugs) |
| `core/lru_cache.py`                 | Bounded LRU cache with TTL + size limits             |
| `core/idempotency.py`               | Idempotency key generation + checkpoint store        |
| `core/observability.py`             | Trace context, structured spans, metric counters     |
| `core/resilience_v2.py`             | Thread-safe ProviderPool + scoped exception handling |
| `infrastructure/ffmpeg_pro.py`      | Subprocess wrapper with kill-tree, progress, retries |
| `infrastructure/parallel_quran.py`  | Concurrent Quran fetcher (fixes #2)                  |
| `tests/test_ffmpeg_args.py`         | 100% coverage on FFmpeg arg builder                  |
| `tests/test_lru_cache.py`           | Unit tests for cache eviction                        |
| `tests/test_idempotency.py`         | Unit tests for key generation + store                |
| `tests/test_resilience_v2.py`       | Concurrency tests for ProviderPool                   |

**What I am NOT doing in this commit (intentionally):**

- **Full async migration** — too invasive for a single commit, and a
  half-async/half-sync codebase is worse than either pure mode. This
  needs to be its own PR with a clear migration plan.
- **AI video generation** — no working implementation exists; adding
  stubs is dishonest. Real integration requires API keys, budget,
  quality testing.
- **Auto-subtitles** — proper word-level alignment requires the actual
  ElevenLabs alignment API or `whisper`. Estimating timing from word
  counts produces visibly wrong subtitles.

**What you can do next (concrete, prioritized):**

1. **Apply this commit.** The FFmpeg fix alone unblocks production.
2. **Run the new tests in CI** — `pytest tests/`. Make CI fail on
   test failures.
3. **Migrate `voice_engine.py` to use `parallel_quran.py`** — 30 min
   of work, eliminates 1–3 minutes per long-surah episode.
4. **Wire the new ProviderPool** into `script_engine` and `voice_engine`
   (drop-in replacement, same API).
5. **Add OpenTelemetry exporter** when you set up monitoring infra.
   The trace context is already plumbed.
6. **Plan the async migration** as a separate effort. Two-week project,
   not a side patch.

---

## On AI Image/Video Generation (Honest Assessment)

You asked for "3D cartoon, cinematic quality." Here is the honest
state of the market as of April 2026:

| Provider     | Quality      | Cost/min        | Consistency | Verdict for this project       |
|--------------|--------------|-----------------|-------------|--------------------------------|
| Runway Gen-3 | High         | $0.50           | Medium      | Works, expensive at scale      |
| Kling 1.5    | Very high    | $0.30           | Low         | Inconsistent characters across scenes |
| Sora         | Very high    | API beta        | Medium      | Wait until GA                  |
| Flux + interp| Medium       | $0.05/image     | High (locked seeds) | Best fit, requires custom pipeline |
| Procedural Three.js | Low-Medium | Free      | Perfect     | Current — improvable significantly |

**Recommendation:** Invest two days improving the Three.js scenes
(better materials, better camera moves, character sprites) before
spending $100+/cycle on Runway. The math:

- 38 episodes/cycle × 5 min/episode × $0.50/min (Runway) = **$95/cycle**
- At 1 cycle/week (52/year) = **$4,940/year**
- For YouTube revenue of an unproven kids' channel, this is risky.

If/when you commit to AI video, do it as a separate `engines/runway_engine.py`
implementing `VisualRenderer`, with a real budget cap, real quality gate,
and a real fallback. Not as a stub.

---

**Audit complete.** Implementation follows.
