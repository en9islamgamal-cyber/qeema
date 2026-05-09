# QEEMA v22.6 — Changelog

## Highlights

v22.6 closes three critical gaps that were keeping the v22.5 pipeline from
shipping a doctrinally-safe Episode 1:

1. **Phase 2 actually uses the batch engines now.** v22.5 defined
   `BatchTTSDirector` and `BatchVisualPromptEngine` but never wired them
   into the orchestrator — Phase 2 still ran 21 chained Gemini calls for
   visuals + 7 for TTS. v22.6 wires both, dropping Phase 2 to 1+1 calls
   on dedicated keys 2 + 3.

2. **The legacy fallback prompt no longer suggests "المغناطيس" as an
   analogy domain.** This was the root cause of the v22.5.7 incident where
   ayah 5 (إياك نعبد) failed tafsir review — the LLM was being told it was
   *fine* to use a magnet analogy, then a separate Gemini reviewer flagged
   it. v22.6 removes the suggestion AND adds the four canonical
   forbidden-analogy rules to the legacy `SYSTEM_PROMPT`.

3. **`ForbiddenAnalogyDetector` adds defense-in-depth.** A keyword-level
   deterministic check that runs *before* the Gemini reviewer. If any of
   the four canonical doctrinal errors appears in the explanation, the
   detector forces `passed=False` regardless of the Gemini reviewer's
   verdict. The Gemini reviewer can still slip on subtle cases; this
   layer catches the obvious ones with zero quota cost.

## Architecture changes

### Phase 2 split across keys 2 + 3

| Stage | v22.5 actual | v22.6 |
|---|---|---|
| Visual prompts | 21 chained calls on Key 2 | **1 batch call on Key 3** |
| TTS direction | 7 calls on Key 2 (silently broken — see bug below) | **1 batch call on Key 2** |
| Total Phase 2 calls | 28 | **2** |

`GEMINI_API_KEY_3` was already in the workflow secrets but wasn't being
read. v22.6 adds `_phase2_visual_gemini_adapter()` that reads it.

### Bug fix: legacy TTSDirector silent failure

v22.5's `_run_phase2_tts_director` referenced `episode_direction.directions`,
but the dataclass field is named `segments`. Result: `AttributeError` →
`except` → log "audio will use base voice settings only", every run.
The legacy TTSDirector was effectively dead code in production.

v22.6's `_legacy_tts` reads `.segments` correctly, restoring the legacy
fallback path.

### `BatchVisualOut` schema enrichment

Was 4 fields (subject, environment, mood_lighting, color_palette).
Now mirrors `DeepVisualPromptResult` exactly with all 14 cinematic fields
(layer 1 primitives + layer 2 aesthetic + layer 3 composition). The
`BatchVisualPromptEngine.to_legacy_dicts()` converter produces a list-of-
dicts that drops straight into `episode_data["_deep_visuals"]` with no
downstream code changes.

### `BatchTTSOut` schema redesign

Was per-ayah voice knobs (speed/stability/style). The orchestrator's TTS
synthesis stage doesn't consume those — it consumes per-segment SSML
directions keyed by `{segment_id: {directed_text, pace, ...}}`. The new
schema produces exactly that shape via `to_legacy_dict()`.

### ForbiddenAnalogyDetector

New class in `engines/tafsir_validator.py`. Four rules, each with:

- `topic_keywords`: phrases in the ayah text that indicate the rule applies
- `forbidden_keywords`: patterns whose presence triggers the concern

Critical implementation detail: Quranic text from quran.com API uses
`U+0671` (alif wasla, ٱ) wherever MSA uses `U+0627` (alif, ا). The
`_normalize` method maps the former to the latter — without this, no rule
would ever fire on real API-fetched ayah text.

## Test coverage

Total: 532 tests (was 452 baseline).

New tests:
- `test_batch_engines.py` (36): schema integrity, 3-layer fallback,
  smart-quote normalization, markdown stripping, regex salvage,
  to_legacy_* converters, all 4 batch engines.
