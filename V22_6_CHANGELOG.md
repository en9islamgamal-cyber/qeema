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


---

## v22.6.2 hotfix — BatchTafsirReviewer recovery from malformed JSON

### Incident

Episode 1 first run: BatchScriptEngine succeeded ✅, BatchTafsirReviewer
failed with `JSON parse failed at pos 406 → Salvage failed: Expecting ','
delimiter: line 20 column 6 (char 330)`. The fallback chain (per-ayah
multi-key rotation) caught the failure and finished Phase 1 successfully.
But the user wants the batch path itself to succeed 100%.

### Root cause analysis

The character-level error position (330 of 406+) ruled out truncation —
the failure was structural inside the response. Two compounding causes:

1. **Tight `max_tokens=4096`** for tafsir batch review. Seven ayahs each
   with potentially multi-sentence Arabic concerns can easily push the
   output past 4 K tokens. When Gemini gets close to the ceiling, it
   sometimes emits structurally-broken JSON.

2. **Naive salvage layer** — the v22.6.0 cleaner only handled markdown
   fences, smart quotes, and trailing commas. It did NOT escape literal
   newlines that Arabic LLMs frequently insert mid-string for
   readability. A raw `\n` inside a quoted string breaks `json.loads`.

### Fixes

**1. `_aggressive_json_clean()` — overhauled**

A character-walking cleaner that handles all six known Gemini-with-Arabic
failure modes:

| # | Failure mode | Fix |
|---|---|---|
| 1 | Markdown fences (`` ```json ``` ``) | Strip leading/trailing |
| 2 | Smart quotes around Arabic strings (`"` `"` `'` `'` `«` `»`) | Normalize to ASCII |
| 3 | Trailing commas before `}` or `]` | Remove |
| 4 | **Unescaped raw `\n` inside quoted Arabic strings** (the v22.6.1 cause) | Walk char-by-char with string-state tracking, escape `\n`/`\r`/`\t` only when inside `"..."` |
| 5 | BOM and zero-width chars | Strip |
| 6 | Leading/trailing chatter ("تمام، هكتبلك:") | Extract `{…}` block |

**2. `_try_iterative_json_recovery()` — new layer**

When the response is truncated but the prefix is balanced, walk back to
the last `}` at depth 0 and parse the prefix. Properly tracks brace
depth respecting strings (a `}` inside a quoted string is not counted).

**3. BatchTafsirReviewer — `max_tokens` 4096 → 16384** + automatic retry

- The tight 4 K budget was a real constraint for 7 ayahs of Arabic.
- On first failure, retry with a simplified prompt (drops examples,
  keeps doctrinal constraints) at `temperature=0.0` for determinism.
- Both attempts use the new cleaner.

**4. Diagnostic dump** — new `_dump_failed_response()`

Any future `_call_gemini_with_schema` failure now writes the raw response
+ error summary to `logs/gemini_failures/{Schema}_{timestamp}.txt`.
GitHub Actions artifact upload picks this up automatically. Controlled
by env var `QEEMA_DEBUG_DIR`.

### Test coverage

Added 25 tests:

- `TestAggressiveJsonClean` (13): each of the 6 failure modes in
  isolation + combined real-world payloads + idempotency.
- `TestIterativeJsonRecovery` (6): truncation patterns + nested objects
  + braces inside strings.
- `TestBatchTafsirReviewerRetry` (4): retry path triggers, simplified
  prompt is shorter and still carries red flags, both-attempts-fail returns None.
- `TestRealisticMalformedJsonE2E` (1): reconstructs the **exact**
  v22.6.1 episode 1 incident pattern (smart quotes + unescaped Arabic
  newline) and verifies the new cleaner recovers all 7 reviews.

Total: **557 tests pass**, both with and without `google-genai`
installed (matches CI pre_check vs pipeline job environments).

### What is NOT verified

- Real Gemini behaviour with the new `max_tokens=16384` is untested —
  but raising the ceiling is conservative (the budget was previously too
  tight, not too loose).
- The diagnostic dump path is exercised by tests via mock, but the file
  upload to GitHub Actions artifact happens at workflow level; no
  changes were made there.
- If a future failure mode emerges that none of the six cleaners handle,
  the diagnostic dump will surface the exact raw response so we can
  extend the cleaner with confidence.


---

## v22.6.3 — Phase 2 reliability overhaul

### Incident summary

Episode 1 Phase 2 reported `Pipeline succeeded` but five things were
actually broken (caught by inspecting the phase-state artifact + log):

| # | Surface symptom | Real cause |
|---|---|---|
| 1 | Gemini `400 INVALID_ARGUMENT` on TTS director batch | `BatchTTSOut` schema state space too large |
| 2 | Log claimed "28 segments directed with SSML" | Legacy TTSDirector returned fallback (no SSML), orchestrator misreported |
| 3 | All 7 Leonardo image generations fell back to CSS | `script.ayah_scenes[i].visual_prompt` was empty |
| 4 | Phase 3 would not have any visuals or TTS to use | `_deep_visuals` and `_tts_directions` only persisted to ephemeral temp JSON |
| 5 | Phase 2 wasted 23.3s + 1 Gemini call regenerating Phase 1's script | Temp episode JSON wiped between runners |

