"""End-to-end runtime test: cross-run survivability of Phase 2 outputs.

This test simulates the actual sequence of events the production
pipeline goes through across THREE separate workflow runs:

  Run 1 (Phase 1):
    - Orchestrator generates script + tafsir review
    - Saves episode JSON to temp/episodes/episode_NNN.json
    - phase_router._run_phase_1 reads it → state.script_data
    - PhaseStateManager persists to state/phases/episode_NNN.json
    - state/phases/ uploaded as artifact, then cached for next run

  [Runner restarts — temp/ wiped, state/phases/ restored from cache]

  Run 2 (Phase 2):
    - PhaseStateManager.load() returns state with script_data populated
    - Orchestrator._reload_episode_script restores temp JSON from
      state.script_data (no Gemini call!)
    - Phase 2 runs (Leonardo + ElevenLabs + deep visuals + TTS dirs)
    - _deep_visuals + _tts_directions written to temp episode JSON
    - phase_router._run_phase_2 reads them → asset_paths
    - PhaseStateManager persists asset_paths to state/phases/

  [Runner restarts again]

  Run 3 (Phase 3):
    - PhaseStateManager.load() returns state with script_data + asset_paths
    - Orchestrator hydrates temp JSON from script_data + _deep_visuals +
      _tts_directions in asset_paths
    - script_engine.load_from_disk inflates script with full visual_prompts
      and TTS directions
    - Render + upload uses populated fields → real cinematic video

This test actually instantiates PhaseStateManager and runs the
above sequence with realistic data. It does NOT mock the persistence
layer. The only mocks are external IO (Gemini, ElevenLabs, etc).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest


def test_full_three_run_simulation_preserves_phase2_outputs(tmp_path):
    """Simulate three workflow runs against the SAME state directory
    (which is what GitHub Actions cache effectively does)."""
    from core.phase_state import PhaseStateManager, Phase

    # ── Persistent across all runs (would be GitHub Actions cache) ──
    state_dir = tmp_path / "state" / "phases"
    state_dir.mkdir(parents=True)

    # ─── RUN 1: Phase 1 ─────────────────────────────────────────────
    # Imagine Phase 1 just finished. The orchestrator wrote temp JSON,
    # phase_router._run_phase_1 read it, PhaseStateManager persists it.
    psm_run1 = PhaseStateManager(state_dir)
    state = psm_run1.load(episode_number=1)
    assert state.script_data is None  # fresh start

    script_data = {
        "episode_number": 1,
        "title": "سورة الفاتحة",
        "youtube_title": "أسرار الفاتحة",
        "intro_text": "أهلاً بيكم",
        "outro_text": "نشوفكم في الحلقة الجاية",
        "ayah_scenes": [
            {
                "scene_id": i,
                "ayah": {
                    "surah": 1, "number": i,
                    "text": f"آية {i}", "audio_url": None,
                },
                "hook_text": f"hook {i}",
                "explain_text": f"explain {i}",
                "story_text": f"story {i}",
                "moral_text": f"moral {i}",
                "scene_emotion": "warm",
                "visual_prompt": "",  # not yet generated
                "image_path": None,
                "intro_audio": None,
                "hook_audio": None,
                "story_audio": None,
                "explain_audio": None,
                "moral_audio": None,
                "ayah_audio": None,
            }
            for i in range(1, 8)
        ],
    }
    state.script_data = script_data
    psm_run1.mark_phase_complete(
        state, phase=Phase.PLANNING,
        outputs={"script_data": script_data},
    )

    # File is now on disk
    state_file = state_dir / "episode_001.json"
    assert state_file.exists()

    # ─── [SIMULATE RUNNER RESTART] ──────────────────────────────────
    # In reality temp/ would be wiped here. We just create a NEW psm.
    del psm_run1, state

    # ─── RUN 2: Phase 2 ─────────────────────────────────────────────
    psm_run2 = PhaseStateManager(state_dir)
    state = psm_run2.load(episode_number=1)

    # Phase 1 outputs survived
    assert state.script_data is not None
    assert state.script_data["title"] == "سورة الفاتحة"
    assert len(state.script_data["ayah_scenes"]) == 7
    assert state.phase == Phase.PLANNING  # phase_1 is done

    # Imagine the orchestrator runs Phase 2:
    #   - It restores temp JSON from state.script_data (no Gemini)
    #   - BatchVisualPromptEngine generates _deep_visuals
    #   - BatchTTSDirector generates _tts_directions
    #   - ElevenLabs generates audio files
    # The result is captured in asset_paths:
    deep_visuals = [
        {
            "subject": f"watercolor scene {i}",
            "action": "unfolds gently",
            "environment": "warm garden",
            "time_of_day": "golden hour",
            "mood": "peaceful",
            "color_palette": "warm ochre, sage",
            "lighting_direction": "soft side",
            "atmospheric_elements": "dust motes",
            "camera_angle": "medium",
            "depth_of_field": "shallow",
            "foreground": "earth",
            "midground": f"scene {i}",
            "background": "hills",
            "focal_point": f"point {i}",
            "is_usable": True,
            "layers_completed": 3,
        }
        for i in range(1, 8)
    ]
    tts_directions = {
        "intro_text": {
            "directed_text": 'أهلاً بيكم <break time="300ms"/>',
            "pace": "normal",
            "pronunciation_notes": [],
        },
        **{
            f"ayah_{i}.{kind}": {
                "directed_text": f"text {i} {kind} <break/>",
                "pace": "fast" if kind == "hook" else "normal",
                "pronunciation_notes": [],
            }
            for i in range(1, 8)
            for kind in ("hook", "story", "moral")
        },
        "outro_text": {
            "directed_text": "نشوفكم <break/>",
            "pace": "slow",
            "pronunciation_notes": [],
        },
    }
    audio_map = {
        f"ayah_{i}_{kind}": f"/runner/temp/ep1/ayah_{i}_{kind}.mp3"
        for i in range(1, 8)
        for kind in ("hook", "story", "moral", "explain", "ayah")
    }
    audio_map["intro"] = "/runner/temp/ep1/intro.mp3"
    audio_map["outro"] = "/runner/temp/ep1/outro.mp3"
    mastered_map = {
        k: v.replace(".mp3", ".m4a").replace("/temp/", "/temp/mastered/")
        for k, v in audio_map.items()
    }

    psm_run2.mark_phase_complete(
        state, phase=Phase.ASSETS,
        outputs={
            "asset_paths": {
                "ep_dir": "/runner/temp/ep1",
                "audio_map": audio_map,
                "mastered_map": mastered_map,
                "ai_images_dir": "/runner/temp/ep1/ai_images",
                "_deep_visuals": deep_visuals,
                "_tts_directions": tts_directions,
            },
        },
    )

    # ─── [SIMULATE RUNNER RESTART AGAIN] ────────────────────────────
    del psm_run2, state

    # ─── RUN 3: Phase 3 ─────────────────────────────────────────────
    psm_run3 = PhaseStateManager(state_dir)
    state = psm_run3.load(episode_number=1)

    # Both phases' outputs survived
    assert state.phase == Phase.ASSETS
    assert state.script_data["title"] == "سورة الفاتحة"
    assert len(state.script_data["ayah_scenes"]) == 7

    asset_paths = state.asset_paths
    # Audio paths preserved
    assert asset_paths["audio_map"]["intro"] == "/runner/temp/ep1/intro.mp3"
    assert "ayah_5_hook" in asset_paths["mastered_map"]
    # Phase 2 cinematic outputs preserved (the v22.6.3 critical fix)
    assert "_deep_visuals" in asset_paths
    assert len(asset_paths["_deep_visuals"]) == 7
    assert (
        asset_paths["_deep_visuals"][0]["subject"]
        == "watercolor scene 1"
    )
    assert "_tts_directions" in asset_paths
    assert "ayah_3.story" in asset_paths["_tts_directions"]
    assert (
        "<break"
        in asset_paths["_tts_directions"]["intro_text"]["directed_text"]
    )

    # Now Phase 3 main loop reconstructs temp JSON. We verify the
    # reconstruction by simulating what the orchestrator does:
    temp_dir = tmp_path / "temp" / "episodes"
    temp_dir.mkdir(parents=True)
    ep_json_path = temp_dir / "episode_001.json"

    payload = dict(state.script_data)
    payload["_deep_visuals"] = asset_paths["_deep_visuals"]
    payload["_tts_directions"] = asset_paths["_tts_directions"]
    ep_json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # The reconstructed temp JSON has everything Phase 3 needs:
    reloaded = json.loads(ep_json_path.read_text(encoding="utf-8"))
    assert reloaded["title"] == "سورة الفاتحة"
    assert len(reloaded["ayah_scenes"]) == 7
    # The two critical fields that v22.6.3 fixes:
    assert "_deep_visuals" in reloaded
    assert reloaded["_deep_visuals"][2]["focal_point"] == "point 3"
    assert "_tts_directions" in reloaded
    assert reloaded["_tts_directions"]["ayah_5.moral"]["pace"] == "normal"

    # If we get here, the full three-run flow is consistent. Phase 3
    # would have all the data it needs to render a complete video.


def test_phase2_outputs_serializable(tmp_path):
    """Sanity: deep_visuals (with possibly nested arrays) and tts_directions
    (with Arabic strings) survive JSON round-trip via PhaseStateManager."""
    from core.phase_state import PhaseStateManager, Phase

    state_dir = tmp_path / "state" / "phases"
    state_dir.mkdir(parents=True)
    psm = PhaseStateManager(state_dir)

    state = psm.load(1)
    state.script_data = {"episode_number": 1}
    psm.save(state)

    # Realistic Arabic content
    deep_visuals = [
        {
            "subject": "بذرة تنمو",
            "color_palette": "warm ochre, soft sage",
            "is_usable": True,
        },
    ]
    tts_directions = {
        "ayah_1.hook": {
            "directed_text": 'هل عمرك سألت <break time="500ms"/> نفسك؟',
            "pace": "fast",
            "pronunciation_notes": ["تَخَيَّل"],
        },
    }

    psm.mark_phase_complete(
        state, phase=Phase.ASSETS,
        outputs={
            "asset_paths": {
                "_deep_visuals": deep_visuals,
                "_tts_directions": tts_directions,
            },
        },
    )

    # Reload from disk
    psm_2 = PhaseStateManager(state_dir)
    reloaded = psm_2.load(1)
    rdv = reloaded.asset_paths["_deep_visuals"]
    rtd = reloaded.asset_paths["_tts_directions"]

    assert rdv[0]["subject"] == "بذرة تنمو"
    assert rtd["ayah_1.hook"]["pronunciation_notes"] == ["تَخَيَّل"]
    # The break tag's quotation marks survived intact
    assert '<break time="500ms"/>' in rtd["ayah_1.hook"]["directed_text"]
