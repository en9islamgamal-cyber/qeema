# Migration Guide: v10 → v11

This document explains how to upgrade an existing v10 deployment to v11.

---

## Breaking Changes

### 1. File layout

The codebase is reorganized into 4 packages:

| v10 | v11 |
|-----|-----|
| `config.py` | `core/config.py` |
| `models.py` | `core/models.py` |
| `core_adapters.py` | `infrastructure/llm_adapters.py` |
| `script_engine.py` | `engines/script_engine.py` |
| `voice_engine_v2.py` | `engines/voice_engine.py` |
| `visual_engine.py` | `engines/visual_render_engine.py` + `engines/scene_templates.py` |
| `video_engine.py` | `infrastructure/ffmpeg_assembler.py` |
| `intro_outro_engine.py` | `engines/intro_outro_engine.py` |
| `quality_gate.py` | `engines/quality_validator.py` |
| `thumbnail_engine.py` | `engines/thumbnail_engine.py` |

### 2. Removed modules

These v10 modules are removed (their content was either dead code, unused, or replaced):

- `ai_director.py` — never wired in
- `smart_timing.py` — never wired in
- `brand_engine.py` — replaced by `engines/intro_outro_engine.py`
- `sfx_engine.py` — sounds are now handled by `voice_engine`
- `gamification_engine.py` — out of scope

### 3. New required env vars

| Var | Required when | Purpose |
|-----|---------------|---------|
| `ELEVENLABS_VOICE_ID` | always | Cache key (was hardcoded as a constant in v10) |

### 4. Database schema

A new column is required:
```sql
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS surah TEXT;
ALTER TABLE episodes ADD CONSTRAINT episodes_status_check CHECK (status IN (
    'pending', 'processing', 'completed',
    'failed', 'failed_quality', 'failed_permanent'
));
```

The constraint adds two new statuses: `failed_quality` and `failed_permanent`.

### 5. Cache invalidation

⚠️ **Important**: v11 uses different cache keys than v10. After upgrading,
the existing cache directories should be cleared:

```bash
rm -rf temp/tts_cache temp/scene_cache
# Quran audio cache (assets/quran_audio) is compatible — keep it.
```

---

## Migration Steps

### Step 1: Backup
```bash
cp -r your_v10_dir/ your_v10_backup/
```

### Step 2: Install v11
```bash
git pull  # or replace files
pip install -r requirements.txt --upgrade
```

### Step 3: Update database
Run the migration SQL above in your Supabase SQL editor.

### Step 4: Clear stale caches
```bash
rm -rf temp/
```

### Step 5: Add new env var
```bash
export ELEVENLABS_VOICE_ID=UR972wNGq3zluze0LoIp
```

### Step 6: Test with dry run
```bash
python main.py --episode 1 --dry-run
```

If this succeeds, you're good to go.

---

## Performance Comparison

Approximate per-episode runtime on the same hardware:

| Stage | v10 | v11 | Speedup |
|-------|-----|-----|---------|
| script | 60s | 60s | 1.0× |
| audio | 90s | 35s | **2.6× (parallel)** |
| render | 480s | 110s | **4.4× (browser pool)** |
| concat | 25s | 5s | **5.0× (stream-copy)** |
| upload | 60s | 60s | 1.0× |
| **TOTAL** | **~12 min** | **~5 min** | **~2.4×** |

---

## Behavioral Changes

### Stricter quality gate
v10 had `quality_gate.py` but never called it. v11 wires it into
`ScriptEngine`. If your scripts now fail quality, see the "Troubleshooting"
section in the README. You can lower `ScriptQualityValidator.PASS_THRESHOLD`
if needed (default: 70.0).

### Cleaner failures
v10 silently retried indefinitely on permanent errors (e.g., bad API key).
v11 stops immediately on `PermanentError`. If a CI run fails fast with
"AuthenticationError", check your secrets.

### Resumable cleanup
v10 deleted intermediate files **before** confirming upload success.
If upload failed after cleanup, you'd lose work. v11 only cleans up
after both upload + DB commit confirm — safer for transient network blips.

---

## Rollback

If anything goes wrong, restore from your backup:
```bash
rm -rf your_v10_dir/
mv your_v10_backup/ your_v10_dir/
```

The Supabase schema changes are backwards compatible (added columns are
nullable; constraint is permissive of all v10 statuses).
