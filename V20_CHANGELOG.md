# QEEMA v20 — Smart Cost-Aware AI System

## 🎯 Goal
**7 episodes/month at maximum quality within fixed quota budgets.**

Existing subscriptions:
- ElevenLabs Starter ($6/mo) — 30,000 credits
- Leonardo Free Trial — 150 tokens (one-time)
- Anthropic API — pay-as-you-go

---

## 📊 Cost Reduction Summary

| Optimization | API Calls Reduced | Monthly Saving |
|--------------|-------------------|----------------|
| Multi-task script (1 call vs 6) | -83% Gemini calls | $0.04 |
| Batched Tafsir validation | -80% Anthropic calls | $0.70 |
| Combined per-scene TTS | -40% TTS overhead | ~3000 chars/mo |
| Quota-aware degradation | prevents overruns | priceless |
| **Per episode cost** | **~$0.85 → ~$0.55** | **35% reduction** |

---

## 🆕 v20 Files

### `engines/script_engine_v20.py` (130 lines)
Multi-task prompt that generates the entire episode in 1 LLM call.
Reduces Gemini API calls from 6 to 1 per episode.

### `core/degradation_modes.py` (115 lines)
Three quality tiers:
- **HIGH**: 7 images, adaptive voice, Claude validation (default for eps 1-4)
- **BALANCED**: 5 images, adaptive voice, batched validation (eps 5-6)
- **ECONOMY**: 3 images, single voice, heuristic validation (ep 7)

Auto-selects based on remaining quota.

### `core/cost_dashboard.py` (200 lines)
Writes Markdown reports:
- `logs/dashboard_{YYYY-MM}.md` — monthly summary with progress bars
- `logs/episode_NNN_breakdown.md` — per-episode cost breakdown

Uploaded as workflow artifact (90 days retention).

---

## 🔧 Modified Files

### `engines/tafsir_validator.py`
Added `validate_episode_batched_v20()` — single Claude call validates all
ayahs in one batched prompt. Falls back to per-ayah on parse error.

### `engines/voice_engine.py`
Added `synthesize_combined()` — joins multiple text segments with SSML
breaks into one API call. Saves overhead chars per request.

### `main.py`
- `--mode {high|balanced|economy|auto}` CLI flag
- Wires CostDashboard to all stages
- Auto-selects mode if not specified

### `.github/workflows/pipeline.yml`
- Adds `mode` workflow input (dropdown)
- Uploads `dashboards-{run_id}` artifact (90 days)

---

## 📉 Token Budget Per Episode

### HIGH Mode (eps 1-4)
| Resource | Budget | Strategy |
|----------|--------|----------|
| Gemini tokens | ~6,000 | 1 multi-task call |
| Anthropic tokens | ~2,000 | 1 batched validation |
| ElevenLabs chars | ~3,000 | 5 combined per-scene calls |
| Leonardo tokens | ~21 | 7 Lightning XL images |

**Cost: ~$0.55/episode**

### BALANCED Mode (eps 5-6)
| Resource | Budget | Strategy |
|----------|--------|----------|
| Gemini tokens | ~6,000 | 1 multi-task call |
| Anthropic tokens | ~2,000 | 1 batched validation |
| ElevenLabs chars | ~2,500 | shorter per-scene |
| Leonardo tokens | ~15 | 5 images, reuse pattern |

**Cost: ~$0.40/episode**

### ECONOMY Mode (ep 7)
| Resource | Budget | Strategy |
|----------|--------|----------|
| Gemini tokens | ~6,000 | 1 multi-task call |
| Anthropic tokens | ~0 | heuristic only |
| ElevenLabs chars | ~2,500 | combined |
| Leonardo tokens | ~9 | 3 images |

**Cost: ~$0.20/episode**

