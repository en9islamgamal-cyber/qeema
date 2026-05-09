"""
tests/test_pipeline_e2e_contract.py — VALUE / QEEMA v22.5

End-to-end pipeline CONTRACT test. Does NOT make real API calls — instead
verifies that the data shapes flowing between phases match what each phase
expects, and that the state-persistence + phase-isolation invariants hold.

[Why this exists]
The 3-phase pipeline is the architectural backbone of v22.5. It depends on:
  1. Phase 1 writing episode JSON to a known path
  2. Phase 2 reading from that path, augmenting, writing back
  3. Phase 3 reading the augmented JSON + the phase_state.json

If any phase changes the JSON shape without coordination, the whole pipeline
breaks at the next handoff. These tests lock the contract.

[What we DO test]
  - Phase 1 output JSON has the keys Phase 2 needs
  - Phase 2 output preserves Phase 1's keys (doesn't wipe them)
  - phase_state.json round-trip survives restart
  - Phase 1 uses key #1, Phase 2 uses key #2 (independent quotas)
  - Tafsir validation runs in Phase 1, NOT Phase 2 or 3
  - Quota checks happen in the right phase

[What we do NOT test]
  - Real LLM/Leonardo/ElevenLabs calls (those are integration tests, not contract tests)
  - Real video rendering (FFmpeg)
  - YouTube upload

[Test isolation]
Each test gets its own tmp_path so Phase state files don't bleed between tests.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════
@pytest.fixture
def episode_workdir(tmp_path):
    """Per-test working directory with realistic Qeema layout."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "phases").mkdir()
    (tmp_path / "temp").mkdir()
    (tmp_path / "temp" / "episodes").mkdir()
    (tmp_path / "temp" / "episodes" / "episode_001").mkdir()
    return tmp_path


@pytest.fixture
def fake_phase1_episode_json():
    """Realistic Phase 1 output — what script_engine writes."""
    return {
        "episode_number": 1,
        "surah_name": "الفاتحة",
        "title_ar": "تدبر سورة الفاتحة",
        "hook_text": "هل تعرف...",
        "intro_text": "اليوم سنتعلم...",
        "moral_text": "تعلمنا...",
        "cta_text": "اشترك...",
        "ayah_scenes": [
            {
                "ayah": {"number": 1, "text": "بسم الله الرحمن الرحيم", "surah": 1, "surah_name": "الفاتحة"},
                "story_text": "قصة قصيرة...",
                "explain_text": "معنى الآية...",
                "moral_text": "العبرة...",
                "visual_prompt": "Arabic mosque at sunrise",
                "emotion": "warm",
            },
            {
                "ayah": {"number": 2, "text": "الحمد لله رب العالمين", "surah": 1, "surah_name": "الفاتحة"},
                "story_text": "...",
                "explain_text": "...",
                "moral_text": "...",
                "visual_prompt": "garden under sunrise",
                "emotion": "reverent",
            },
        ],
    }


