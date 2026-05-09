# QEEMA v15 — Complete Changelog

## v15.1 — Production Hotfix (May 4, 2026)

### 🔴 Critical Fixes

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | `engines/intro_outro_engine.py` | `_make_silence` failed with "Nothing was written into output" | Codec `aac` → `libmp3lame` to match `.mp3` extension; updated lavfi syntax to `channel_layout=stereo:sample_rate=44100` |
| 2 | `main.py` | VERSION = "13.0.0" displayed in banner | Updated to "15.0.0" |
| 3 | `main.py` | `EngineConfig` re-instantiation dropped v15 fields (BGM/crossfades/etc) when `QEEMA_CRAFTING` env was set | Use `dataclasses.replace()` to preserve all fields |
| 4 | `engines/voice_engine.py` | TTS cache served stale audio after voice setting changes | Cache key now includes `stability/style/speed/model` signature |
| 5 | `engines/voice_engine.py` | Mastered audio used `.aac` raw stream extension | Changed to `.m4a` (proper M4A container with duration metadata) |
| 6 | `orchestrator.py` | Log said "Warming up v14 orchestrator" | Updated to v15 |
| 7 | `orchestrator.py` | `enable_color_grade`/`enable_crossfades`/`enable_bgm` not readable from ENV | Added ENV override logic |
| 8 | `engines/quality_score.py` | Late `import _CHARACTER_NAMES` from script_engine — cyclic risk | Inlined as `_KNOWN_CHARACTER_NAMES` frozenset |
| 9 | `main.py` | Cinematic features (BGM, subtitles, etc.) not wired to Orchestrator constructor | All features now passed through `_build_orchestrator()` |
| 10 | `main.py` | Quality validator hard-coded to legacy `ScriptQualityValidator` | Auto-prefer v15 `QualityScorerAdapter` with legacy fallback |

### 📊 Validation

All 12 modified Python files pass:
- ✅ AST syntax check
- ✅ Import resolution
- ✅ EngineConfig replace() preserves v15 fields
- ✅ scene_templates produces valid HTML with logo + font
- ✅ QualityScorer scores correctly (88.75/100 on test fixture)
- ✅ ffmpeg silence command produces valid 56KB MP3 file

### 🔬 ENV Variables (v15)

| ENV | Default | Purpose |
|-----|---------|---------|
| `ELEVENLABS_SPEED` | 0.85 | Voice speed (0.7-1.5) |
| `ELEVENLABS_STABILITY` | 0.68 | Voice consistency |
| `ELEVENLABS_STYLE` | 0.30 | Voice expressiveness |
| `ENABLE_BGM` | true | Background nasheed mixing |
| `ENABLE_SUBTITLES` | false | Burn ASS subtitles into video |
| `ENABLE_CROSSFADES` | true | Smooth scene transitions |
| `ENABLE_COLOR_GRADE` | true | Warm tint pass |
| `QUALITY_THRESHOLD` | 70 | Quality gate (0-100) |
| `QEEMA_CRAFTING` | from config | Smart prompt crafting |
| `QEEMA_SSML` | from config | SSML output (currently metadata only) |

### 🚦 Migration

**For users on v14:**
```bash
# 1. Replace files
cp -r qeema-main/* /your/repo/

# 2. Clear v14 script cache (forces v15 regeneration)
rm -rf temp/episodes/*.json

# 3. Optionally clear TTS cache (forces re-synthesis with v15 voice settings)
rm -rf temp/tts_cache/

# 4. Commit + push, then trigger workflow
git add . && git commit -m "Upgrade to QEEMA v15.1"
git push
```

### 🎯 Expected Behavior

1. **Banner:** Will show `Version 15.0.0`
2. **Log:** Will show `🔥 Warming up v15 orchestrator`
3. **TTS init:** Will log all voice settings including `speed=0.85`
4. **Quality:** Will log `✅ Using v15 quality scorer (threshold=70)`
5. **Cost log:** `logs/costs_YYYY-MM-DD.jsonl` will be created

### 🐛 Known Limitations

- **SSML in `voice_engine`:** SSML is generated in script JSON but not yet sent to ElevenLabs (eleven_multilingual_v2 only supports `<break>` tags, not full prosody). Pauses come from Arabic punctuation + `speed=0.85` instead.
- **Leonardo.ai integration:** API key is configured but no provider implementation yet — `visual_prompt` from LLM is unused.
- **Browser pool size:** Still 1 — render time is sequential. Future v15.2 task.