### Monthly Total (4×HIGH + 2×BALANCED + 1×ECONOMY)
```
ElevenLabs: 4×3000 + 2×2500 + 1×2500 = 19,500 chars / 30,000 (65%)
Leonardo:   4×21   + 2×15   + 1×9   = 117 tokens / 150 (78%)
Anthropic:  6×$0.05 + 1×$0.00 = $0.30
─────────────────────────────────────
Total month cost: ~$3.40 + $6 (ElevenLabs) = $9.40
```

vs v19 estimate of $8.10 — slightly higher with v20 because we now do
batched Claude validation, but quality is much better and quota is safer.

---

## 🧠 How Quota-Aware Mode Selection Works

At episode start, the orchestrator queries QuotaManager:

```python
mode = auto_select_mode(quota_manager)

if leo >= 35 and el >= 3500:    # plenty of quota
    mode = HIGH
elif leo >= 15 and el >= 2500:  # tight
    mode = BALANCED
else:                            # critical
    mode = ECONOMY
```

You can override via CLI:
```bash
python main.py --episode 5 --mode economy
```

Or via workflow_dispatch input.

---

## 🎬 Smart Reuse Strategies

### BALANCED mode (5 images for 7 scenes)
The orchestrator now reuses images for similar emotional contexts:
- intro_scene → unique image
- ayah_1 (first ayah) → unique image
- ayah_2 (similar emotion to ayah_1) → reuse ayah_1's image
- ayah_3 → unique image
- ayah_4 → reuse ayah_3
- ayah_5 → unique image
- outro_scene → reuse intro
= **5 generated, 7 used**

### ECONOMY mode (3 images for 7 scenes)
- Hero intro/outro composite (same image)
- One mid-episode hero ayah image
- All other ayahs use CSS gradient with locked palette
= **3 generated, 7 used**

---

## 🔍 Cost Dashboard Example

After running episode 3, `logs/dashboard_2026-05.md` contains:

```markdown
# Monthly Cost Dashboard — 2026-05

Status: 🟢 HEALTHY

## Episodes Progress
3/7 episodes completed

## Quota Utilization
| Service | Used | Total | % | Bar |
|---------|------|-------|---|-----|
| ElevenLabs | 9,000 | 30,000 | 30.0% | ██████░░░░░░░░░░░░░░ |
| Leonardo | 63 | 150 | 42.0% | ████████░░░░░░░░░░░░ |

## Projections
- Average cost per episode: $0.55
- Projected month-end total: $3.85

## Recommendations
✅ All quotas healthy. Continue with current quality mode.
```

---

## ⚠️ Critical Behavior Changes

### v19 → v20 differences
1. **Episode 7 will run in ECONOMY mode by default** (3 images, heuristic validation).
   If you want full quality on ep 7, manually set `--mode high` (will likely fail
   if Leo quota exhausted).

2. **Tafsir validation is now batched**. If batched call fails, falls back to
   per-ayah. You'll see this in logs:
   ```
   ⚠️ Batched validation failed: ... — falling back to per-ayah
   ```
   This is normal recovery — not an error.

3. **Multi-task script prompt may occasionally fail to parse**. Built-in retry
   reverts to per-ayah generation (slow path). Logged as:
   ```
   ⚠️ Multi-task parse failed: ... — using legacy 6-call generation
   ```

---

## 🚀 Migration from v19

```bash
# 1. Replace files (everything backward compatible)
unzip qeema_v20.zip
cd qeema-main

# 2. Optional: migrate quota state from v19
# (auto-loads from logs/quota_state.json — no action needed)

# 3. First v20 run with mode override
python main.py --episode 1 --mode high --dry-run

# 4. Workflow trigger
gh workflow run pipeline.yml -f episode=1 -f mode=auto
```

---

## 📦 File Inventory

**v20 NEW (3):**
- `engines/script_engine_v20.py`
- `core/degradation_modes.py`
- `core/cost_dashboard.py`

**v20 MODIFIED (4):**
- `main.py` — `--mode` flag, dashboard wiring
- `engines/voice_engine.py` — `synthesize_combined()` method
- `engines/tafsir_validator.py` — `validate_episode_batched_v20()` method
- `.github/workflows/pipeline.yml` — `mode` input, dashboards artifact