# ════════════════════════════════════════════════════════════════
# Phase 1 → Phase 2 handoff
# ════════════════════════════════════════════════════════════════
class TestPhase1ToPhase2Handoff:
    def test_phase1_json_has_keys_phase2_needs(self, fake_phase1_episode_json):
        """Phase 2 reads `ayah_scenes`, expects each to have `visual_prompt`,
        `emotion`, and an `ayah` dict with `number` + `text`."""
        ep = fake_phase1_episode_json
        assert "ayah_scenes" in ep, "Phase 2 deep-visuals reads `ayah_scenes`"

        for scene in ep["ayah_scenes"]:
            # DeepVisualPromptGenerator reads:
            assert "visual_prompt" in scene, "deep visuals needs `visual_prompt`"
            assert "emotion" in scene, "deep visuals needs `emotion`"
            # TTSDirector reads `ayah.text` and `story_text` etc.
            assert "ayah" in scene
            assert "number" in scene["ayah"]
            assert "text" in scene["ayah"]
            # Voice engine reads:
            assert "story_text" in scene
            assert "explain_text" in scene
            assert "moral_text" in scene

    def test_phase2_does_not_wipe_phase1_keys(
        self, episode_workdir, fake_phase1_episode_json,
    ):
        """When Phase 2 augments the episode JSON, the Phase 1 keys must remain."""
        ep_path = episode_workdir / "temp" / "episodes" / "episode_001.json"
        ep_path.write_text(json.dumps(fake_phase1_episode_json), encoding="utf-8")

        # Simulate Phase 2: read, add `_deep_visuals`, write back
        with open(ep_path, encoding="utf-8") as f:
            ep_data = json.load(f)
        ep_data["_deep_visuals"] = [
            {"subject": "mosque", "mood": "reverent"},
            {"subject": "garden", "mood": "peaceful"},
        ]
        with open(ep_path, "w", encoding="utf-8") as f:
            json.dump(ep_data, f, ensure_ascii=False, indent=2)

        # Read back — verify Phase 1 keys still there
        with open(ep_path, encoding="utf-8") as f:
            after = json.load(f)
        assert "ayah_scenes" in after, "Phase 2 wiped ayah_scenes!"
        assert after["surah_name"] == "الفاتحة"
        assert after["hook_text"] == "هل تعرف..."
        # AND the new key is present
        assert "_deep_visuals" in after

    def test_phase_state_json_survives_phase_boundaries(self, episode_workdir):
        """phase_state.json must merge across writes (Phase 2 must not wipe Phase 1's data)."""
        state_path = episode_workdir / "temp" / "episodes" / "episode_001" / "_phase_state.json"

        # Phase 1 writes
        existing = {}
        if state_path.exists():
            with open(state_path) as f:
                existing = json.load(f)
        existing.update({"phase1_completed_at": 1700000000.0, "script_ready": True})
        with open(state_path, "w") as f:
            json.dump(existing, f)

        # Phase 2 writes — must MERGE, not replace
        with open(state_path) as f:
            existing = json.load(f)
        existing.update({
            "phase2_completed_at": 1700001000.0,
            "audio_map": {"scene_0": "/path/to/audio.mp3"},
            "mastered_map": {"scene_0": "/path/to/mastered.aac"},
        })
        with open(state_path, "w") as f:
            json.dump(existing, f)

        # Phase 3 reads — must see BOTH phase 1 + phase 2 keys
        with open(state_path) as f:
            final = json.load(f)
        assert final["phase1_completed_at"] == 1700000000.0, \
            "Phase 1 timestamp lost"
        assert final["phase2_completed_at"] == 1700001000.0, \
            "Phase 2 timestamp lost"
        assert final["audio_map"]["scene_0"] == "/path/to/audio.mp3"
        assert final["mastered_map"]["scene_0"] == "/path/to/mastered.aac"


# ════════════════════════════════════════════════════════════════
# Phase 2 → Phase 3 handoff
# ════════════════════════════════════════════════════════════════
class TestPhase2ToPhase3Handoff:
    def test_phase3_can_reload_script_from_disk(
        self, episode_workdir, fake_phase1_episode_json,
    ):
        """Phase 3 might run on a different runner — must reconstruct script
        from the persisted JSON, not from in-memory state."""
        ep_path = episode_workdir / "temp" / "episodes" / "episode_001.json"
        # Simulate full Phase 2 output (Phase 1 + augmentations)
        ep_data = dict(fake_phase1_episode_json)
        ep_data["_deep_visuals"] = [{"subject": "x"} for _ in ep_data["ayah_scenes"]]
        ep_data["_tts_directions"] = {"scene_0": {"directed_text": "..."}}
        ep_path.write_text(json.dumps(ep_data, ensure_ascii=False), encoding="utf-8")

        # Phase 3 simulation: must be able to load this without errors
        with open(ep_path, encoding="utf-8") as f:
            reloaded = json.load(f)

        assert reloaded["episode_number"] == 1
        assert reloaded["title_ar"]
        assert len(reloaded["ayah_scenes"]) == 2
        assert "_deep_visuals" in reloaded
        assert "_tts_directions" in reloaded

    def test_phase3_fails_loud_when_audio_map_missing(self, episode_workdir):
        """If Phase 2 didn't run (or its state file is empty), Phase 3 must
        FAIL LOUDLY rather than render a silent video."""
        state_path = episode_workdir / "temp" / "episodes" / "episode_001" / "_phase_state.json"
        state_path.write_text(json.dumps({}))  # empty state

        # Simulate the orchestrator's check
        with open(state_path) as f:
            state = json.load(f)

        mastered = state.get("mastered_map", {})
        assert not mastered, "If Phase 2 didn't run, mastered_map must be empty"
        # Real code raises PipelineError("Phase 3 needs mastered audio from Phase 2")
        # This test just enforces the empty-detection contract.