### Fixes

**Fix 1 — `SegmentTTSOut` / `BatchTTSOut` schema simplification**

Removed `max_length` constraints from `segment_id`, `directed_text`,
`pace_reason`, `pronunciation_notes`, and `directions` array. Gemini's
JSON-Schema enforcer rejects schemas where `array_max × per_field_max`
exceeds an internal limit, returning HTTP 400 with `'too many states for
serving'`. We keep Pydantic-side validation post-response (still rejects
empty arrays via `min_length=1` and missing required fields).

The other batch schemas (`BatchScriptOut`, `BatchReviewOut`,
`BatchVisualOut`) were not touched because they were already proven to
work in production runs.

**Fix 2 — Honest TTS fallback reporting**

`_legacy_tts` now returns `None` when `episode_direction.fallback_used`
is True (TTSDirector populates that flag when its Gemini call fails JSON
parse and it produces no-SSML fallback segments). The orchestrator's
caller logs `ℹ️ no directions produced — audio will use base voice
settings` instead of the previous misleading `🎙️ N segments directed
with SSML`.

**Fix 3 — Persist Phase 2 outputs to phase state**

`_run_phase2_deep_visuals` and `_run_phase2_tts_director` now also call
`_save_phase_state(deep_visuals=...)` and `_save_phase_state(tts_directions=...)`
after writing the temp JSON. Phase 3 (running on a fresh runner with no
temp files) can recover both via `_load_phase_state`. The temp JSON
remains the authoritative source within a single run; the phase state
is the cross-run survivor.

**Fix 4 — Refresh script after deep visuals augment it**

After `_run_phase2_deep_visuals` finishes, the orchestrator now calls
`_reload_episode_script` again to rebuild the in-memory script with the
fresh `_deep_visuals` dictionary. Previously the script object was
loaded BEFORE `_deep_visuals` existed, so its `ayah_scenes[i].visual_prompt`
fields stayed empty and every AI image generation fell back to CSS.
This was a v22.5 latent bug exposed by Episode 1.

**Fix 5 — Phase 1 snapshots episode JSON for Phase 2/3 reload**

After Phase 1 finishes, the temp episode JSON is read back and saved
into the phase state under `episode_json_snapshot`. `_reload_episode_script`
now restores the temp JSON from that snapshot before attempting
`load_from_disk`. Eliminates the wasteful regeneration in Phase 2 (saved
~23s + 1 Gemini call per episode) and makes Phase 2/3 deterministic
even when the GitHub Actions runner restarts.

### Test coverage

11 new tests in `tests/test_v22_6_3_phase2_fixes.py`:

- `TestSchemaStateSpace` (4): no `maxLength` on TTS schema fields, no
  `maxItems` on `directions` array, Pydantic still enforces required
  fields, `min_length=1` still enforced.
- `TestLegacyTtsHonestReporting` (2): `_legacy_tts` returns `None` when
  `fallback_used=True`, returns dict when real SSML produced.
- `TestPhase2OutputsPersistedToState` (2): `_run_phase2_deep_visuals`
  and `_run_phase2_tts_director` both call `_save_phase_state` with
  their payloads.
- `TestReloadFromSnapshot` (3): snapshot restored when temp JSON
  missing, NOT overwritten when temp exists, falls through to
  `load_from_disk` → `UnifiedScriptEngine` when neither exists.

Total suite: **568 tests**, all pass with and without `google-genai` installed.

### What is NOT verified

- Real Gemini call against the simplified `BatchTTSOut` schema. Removing
  the bounds is conservative (Pydantic still validates structure) but
  the production behavior depends on Gemini accepting the schema. If
  Gemini is happy, the legacy fallback is no longer needed.
- The phase state is roughly twice as large now (it carries a full
  episode JSON snapshot plus deep_visuals + tts_directions). The cache
  upload still fits well under the 100MB Actions artifact limit
  (Episode 1 phase state = ~50KB), but if episodes get larger we may
  need to split.
- `_reload_episode_script` is invoked twice per Phase 2 run now (once
  before deep visuals, once after). Each call is a disk read, not a
  Gemini call, so cost is negligible.

### Deployment

Replace these three files in the repo, push, re-run Phase 2 for episode 1:

```
engines/batch_engines.py        — schema bounds removed
orchestrator.py                  — 5 fixes integrated
tests/test_v22_6_3_phase2_fixes.py — NEW
V22_6_CHANGELOG.md               — this section appended
```

Expected Phase 2 log markers indicating success:

- `🎨 Phase 2 deep visuals: 7/7 usable` (already happened in Episode 1)
- `♻️ Restored episode JSON from phase state snapshot (7 scenes)` (NEW —
  no more BatchScriptEngine call in Phase 2)
- `🎙️ BatchTTSDirector: directing 21 segments` followed by HTTP 200
  (NOT the 400 from v22.6.2)
- `🎙️ Phase 2 TTS director: 21 segments directed with SSML` (this time
  truthful — backed by real SSML from BatchTTSDirector)
- `✅ AI images: 7 generated` (NOT `0 generated` + CSS fallback)