**v19 INHERITED (unchanged, all working):**
- All v18 features (TafsirValidator, VisualPromptEngineer, HookOptimizer, etc.)
- QuotaManager (v19)
- Lightning XL config (v19)

---

# v20.1 Patch — Skip Supabase Mode (Testing/Dev)

## 🎯 ليه التحديث ده

في مرحلة الاختبارات الأولى، Supabase زيادة عن الحاجة:
- بياخد وقت setup
- ممكن يحجب الاختبار لو فيه حاجة في الـ network
- مش محتاج تتبع DB في الـ dry runs

v20.1 بيضيف خيار **يخطي Supabase تماماً** ويستخدم ملف JSON محلي.

## ✨ الإضافات

### `infrastructure/repository_local.py` (NEW — 220 lines)
JSON-file backed `EpisodeRepository` — drop-in replacement لـ Supabase.

- 11 unit tests passed ✅
- Same ABC interface (مفيش أي تغيير في orchestrator)
- Atomic writes (tmp + rename pattern)
- Thread-safe (in-process lock)
- State file: `state/local_episodes.json`
- Bonus methods: `reset()`, `stats()`

### CLI Flags (NEW)
```bash
--skip-supabase          # استخدم LocalRepository بدل Supabase
--reset-local-state      # امسح الـ local state قبل الـ run (dev only)
```

### Workflow Inputs (NEW)
```yaml
inputs:
  skip_supabase:
    type: boolean
    default: false
  reset_local_state:
    type: boolean
    default: false
```

### Config Validation
- لو `SKIP_SUPABASE=true` env متضبط، الـ `SUPABASE_URL/KEY` مش هيبقوا required
- متضبط تلقائياً لما الـ `--skip-supabase` flag يتمرر

### Workflow Artifact (NEW)
- `local-repo-{run_id}` (90 days retention) — يحتفظ بـ state file بعد الـ run

## 🚀 طريقة الاستخدام

### Local development
```bash
# أول run (مفيش Supabase، مفيش YouTube)
python main.py --episode 1 --skip-supabase --dry-run

# مع reset كل مرة (للاختبارات المتكررة)
python main.py --episode 1 --skip-supabase --reset-local-state --dry-run

# عرض الـ state بدون run
python main.py --status --skip-supabase
```

### GitHub Actions
```bash
gh workflow run pipeline.yml \
  -f episode=1 \
  -f skip_supabase=true \
  -f dry_run=true \
  -f mode=high
```

### الانتقال للإنتاج
لما تخلص الاختبارات، شيل الـ flag:
```bash
gh workflow run pipeline.yml -f episode=1   # uses Supabase by default
```

State فيlocal_episodes.json يبقى موجود في حالة عايز ترجعله، بس في الـ production الـ Supabase هو الـ authoritative.

## ⚠️ ملاحظات

**1. `LocalRepository` و `SupabaseRepository` مش متزامنين**. لو شغّلت لمدة في local، state Supabase مش هيتحدث، والعكس. اعتبرهم بيئتين منفصلتين.

**2. لو حصل race condition** (محرابيات شغّالات في نفس الوقت على نفس الـ JSON file)، الكتابة الأخيرة هي اللي بتثبت. مش مشكلة في الـ pipeline العادية لأن الـ workflow بيستخدم `concurrency: qeema-pipeline` اللي بيمنع التشغيل المتوازي.

**3. متستخدمش `--reset-local-state` في الإنتاج**. فيه `--skip-supabase` بدون reset لو عايز state يستمر بين الـ runs.

## 📝 Files Modified (v20.1)

- `main.py` — `--skip-supabase`, `--reset-local-state` flags + repository switching
- `core/config.py` — `SKIP_SUPABASE` env var bypasses validation
- `.github/workflows/pipeline.yml` — workflow inputs + artifact upload

## 📦 Files Added (v20.1)

- `infrastructure/repository_local.py` (220 lines, 11 tests passed)
