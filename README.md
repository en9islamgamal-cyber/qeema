# قِيمَة · VALUE — v11.0

> Quranic Children's Content Pipeline — Production-grade automation for
> generating educational YouTube videos from Juz Amma.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Architecture](https://img.shields.io/badge/architecture-hexagonal-green.svg)](#architecture)
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()

---

## Overview

**QEEMA** automates a complete YouTube video pipeline for children's Quranic content:

1. Generates a script per episode using LLMs (Egyptian Arabic dialect)
2. Synthesizes narration with ElevenLabs (or Google TTS fallback)
3. Fetches authentic Quranic recitation from multiple CDNs
4. Renders every scene procedurally (Three.js + Playwright)
5. Composes final video with branded intro/outro
6. Uploads to YouTube with thumbnail
7. Tracks every step in Supabase

The pipeline is designed to run unattended on CI (GitHub Actions, every 6h)
and produces a complete 38-episode series of Juz Amma surahs.

---

## Architecture

The codebase follows **Hexagonal Architecture (Ports & Adapters)**:

```
┌─────────────────────────────────────────────────────────────────┐
│                          main.py                                │
│             (composition root + signal handling)                │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                  ┌───────────▼───────────┐
                  │    orchestrator.py    │  ← coordinates stages
                  └───────────┬───────────┘
                              │
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
   ┌───▼─────────┐    ┌───────▼────────┐    ┌────────▼────────┐
   │  engines/   │    │    core/       │    │ infrastructure/ │
   │             │    │  (interfaces,  │    │   (adapters)    │
   │ • script    │    │   models,      │    │ • llm           │
   │ • voice     │    │   exceptions,  │    │ • tts           │
   │ • visual    │    │   resilience,  │    │ • quran cdn     │
   │ • intro/out │    │   config)      │    │ • supabase      │
   │ • thumbnail │    │                │    │ • youtube       │
   │ • quality   │    │                │    │ • ffmpeg        │
   │             │    │                │    │ • browser pool  │
   └─────────────┘    └────────────────┘    └─────────────────┘
```

**Why Hexagonal?**
- Each engine depends only on `core/` interfaces
- Replacing ElevenLabs with OpenAI TTS = swap one adapter
- All resilience patterns (retry, circuit breaker) live in `core/`

---

## Key Improvements (v10 → v11)

| # | Issue (v10) | Fix (v11) |
|---|-------------|-----------|
| 1 | Browser launched per scene (~160s wasted/episode) | `BrowserPool` launches once → **4-5× faster** |
| 2 | LLM retry was recursive (stack overflow risk) + shared `self.ptr` | `ProviderPool` with circuit breaker, lock-protected, iterative |
| 3 | Cache key used adapter name (cache poisoning between voices) | Cache key uses real `voice_id` |
| 4 | Cleanup deleted files **before** upload confirmation | Transactional cleanup: only after upload + DB commit |
| 5 | Quality gate was dead code | Wired into `ScriptEngine`; orchestrator respects it |
| 6 | Per-segment encode + concat re-encode (double work) | Single encode; concat uses stream-copy |
| 7 | Triple-redefined ffmpeg `-i` in intro/outro wrapper | Concat demuxer with list file |
| 8 | Groq forced `json_object` for all calls (broken responses) | Per-adapter response format |
| 9 | No exception hierarchy (everything was `Exception`) | Full hierarchy: `Transient` / `Permanent` / `Pipeline` |

---

## Module Map

```
qeema/
├── core/
│   ├── config.py           # AppConfig, APIKeys, Paths, Video, Audio
│   ├── exceptions.py       # QeemaError + 14 subclasses
│   ├── interfaces.py       # 10 abstract ports (LLM, TTS, Repo, ...)
│   ├── models.py           # Pydantic v2: EpisodeScript, AyahScene
│   ├── resilience.py       # RetryPolicy, CircuitBreaker, ProviderPool
│   └── logging_setup.py    # JSON logs + ContextLogAdapter
│
├── data/
│   └── curriculum.py       # 38 surahs (Juz Amma + Al-Fatiha)
│
├── infrastructure/
│   ├── llm_adapters.py         # GeminiJsonAdapter, GroqJsonAdapter
│   ├── tts_providers.py        # ElevenLabsProvider, GoogleTTSProvider
│   ├── quran_sources.py        # 4 CDN sources (Alafasy, Husary)
│   ├── quran_text_api.py       # Verified ayah text fetcher
│   ├── repository_supabase.py  # SupabaseRepository
│   ├── youtube_uploader.py     # OAuth refresh + resumable upload
│   ├── ffmpeg_assembler.py     # Single-encode + stream-copy concat
│   ├── browser_pool.py         # ⭐ Performance win: pool of Chromiums
│   └── audio_utils.py          # Cache keys, ffprobe, validation
│
├── engines/
│   ├── script_engine.py        # Curriculum → verified ayahs → LLM JSON
│   ├── voice_engine.py         # Parallel TTS + Quran CDN pool
│   ├── visual_render_engine.py # ProceduralRenderer (HTML→webm→mp4)
│   ├── scene_templates.py      # 10 Three.js scene builders
│   ├── intro_outro_engine.py   # Branded 5s clips + wrap_episode
│   ├── thumbnail_engine.py     # PIL-based thumbnails (Arabic shaping)
│   └── quality_validator.py    # Rule-based script gate
│
├── orchestrator.py             # Pipeline coordinator (8 stages)
├── main.py                     # CLI + composition root
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
# LLMs (at least one required)
GEMINI_API_KEY=...
GEMINI_API_KEY_2=...
GEMINI_API_KEY_3=...
GROQ_API_KEY=gsk_...
ANTHROPIC_API_KEY=...

# TTS (at least one required)
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=UR972wNGq3zluze0LoIp
GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-sa.json

# Persistence (required)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=...

# YouTube (required unless --dry-run)
YOUTUBE_CLIENT_ID=...
YOUTUBE_CLIENT_SECRET=...
YOUTUBE_REFRESH_TOKEN=...
```

### 4. Initialize Supabase schema

In your Supabase SQL editor, run `supabase_schema.sql`. This creates the
two tables and seeds 38 pending episodes.

---

## Usage

### Run next pending episode

```bash
python main.py
```

### Run specific episode

```bash
python main.py --episode 5
```

### Dry run (no upload)

```bash
python main.py --episode 1 --dry-run
```

### View status dashboard

```bash
python main.py --status
```

### List configured voices/providers

```bash
python main.py --list-voices
```

---

## Pipeline Stages

Each episode runs through 8 stages, each timed and logged:

| # | Stage | Engine | Approx. duration |
|---|-------|--------|------------------|
| 1 | `script` | ScriptEngine + Quran API + LLM pool | ~30-60s |
| 2 | `audio` | VoiceEngine (parallel TTS + CDN) | ~25-45s |
| 3 | `audio_master` | ffmpeg loudnorm + fades | ~5-10s |
| 4 | `render_scenes` | ProceduralRenderer (Three.js) | ~3-5min |
| 5 | `concat_raw` | FFmpegAssembler (stream-copy) | ~5s |
| 6 | `wrap_branded` | IntroOutroEngine | ~10-15s |
| 7 | `thumbnail` | ThumbnailEngine (PIL) | ~2s |
| 8 | `upload` | YouTubeUploader (resumable) | ~30-90s |

**Total per episode: ~6-10 minutes** on a typical CI runner.

---

## Caching strategy

The pipeline aggressively caches at multiple levels:

- **Scripts** → `temp/episodes/episode_NNN.json` (Pydantic-validated reload)
- **TTS clips** → `temp/tts_cache/{sha256(voice_id + text)}.mp3`
- **Quran audio** → `assets/quran_audio/{reciter}_NNN_NNN.mp3`
- **Scene videos** → `temp/scene_cache/{sha256(scene + palette + text + audio)}.mp4`
- **Intro/outro** → `assets/branding/{intro,outro}.mp4` (built once)

All caches are invalidated automatically when their inputs change.

---

## Failure handling

The exception hierarchy distinguishes failures by recovery strategy:

- `TransientError` → CI retries up to 3× with backoff
- `PermanentError` → marks episode `failed_permanent`, no retry
- `QualityGateError` → marks `failed_quality`, blocks upload
- `PipelineError` → marks `failed`, retryable after manual review

Logs go to `logs/pipeline.log` (JSON format if `python-json-logger` is installed)
and are uploaded as artifacts on CI failure.

---

## Troubleshooting

### "Missing required configuration"
You haven't set the required env vars. Run `python main.py --list-voices`
to check what's configured.

### "Browser pool exhausted"
The `BrowserPool` ran out of browsers. Increase `procedural.browser_pool_size`
in `core/config.py` (default: 1).

### "Quality below threshold"
The LLM produced a script that violates rules in `quality_validator.py`.
Check the `critiques` field in the log; the most common causes are:
- Lebanese dialect (هلق، شو، هيك) instead of Egyptian
- Forbidden punishment words (نار، عذاب، جحيم)
- Word-final tashkeel (TTS artifacts)

### "Stream-copy concat failed"
Codecs differ between segments. The assembler will auto-fall-back to
re-encode; this is logged as a warning, not an error.

---

## License

Proprietary — © Eslam Elfaramawy / VALUE Channel.
Not for redistribution without written consent.