- `test_forbidden_analogy_detector.py` (23): all 4 rules positive +
  negative cases, Arabic morphology (tashkeel + alif wasla), edge cases
  (empty input, multiple rules firing, traceability).
- `test_phase2_batch_wiring.py` (15): orchestrator → batch engine
  integration, key splitting, legacy fallback chain, the v22.5 .segments
  bug regression check.
- `test_v22_6_e2e_smoke.py` (6): end-to-end mock test simulating
  Episode 1 Phase 1 + Phase 2 with all Gemini calls mocked.

## Files changed

```
engines/batch_engines.py        — schema enrichment, prompt rewrites, converters
engines/script_engine.py        — forbidden analogies in SYSTEM_PROMPT,
                                  magnet removed from suggested domains
engines/tafsir_validator.py     — ForbiddenAnalogyDetector class +
                                  red flags in reviewer prompts +
                                  detector wired into validate_explanation
orchestrator.py                 — Phase 2 batch wiring, key 3 adapter,
                                  .segments bug fix, detector wired
                                  into _try_batch_tafsir
.github/workflows/pipeline.yml  — workflow display name v22.5 → v22.6
main.py                         — VERSION 22.5.0 → 22.6.0

tests/test_batch_engines.py     — NEW
tests/test_forbidden_analogy_detector.py — NEW
tests/test_phase2_batch_wiring.py — NEW
tests/test_v22_6_e2e_smoke.py   — NEW

tests/test_pipeline_e2e_contract.py — updated (split phase 2 keys)
```

## What was NOT verified

- **Real Gemini behaviour**: All Gemini calls are mocked. The first real
  `BatchScriptEngine` call against gemini-2.5-flash with the new prompts
  has not happened yet. If Gemini's structured output behaviour differs
  from our schema expectations, the 3-layer fallback should catch it,
  but only a real run will tell.

- **Real ElevenLabs/Leonardo/YouTube calls**: Those paths weren't touched
  in v22.6 and remain on their v22.5 contracts.

- **Quota dynamics**: Best case Phase 2 = 2 calls. Worst case (batch
  failures across both stages, falling back to legacy on Key 2 + Key 3
  respectively) = 28 calls. Both fit comfortably in the 20/day per-key
  budget on separate Google accounts.

## How to deploy

1. Replace the repo contents with this ZIP.
2. Confirm `GEMINI_API_KEY_3` is set in GitHub Secrets (it already is per
   the audit screenshot).
3. Push to main.
4. Manually trigger workflow with `phase=` blank (auto-detect → starts
   at Phase 1 for Episode 1).
5. Watch the run for `(BATCH v22.6 — 1 Gemini call ...)` log lines.

If Phase 1 batch script fails its parse and falls back to legacy, the
v22.6 SYSTEM_PROMPT now blocks the magnet analogy at the source. If
something does slip through, `ForbiddenAnalogyDetector` will flag it at
review time.


---

## v22.6.1 hotfix — test fixture portability

**Issue**: The `pre_check` GitHub Actions job runs `pytest tests/` BEFORE
the pipeline job's `pip install -r requirements.txt`. The v22.6 batch
engine tests used `patch("google.genai.types", ..., create=True)` which
internally calls `import google.genai` to resolve the target — failing
with `ModuleNotFoundError` when google-genai isn't installed yet.
Result: 18 errors + 3 failures in pre_check.

**Fix**: Replaced patch-based mocking with direct `sys.modules` injection
via a `fake_google_genai_in_sys_modules()` context manager. This bypasses
the import machinery entirely. Tests now pass whether or not google-genai
is actually installed.

Files touched:
- `tests/test_batch_engines.py` — `fake_genai_types` fixture rewritten
- `tests/test_v22_6_e2e_smoke.py` — same pattern, three call sites updated

Verified: 532/532 tests pass in both states (google-genai installed and
google-genai NOT installed), so the suite works in both pre_check and
pipeline job environments.
