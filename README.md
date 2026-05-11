# قِيمَة · VALUE — v22.7

> Quranic Children's Content Pipeline — Production-grade automation for
> generating educational YouTube videos from Juz Amma. Egyptian Arabic
> dialect, religiously validated, fully unattended on GitHub Actions.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Architecture](https://img.shields.io/badge/architecture-hexagonal-green.svg)](#architecture)
[![Pipeline](https://img.shields.io/badge/pipeline-3--day%20phased-orange.svg)](#3-day-phase-pipeline)
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()

---

## Overview

**QEEMA** automates a complete YouTube video pipeline for children's Quranic
content. The current production version (v22.7) runs as a **3-day phased
pipeline** designed to fit comfortably inside free-tier API quotas:

1. **Phase 1 (Day 1):** generate script in Egyptian Arabic + per-ayah
   religious validation via Gemini reviewer
2. **Phase 2 (Day 2):** synthesize narration (ElevenLabs), generate deep
   visual prompts + SSML TTS direction, fetch Quran recitation from CDN,
   master audio, then **upload all assets to Supabase Storage** so Day 3
   can run on a different runner
3. **Phase 3 (Day 3):** download assets, render every scene procedurally
   (HTML/CSS/Three.js + Playwright), burn subtitles, wrap branded
   intro/outro, generate three thumbnail variants, and upload to YouTube

The pipeline is designed to run unattended on GitHub Actions and produces a
complete 38-episode series of Juz Amma surahs.

---

## What changed since v11

The pipeline has gone through ten major iterations since v11. The big themes:

- **Free-tier first**: the entire pipeline now runs inside Gemini's free
  quotas (3 keys × 20/day = 60 calls/day), ElevenLabs' free 30k chars/month,
  and Leonardo's free 150 tokens/day. No Anthropic or paid Groq dependency.
- **3-day phase split**: Gemini-heavy planning runs on Day 1, asset
  generation on Day 2, rendering + upload on Day 3 — three separate workflow
  runs that each fit in a runner timeout.
- **Cross-runner persistence (v22.7)**: Phase 2 uploads its temp/ directory
  to a Supabase Storage bucket; Phase 3 downloads on a fresh runner before
  rendering. Without this, Phase 3 used to crash with "Audio missing".
- **Religious validation**: every ayah's explanation + analogy passes a
  Gemini reviewer with a Pydantic-schema-enforced JSON output. Confidence
  < 0.65 or any "forbidden analogy" hit fails the episode.
- **Multi-task script generation**: 7 ayahs in one Gemini call (down from 7
  separate calls) with response_schema validation.
- **Per-segment cinematic structure**: every ayah becomes 5 sub-segments
  (hook → analogy → recitation → explain → moral), each with its own
  emotion-aware TTS settings and color grade.
- **Review gate**: configurable threshold (default: episodes ≤ 10 need
  manual approval). Set `QEEMA_AUTO_APPROVE=true` in workflow env to bypass.

---

## Architecture

The codebase follows **Hexagonal Architecture (Ports & Adapters)** with a
phase router layered on top of the legacy orchestrator:

```
┌──────────────────────────────────────────────────────────────────────┐
│                           main.py                                    │
│   (composition root + signal handling + AssetStorage wiring)         │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                ┌────────────────▼─────────────────┐
                │       core/phase_router.py       │  ← Day 1 / 2 / 3
                │  (decides what to run today,     │     dispatcher
                │   persists state between days)   │
                └────────────────┬─────────────────┘
                                 │
                ┌────────────────▼─────────────────┐
                │         orchestrator.py          │  ← per-phase
                │       (phase-aware stages)       │     stage runner
                └────────────────┬─────────────────┘
                                 │
       ┌─────────────────────────┼─────────────────────────┐
       │                         │                         │
   ┌───▼─────────┐    ┌──────────▼──────────┐    ┌─────────▼────────┐
   │  engines/   │    │       core/         │    │ infrastructure/  │
   │             │    │  (interfaces,       │    │   (adapters)     │
   │ • script    │    │   models,           │    │ • llm            │
   │ • voice     │    │   exceptions,       │    │ • tts            │
   │ • visual    │    │   resilience,       │    │ • quran cdn      │
   │ • intro/out │    │   pipeline strategy,│    │ • supabase repo  │
   │ • thumbnail │    │   quota manager,    │    │ • supabase store │
   │ • quality   │    │   phase state,      │    │ • youtube        │
   │ • tafsir    │    │   stage retry,      │    │ • ffmpeg         │
   │ • leonardo  │    │   config)           │    │ • browser pool   │
   │ • subtitles │    │                     │    │ • bgm mixer      │
   └─────────────┘    └─────────────────────┘    └──────────────────┘
```

**Why Hexagonal?**
- Each engine depends only on `core/` interfaces
- Replacing ElevenLabs with CambAI = swap one adapter (already wired as fallback)
- All resilience patterns (retry, circuit breaker, rate limiting) live in `core/`

---

## 3-Day Phase Pipeline

The biggest architectural change since v11. Each episode is split across three
GitHub Actions runs on three consecutive days. State persists in
`state/phases/episode_NNN.json` (cached via GitHub Actions cache) and physical
assets persist in a Supabase Storage bucket.

### Phase 1 — Day 1: Planning (Key 1)

Gemini-heavy planning entirely on API key #1. Budget: ~14 calls / 4 RPM.

| Stage | What it does |
|---|---|
| `script` | Multi-task generation → 7 ayahs in 1 Gemini call (batch path) with response_schema |
| `tafsir_validation` | Per-ayah religious review against fetched tafsir text; deterministic forbidden-analogy detector layered on top |

Output: full episode JSON cached to disk. No Leonardo tokens spent, no
ElevenLabs chars spent. Status moves to `script_ready`.

### Phase 2 — Day 2: Assets (Key 2 + Key 3)

Two dedicated Gemini keys, plus Leonardo + ElevenLabs spend.

| Stage | What it does |
|---|---|
| `deep_visuals` | Batch (1 Gemini call on Key 3) → 7 cinematic prompts with subject/action/environment/lighting/composition. Falls back to chained 3-layer generator if batch fails. |
| `tts_director` | Batch (1 Gemini call on Key 2) → 21 per-segment SSML directions (hook/story/moral × 7 ayahs). Legacy path falls back. |
| `ai_images` | Leonardo generation per scene (when quota available). |
| `audio` | ElevenLabs TTS (intro/outro/CTA + 21 ayah segments + Quran fetch from CDN pool) |
| `audio_master` | ffmpeg loudnorm + fades → m4a outputs |
| **`storage_upload`** | **v22.7: upload every audio + image file to `episode-artifacts/episode_NNN/`** |

Output: 60-80 files persisted to Supabase Storage. Status moves to `assets_ready`.

### Phase 3 — Day 3: Render + Publish (No Gemini)

No LLM calls. Pure ffmpeg + Playwright work, plus YouTube.

| Stage | What it does |
|---|---|
| **`storage_download`** | **v22.7: download Phase 2's assets from Supabase to local temp/** |
| `render_scenes` | 32 cinematic segments (intro + 7 × 5-segment ayahs + outro), all aggressively cached by content hash |
| `concat_raw` | Stream-copy concat with mood-aware crossfades when emotion changes |
| `bgm_mix` | Per-segment volume curve (low during recitation, higher during hook) |
| `subtitles` | ASS subtitle generation + burn-in (Arabic typography validated) |
| `wrap_branded` | Concat intro.mp4 + episode + outro.mp4 |
| `thumbnail_variants` | Three PIL-rendered thumbnails for YouTube A/B testing |
| `review_gate` | Block upload for episodes ≤ threshold (configurable, bypassable via `QEEMA_AUTO_APPROVE=true`) |
| `upload` | YouTube resumable upload + thumbnail variants via Test & Compare API |

**Total per episode across 3 days: ~30 minutes of runner time.** Phase 3
alone is 10-15 min the first time, 3-5 min on a re-run (everything cached).

---

## Module Map

```
qeema/
├── core/
│   ├── config.py              # AppConfig, APIKeys, Paths, Video, Audio, Quota
│   ├── exceptions.py          # QeemaError + subclasses (Transient/Permanent/Quality)
│   ├── interfaces.py          # Abstract ports (LLM, TTS, Repo, Renderer, …)
│   ├── models.py              # Pydantic v2: EpisodeScript, AyahScene, EpisodePhase
│   ├── resilience.py          # RetryPolicy, CircuitBreaker, ProviderPool
│   ├── gemini_rate_limiter.py # 4-RPM sliding window, per-key, lock-protected
│   ├── stage_retry.py         # Per-stage retry policy (transient vs non-retryable)
│   ├── pipeline_strategy.py   # HIGH/BALANCED/ECONOMY mode auto-selection
│   ├── quota_manager.py       # Monthly budget for ElevenLabs/Leonardo/CambAI
│   ├── phase_state.py         # ⭐ Per-episode 3-day state (cached on Actions)
│   ├── phase_router.py        # ⭐ Day-of-week → phase dispatcher
│   ├── cost_tracker.py        # Per-stage cost log
│   ├── cost_dashboard.py      # Markdown reports per episode + per month
│   ├── observability.py       # JSON spans → logs/spans.jsonl
│   ├── tafsir_cache.py        # On-disk cache for tafsir API responses
│   └── logging_setup.py       # JSON logs + ContextLogAdapter
│
├── data/
│   └── curriculum.py          # 38 surahs (Juz Amma + Al-Fatiha)
│
├── infrastructure/
│   ├── llm_adapters.py            # GeminiJsonAdapter with response_schema
│   ├── tts_providers.py           # ElevenLabsProvider, CambAIProvider, GoogleTTSProvider
│   ├── quran_sources.py           # 4 CDN sources (Alafasy × 3, Husary)
│   ├── parallel_quran.py          # Concurrent ayah fetch with per-source pool
│   ├── tafsir_api.py              # Combined tafsir text fetcher
│   ├── repository_supabase.py     # Episode CRUD + status transitions
│   ├── repository_local.py        # JSON-file repo for --skip-supabase dev mode
│   ├── youtube_uploader.py        # OAuth refresh + resumable upload + thumbnail variants
│   ├── ffmpeg_assembler.py        # Single-encode + stream-copy concat
│   ├── browser_pool.py            # Pool of Chromiums (Playwright) for scene rendering
│   ├── bgm_mixer.py               # Volume curve + crossfades + subtitle burn-in
│   ├── bgm_director.py            # Per-scene volume planning from emotion tags
│   ├── mood_transitions.py        # Emotion-aware crossfade picker
│   ├── audio_utils.py             # Cache keys, ffprobe, validation, Arabic normalization
│   └── asset_storage.py           # ⭐ v22.7: Supabase Storage bucket for cross-runner assets
│
├── engines/
│   ├── script_engine.py           # Curriculum → verified ayahs → multi-Gemini pool
│   ├── script_engine_v20.py       # Multi-task prompt builder (1500 words, banned-phrase list)
│   ├── script_engine_unified.py   # Batch (1 call) → falls back to legacy per-ayah (7 calls)
│   ├── script_polisher.py         # Banned-phrase / sentence-length / Egyptian-dialect post-checks
│   ├── age_appropriateness.py     # Vocabulary + concept difficulty audit for ages 6-12
│   ├── voice_engine.py            # Parallel TTS + Quran CDN pool + per-emotion settings
│   ├── voice_emotion_mapper.py    # 5 emotion presets × 7 segment types → voice settings
│   ├── visual_render_engine.py    # ProceduralRenderer (HTML → webm → mp4, with cache)
│   ├── scene_templates.py         # 5 CSS scenes + 6 Three.js scenes, emotion-aware
│   ├── visual_prompt_engineer.py  # Locked style template for Leonardo (positive + negative)
│   ├── visual_prompt_deep.py      # 3-layer chained Gemini visual prompts (rich 14-field output)
│   ├── batch_engines.py           # BatchScriptEngine + BatchVisualPromptEngine + BatchTTSDirector
│   ├── tts_director.py            # Per-segment SSML pace/pronunciation directions
│   ├── intro_outro_engine.py      # Branded 5s clips + wrap_episode
│   ├── subtitle_engine.py         # ASS subtitle generator with timing-from-audio
│   ├── subtitle_typography.py     # Arabic shaping validator
│   ├── thumbnail_engine.py        # 3 PIL variants per episode for A/B testing
│   ├── image_engine.py            # Leonardo Phoenix + Lightning XL with quota guard
│   ├── tafsir_validator.py        # Gemini reviewer + ForbiddenAnalogyDetector
│   ├── hook_optimizer.py          # Thompson Sampling for hook-strategy selection
│   ├── review_gate.py             # Manual-approval gate with Telegram notification hook
│   ├── quality_score.py           # v17 quality scorer
│   └── quality_validator.py       # Legacy rule-based gate (fallback)
│
├── orchestrator.py             # Pipeline coordinator (phase-aware)
├── main.py                     # CLI + composition root + AssetStorage wiring
├── requirements.txt
├── supabase_schema.sql
└── .github/workflows/pipeline.yml
```

---

## Setup

### 1. Install system dependencies

```bash
# Ubuntu / Debian
sudo apt-get install ffmpeg fonts-amiri fonts-noto-core
```

### 2. Install Python packages

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### 3. Set environment variables

Create `.env` (or use GitHub Secrets for CI):

```bash
# LLMs — at least one Gemini key required; three keys give 60 calls/day
GEMINI_API_KEY=...
GEMINI_API_KEY_2=...        # second Google project for Phase 2 TTS
GEMINI_API_KEY_3=...        # third Google project for Phase 2 visuals

# TTS — at least one required
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=UR972wNGq3zluze0LoIp
CAMB_AI_KEY=...             # optional fallback (set CAMB_AI_VOICE_ID too)
CAMB_AI_VOICE_ID=...
GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-sa.json   # last-resort fallback

# Images — optional, falls back to CSS scenes
LEONARDO_API_KEY=...

# Persistence — required (script repository AND v22.7 asset storage)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=...

# YouTube — required unless --dry-run
YOUTUBE_CLIENT_ID=...
YOUTUBE_CLIENT_SECRET=...
YOUTUBE_REFRESH_TOKEN=...

# Phase-router behavior
QEEMA_USE_PHASE_ROUTER=true      # enable 3-day phased execution
QEEMA_AUTO_APPROVE=true          # bypass Review Gate (skip manual approval)
```

### 4. Initialize Supabase

In your Supabase SQL editor, run `supabase_schema.sql`. This creates the
episodes/pipeline_state tables and seeds 38 pending episodes.

The `episode-artifacts` Storage bucket is created automatically on first
Phase 2 run. If your service-role token can't create buckets, create it
manually as Private in the Supabase Storage dashboard.

---

## Usage

### Phase-based execution (production)

The workflow YAML calls main.py with `--episode N --phase P`. The phase
router decides what to do based on `state/phases/episode_NNN.json`:

```bash
# Day 1: planning only
python main.py --episode 5 --phase 1

# Day 2: assets only (requires Phase 1 done)
python main.py --episode 5 --phase 2

# Day 3: render + upload (requires Phase 2 done)
python main.py --episode 5 --phase 3

# Auto-detect what to run next
python main.py --episode 5            # picks next pending phase
```

### Legacy single-run execution (dev)

```bash
# Run everything in one go (skips phase split)
QEEMA_USE_PHASE_ROUTER=false python main.py --episode 5
```

### Dry run (no YouTube upload)

```bash
python main.py --episode 5 --phase 3 --dry-run
```

### View status dashboard

```bash
python main.py --status
```

### Local development without Supabase

```bash
python main.py --episode 5 --skip-supabase
# Uses state/local_episodes.json instead.
# Phase 3 will only work on the same runner as Phase 2.
```

---

## Caching strategy

The pipeline aggressively caches at multiple levels — re-runs of Phase 3
typically finish in 3-5 minutes because everything cache-hits:

| What | Where | Invalidation |
|---|---|---|
| Scripts | `temp/episodes/episode_NNN.json` | Manual delete |
| Tafsir text | `state/tafsir_cache.json` | Manual delete |
| TTS clips | `temp/tts_cache/{sha256(voice_id + settings + text)}.mp3` | Settings change |
| Quran audio | `assets/quran_audio/{reciter}_NNN_NNN.mp3` | Permanent |
| Leonardo images | `temp/image_cache/{sha256(prompt + model + emotion)}.png` | Prompt change |
| Scene videos | `temp/scene_cache/{sha256(scene + palette + text + audio + bg + grade)}.mp4` | Any input change |
| Intro/outro | `assets/branding/{intro,outro}.mp4` | Built once |
| Phase state | `state/phases/episode_NNN.json` | GitHub Actions cache |
| Episode assets | Supabase Storage `episode-artifacts/episode_NNN/` | Per-episode |

---

## Failure handling

The exception hierarchy distinguishes failures by recovery strategy:

- `TransientError` → stage_retry handles with exponential backoff
- `PermanentError` → marks episode `failed_permanent`, no retry
- `QualityGateError` → marks `failed_quality`, blocks upload
- `VisualRenderError` → contextualized error with scene name, retryable
- `AssetStorageError` → Phase 2 fails loudly rather than silently corrupting Phase 3

Each Phase has its own retry budget (default 2 attempts per phase). After
that, the episode is parked and needs manual intervention.

Logs go to `logs/pipeline.log` and observability spans to `logs/spans.jsonl`.
Both are uploaded as workflow artifacts on every run.

---

## Religious accuracy

This is non-negotiable. Three layers of protection:

1. **Verified ayah text**: every Quran verse comes from `infrastructure/
   quran_text_api.py` (quran.com / api.alquran.cloud). Never AI-generated.
2. **Tafsir cross-check**: every explanation + analogy is reviewed by a
   Gemini reviewer with the actual tafsir text as context. Confidence < 0.65
   rejects the episode.
3. **Forbidden-analogy detector**: deterministic string matching catches
   four canonical doctrinal errors (e.g., comparing "yawm al-din" to
   biological cycles, comparing worship to physical attraction).

Failing any of these layers marks the episode `religious_rejected` and
prevents upload.

---

## Auto-approval vs Review Gate

By default, episodes 1-10 are blocked from auto-upload — the rendered video
is saved and a review summary is written to `review/episode_NNN_REVIEW.md`.
This catches systemic issues before they hit the public channel.

To bypass for production-confident runs:

```yaml
env:
  QEEMA_AUTO_APPROVE: 'true'
```

Bad uploads can always be deleted from YouTube after the fact.

---

## Troubleshooting

### "Audio missing for render"
Phase 3 ran but Phase 2's assets aren't on disk. Check:
- Did Phase 2 finish? `state/phases/episode_NNN.json` should have `phase: 2`.
- Was AssetStorage wired? Look for `☁️ AssetStorage wired` in the Phase 2 log.
- Does the Supabase bucket `episode-artifacts/episode_NNN/` have files?

### "Phase X exhausted retries"
The same phase failed twice. Inspect the last error in
`state/phases/episode_NNN.json` (`last_error` field). Common causes:
- Gemini daily quota exhausted → wait until UTC midnight reset
- Supabase Storage rate limiting → retry the next day
- Religious validator rejection → script needs a regeneration pass

### "Quality below threshold"
The LLM produced a script that violates rules in `quality_validator.py` or
`script_polisher.py`. Check the `critiques` field. Most common causes:
- Lebanese dialect (هلق، شو، هيك) instead of Egyptian
- Forbidden cliché phrases (يا أحبائي، تعالوا نتعلم)
- Sentence length > 12 words

### "AI images: 0 generated"
Leonardo skipped. Possible causes:
- `LEONARDO_API_KEY` not set → CSS scenes used as fallback (still ships fine)
- Daily quota exhausted (150 free tokens)
- `scene.visual_prompt` is empty — Phase 2 deep_visuals failed to merge into
  the script JSON. Leonardo falls back to CSS which is acceptable.

### "Browser pool exhausted"
The `BrowserPool` ran out of browsers. Increase `procedural.browser_pool_size`
in `core/config.py` (default: 3).

### Phase router refuses to start a phase
Either the prerequisite phase isn't done, or retry cap was hit. The router
log message will tell you which. Reset by editing the phase state JSON.

---

## License

Proprietary — © Eslam Elfaramawy / VALUE Channel.
Not for redistribution without written consent.