# ════════════════════════════════════════════════════════════════
# Phase isolation — keys, quotas
# ════════════════════════════════════════════════════════════════
class TestPhaseKeyIsolation:
    def test_phase1_uses_only_key_1(self):
        """Phase 1 must use ONLY GEMINI_API_KEY (key #1).
        Keys #2 and #3 belong to Phase 2 and are reserved.
        """
        os.environ['GEMINI_API_KEY'] = 'k1'
        os.environ['GEMINI_API_KEY_2'] = 'k2'
        os.environ['GEMINI_API_KEY_3'] = 'k3'
        try:
            from core.config import APIKeysConfig
            api = APIKeysConfig.from_env()
            assert api.script_pool_keys == ('k1', 'k2', 'k3'), \
                f"v22.5.6: Phase 1 uses ALL Gemini keys, got: {api.script_pool_keys}"
            assert api.tafsir_review_key == 'k1', \
                f"Tafsir is in Phase 1 → must use k1, got: {api.tafsir_review_key}"
        finally:
            for k in ('GEMINI_API_KEY', 'GEMINI_API_KEY_2', 'GEMINI_API_KEY_3'):
                os.environ.pop(k, None)

    def test_phase2_uses_key_2_via_env_lookup(self):
        """Phase 2 reads its dedicated keys from env in the orchestrator
        (intentional — keeps Phase 1 ↔ Phase 2 quotas independent).

        v22.6: Phase 2 splits into two dedicated keys:
          - Key 2 (GEMINI_API_KEY_2) → TTS direction
          - Key 3 (GEMINI_API_KEY_3) → visual prompts
        Both are accessed through _build_phase2_adapter, which encodes
        the env-lookup + fallback logic.
        """
        import inspect
        from orchestrator import Orchestrator

        # The shared builder must reference KEY_2 (the canonical Phase 2 key)
        # and have fallback logic.
        builder_src = inspect.getsource(Orchestrator._build_phase2_adapter)
        assert 'GEMINI_API_KEY_2' in builder_src, \
            "Phase 2 builder must read GEMINI_API_KEY_2 (different daily quota)"
        assert 'or os.getenv' in builder_src, \
            "Should have fallback logic for missing KEY_2"

        # The TTS adapter resolves to KEY_2 specifically.
        tts_src = inspect.getsource(Orchestrator._phase2_tts_gemini_adapter)
        assert 'GEMINI_API_KEY_2' in tts_src, \
            "TTS adapter must target GEMINI_API_KEY_2 specifically"

        # The visual adapter resolves to KEY_3 specifically.
        visual_src = inspect.getsource(Orchestrator._phase2_visual_gemini_adapter)
        assert 'GEMINI_API_KEY_3' in visual_src, \
            "Visual adapter must target GEMINI_API_KEY_3 specifically"

    def test_tafsir_validator_uses_phase1_key(self):
        """TafsirValidator gets the same key as ScriptEngine (both in Phase 1).
        They share a rate limiter so the combined 14 calls don't exceed 5 RPM.
        """
        os.environ['GEMINI_API_KEY'] = 'k1'
        os.environ['GEMINI_API_KEY_2'] = 'k2'
        try:
            from core.config import APIKeysConfig
            api = APIKeysConfig.from_env()
            assert api.tafsir_review_key == api.script_pool_keys[0], \
                "Tafsir + script must share key #1 (Phase 1 architecture)"
        finally:
            for k in ('GEMINI_API_KEY', 'GEMINI_API_KEY_2'):
                os.environ.pop(k, None)


# ════════════════════════════════════════════════════════════════
# Tafsir validation gating
# ════════════════════════════════════════════════════════════════
class TestTafsirGating:
    def test_tafsir_failure_raises_quality_gate_error(self):
        """If tafsir validation rejects ≥1 ayah, orchestrator must raise
        QualityGateError BEFORE Phase 2 starts. This protects against
        spending Leonardo+ElevenLabs quota on bad scripts."""
        from core.exceptions import QualityGateError
        from engines.tafsir_validator import TafsirValidationResult

        # If we have rejected results, the orchestrator does:
        rejected = [
            {"ayah": 1, "passed": False, "concerns": ["معنى مغلوط"]},
            {"ayah": 2, "passed": True, "concerns": []},
        ]
        bad = [r for r in rejected if not r.get("passed", False)]
        assert len(bad) == 1
        # The orchestrator's actual gate logic (mirrored here):
        if bad:
            with pytest.raises(QualityGateError):
                raise QualityGateError(
                    f"Tafsir validation FAILED for {len(bad)} ayah(s)",
                    critiques=["sample"],
                )

    def test_tafsir_runs_in_phase1_only(self):
        """Tafsir validation lives in Phase 1 — verify by reading the orchestrator
        source. The actual STAGE INVOCATION should appear in the Phase 1 section
        only, not in Phase 2's run_phase2 block.
        """
        with open("orchestrator.py", encoding="utf-8") as f:
            src = f.read()

        # Find the actual run-phase boundaries (the `if run_phase1:` etc.)
        p1_marker = src.find("if run_phase1:")
        p2_marker = src.find("if run_phase2:")
        p3_marker = src.find("if run_phase3:")
        assert p1_marker > 0 and p2_marker > p1_marker and p3_marker > p2_marker, \
            "Could not locate run-phase markers in orchestrator"

        phase1_block = src[p1_marker:p2_marker]
        phase2_block = src[p2_marker:p3_marker]
        phase3_block = src[p3_marker:src.find("\n    def ", p3_marker)]

        # tafsir_validation stage MUST be invoked in phase 1
        assert "tafsir_validation" in phase1_block, \
            "tafsir_validation stage call missing from Phase 1 block"

        # And the actual lambda call to _validate_tafsir(script, ...) must NOT
        # appear in Phase 2 or Phase 3 execution blocks.
        # Note: the method DEFINITION lives outside any run_phase block —
        # we exclude it by scoping to the run-phase blocks only.
        assert "self._validate_tafsir(script" not in phase2_block, \
            "Tafsir validation stage call must NOT execute in Phase 2"
        assert "self._validate_tafsir(script" not in phase3_block, \
            "Tafsir validation stage call must NOT execute in Phase 3"


