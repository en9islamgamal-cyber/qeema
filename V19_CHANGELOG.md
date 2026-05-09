# QEEMA v19.0 — Quota-Optimized Production (7 episodes/month)

## 🎯 Goal
Maximum quality within fixed budget:
- **ElevenLabs Starter** ($6/mo) — 30,000 credits
- **Leonardo Free Trial** — 150 tokens (one-time)
- **Anthropic API** — pay-as-you-go (~$3/mo)

**Target: 7 episodes per month**

---

## 🔴 NEW: QuotaManager (Hard Budget Enforcement)

`core/quota_manager.py` — Single source of truth for budget tracking.

### What it does
- Tracks per-service usage in `logs/quota_state.json` (persists across runs)
- Pre-flight checks BEFORE expensive API calls
- Records actual consumption AFTER each successful call
- Auto-resets monthly counters (ElevenLabs)
- Leonardo counter persists indefinitely (no monthly reset)
- Refuses to start episode if quota critically low
- Falls back gracefully (Google TTS for ElevenLabs, CSS for Leonardo)

### Math (verified)
```
7 episodes × 3,000 chars/ep = 21,000 chars
Available: 30,000 (Starter) → 70% utilization, 30% buffer ✅

7 episodes × 7 images × 3 tokens = 147 tokens (Lightning XL)
Available: 150 → 98% utilization, 2% buffer ✅
```

---

## 🎨 Image Strategy: Lightning XL Everywhere

### Why
| Strategy | Tokens needed | Fits 150? |
|----------|--------------|-----------|
| Phoenix everywhere | 490 | ❌ |
| Phoenix hero + Lightning ayahs | 245 | ❌ |
| Phoenix intro only + Lightning rest | 196 | ❌ |
| **Lightning XL everywhere** | **147** | **✅** |

### Quality compensation
The visual quality drop from Phoenix → Lightning XL is mitigated by:
1. **VisualPromptEngineer** locked Studio Ghibli style
2. **Per-emotion lighting** (5 distinct moods)
3. **Per-scene color grading** (5 grades from v18)
4. **CSS gradient + particle overlay** as visual richness layer

### Upgrade path documented
When upgrading to Leonardo Premium ($24/mo):
```python
# In core/config.py:
hero_model_id = LEONARDO_PHOENIX  # back to Phoenix (Premium = unlimited!)
scene_model_id = LEONARDO_PHOENIX
```

---

## 🎤 Voice: 2 Concurrent Workers

`voice_parallel_workers: int = 2` (was 3 in v18)

ElevenLabs Starter has 2 concurrent stream limit. With 2 workers, no
risk of 429 rate limits during normal operation.

---

## 📊 Schedule: Every 4 Days

```yaml
schedule:
  - cron: "0 2 */4 * *"   # Every 4 days at 02:00 UTC
```

= ~7-8 runs per month → matches 7 episode target.

---

## 🔧 Wiring Changes

### main.py
- `QuotaManager` initialized FIRST (before all engines)
- Passed to `VoiceEngine(quota_manager=...)`
- Passed to `LeonardoImageEngine(leo_cfg, quota_manager=...)`
- Quota report printed at startup

### voice_engine.py
- `synthesize()` checks budget BEFORE API call
- Records actual chars used AFTER success
- Falls back to Google TTS if available (uses GCP credentials)

### image_engine.py
- `generate()` estimates tokens (Lightning=3, Phoenix=10)
- Refuses if would exceed budget → returns None → CSS fallback
- Records consumption only after successful download

---

## 📋 Workflow Updates

### Run schedule
- Old: every 6 hours (would generate 120 episodes/month — overflow)
- New: every 4 days (~7-8 per month)

### New artifacts
- `quota-state-{run_id}` (90 days retention) — persistent quota state
- `cost-log-{run_id}` (30 days)
- `review-files-{run_id}` (30 days, v18)

### New step: v19 Quota report
At end of each run, prints:
```
═══ v19 Quota Status ═══
Month: 2026-05
Episodes done: 3/7
ElevenLabs: 8,400/30,000 (28.0%)
Leonardo: 63/150 (42.0%)
```

---

## 🚦 What Happens When Quota Runs Out

### ElevenLabs critically low (<2000 chars remaining)
```python
qm.can_start_episode() → False
# Episode skipped entirely. Pipeline exits cleanly.
# Wait for monthly reset (1st of month).
```

### ElevenLabs partial (some scenes can't be voiced)
```python
qm.can_consume_elevenlabs(chars) → False
# Voice engine falls back to Google TTS
# (Requires GOOGLE_APPLICATION_CREDENTIALS env)
```

### Leonardo exhausted
```python
qm.can_consume_leonardo(tokens) → False  
# Image engine returns None
# scene_templates.py renders CSS gradient instead
# Video still produced, just without AI imagery
```

---

## 💰 Expected Monthly Cost

| Service | Monthly Cost |
|---------|--------------|
| ElevenLabs Starter | $6.00 |
| Leonardo Free Trial | $0.00 (one-time) |
| Anthropic (tafsir validation) | ~$2.10 (7 eps × $0.30) |
| **Total** | **~$8.10/month** |

7 episodes/month at this cost = **$1.16 per episode** for fully-validated,
high-quality output.

---

## 🚨 When to Upgrade

You'll know it's time when:
- Channel has 1k+ subscribers
- Monthly views > 10k
- You need to publish 15+ episodes/month
- Engagement validates the format

Then:
```
ElevenLabs Starter $6 → Creator $22 (100k credits = ~30 eps)
Leonardo Free Trial → Premium $24 (Unlimited Phoenix!)
─────────────────────────────────────────────
Monthly: $46/mo for 30+ premium-quality episodes
```

---

## ⚠️ First Run Checklist

```bash
# 1. Verify GitHub Secrets all present
ANTHROPIC_API_KEY  ← v18 critical (tafsir validator)
GEMINI_API_KEY (×3)
GROQ_API_KEY
ELEVENLABS_API_KEY
LEONARDO_API_KEY
SUPABASE_*
YOUTUBE_*
TELEGRAM_BOT_TOKEN  ← optional but recommended (review notifications)
TELEGRAM_CHAT_ID    ← optional

# 2. Clear v18 caches (force v19 regeneration)
rm -rf temp/episodes/*.json
rm -rf temp/tts_cache/

# 3. Push v19 + run episode 1 with manual review
gh workflow run pipeline.yml -f episode=1 -f dry_run=true

# 4. Review review/episode_001_REVIEW.md, then approve
gh workflow run pipeline.yml -f episode=1 -f approve=true

# 5. After episode 10, automatic publishing kicks in
```

---

## 📂 New Files in v19 (1)

- `core/quota_manager.py` (290 lines) — Hard budget enforcement

## 🔧 Modified Files in v19 (5)

- `main.py` — Wires QuotaManager to all engines
- `core/config.py` — Lightning XL everywhere, voice workers=2, QuotaConfig integrated
- `engines/voice_engine.py` — Quota check + Google TTS fallback
- `engines/image_engine.py` — Quota check + token estimation
- `.github/workflows/pipeline.yml` — Schedule every 4 days, quota artifact

---

## 🎬 What You Get

✅ 7 episodes/month, fully automated
✅ Quota-aware: no surprise overages
✅ Graceful degradation: episodes complete even when Leonardo runs out
✅ Full v18 quality: TafsirValidator + VisualPromptEngineer + adaptive voice
✅ Studio Ghibli illustration style maintained across all episodes
✅ Per-emotion color grading + 3-variant thumbnails
✅ Hook bandit learning + manual review for first 10 episodes