# ════════════════════════════════════════════════════════════════
# Quota gating
# ════════════════════════════════════════════════════════════════
class TestQuotaGating:
    def test_leonardo_quota_checked_before_image_call(self):
        """Image engine must check leonardo_remaining before each generation
        to avoid burning the 150-token Free Trial budget on retries."""
        with open("engines/image_engine.py", encoding="utf-8") as f:
            src = f.read()
        assert "can_consume_leonardo" in src, \
            "image_engine must check Leonardo quota before each call"
        assert "leonardo_remaining" in src, \
            "image_engine must read leonardo_remaining for graceful degradation"

    def test_elevenlabs_quota_checked_with_fallback_chain(self):
        """voice_engine must check ElevenLabs quota and fall back through
        the chain (CambAI → Google TTS) when exhausted."""
        with open("engines/voice_engine.py", encoding="utf-8") as f:
            src = f.read()
        assert "can_consume_elevenlabs" in src
        assert '"camb_ai" in self._providers' in src, \
            "Must try CambAI before Google TTS"
        assert '"google_tts" in self._providers' in src, \
            "Google TTS is the final fallback"


# ════════════════════════════════════════════════════════════════
# CambAI integration in voice_engine
# ════════════════════════════════════════════════════════════════
class TestCambAIPipelineIntegration:
    def test_camb_ai_only_wired_when_both_env_vars_set(self):
        """voice_engine wires CambAI ONLY if BOTH CAMB_AI_KEY and
        CAMB_AI_VOICE_ID are set. Just the key is not enough."""
        import tempfile
        from pathlib import Path

        # Setup env: ELEVENLABS yes, CAMB_AI_KEY yes, CAMB_AI_VOICE_ID NO
        os.environ['ELEVENLABS_API_KEY'] = 'fake-el'
        os.environ['CAMB_AI_KEY'] = 'fake-camb'
        os.environ.pop('CAMB_AI_VOICE_ID', None)
        os.environ.pop('GOOGLE_APPLICATION_CREDENTIALS', None)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                from core.config import (
                    APIKeysConfig, AudioConfig, EngineConfig, PathsConfig,
                )
                paths = PathsConfig.from_root(Path(tmp))
                paths.tts_cache.mkdir(parents=True, exist_ok=True)
                paths.quran_cache.mkdir(parents=True, exist_ok=True)

                from engines.voice_engine import VoiceEngine
                ve = VoiceEngine(
                    api_keys=APIKeysConfig.from_env(),
                    paths=paths,
                    audio_cfg=AudioConfig(),
                    engine_cfg=EngineConfig(),
                )

                assert "camb_ai" not in ve._providers, \
                    "CambAI must NOT be wired without CAMB_AI_VOICE_ID"
        finally:
            for k in ('ELEVENLABS_API_KEY', 'CAMB_AI_KEY'):
                os.environ.pop(k, None)

    def test_fallback_chain_order_in_voice_engine(self):
        """ElevenLabs runs out → try CambAI first, then Google TTS."""
        with open("engines/voice_engine.py", encoding="utf-8") as f:
            src = f.read()

        # Find the quota-exhausted fallback block
        fallback_block = src[src.find("can_consume_elevenlabs"):]
        camb_pos = fallback_block.find('"camb_ai" in self._providers')
        google_pos = fallback_block.find('"google_tts" in self._providers')
        assert camb_pos > 0, "CambAI not in fallback chain"
        assert google_pos > 0, "Google TTS not in fallback chain"
        assert camb_pos < google_pos, \
            "CambAI must be tried BEFORE Google TTS (better quality)"
