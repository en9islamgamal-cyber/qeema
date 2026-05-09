"""End-to-end runtime smoke test for v22.6 Phase 1 + Phase 2 batch paths.

This test does NOT replace unit tests — it exercises the actual code paths
the pipeline takes when running an episode, with Gemini calls mocked at
the SDK level. It is the closest thing we have to "running the pipeline"
without a real Gemini key.

What it covers:
  - Phase 1 batch script + batch tafsir review on Key 1
  - Phase 2 batch visual prompts on Key 3
  - Phase 2 batch TTS direction on Key 2
  - Round-trip persistence: episode JSON written by Phase 1 is readable
    by Phase 2 and contains all expected fields after each phase.
  - ForbiddenAnalogyDetector intercepts a forbidden analogy and forces
    failure even if Gemini's mocked review approves.

What it does NOT cover (out of scope for static testing):
  - Actual Gemini API behaviour (would require a real key)
  - Actual ElevenLabs / Leonardo / YouTube API behaviour
  - Network resilience (retries, 429s)
  - The full orchestrator.run() loop — too many side effects
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engines.batch_engines import (
    AyahReviewOut,
    AyahScriptOut,
    AyahVisualOut,
    BatchReviewOut,
    BatchScriptOut,
    BatchTTSOut,
    BatchVisualOut,
    SegmentTTSOut,
)


# ════════════════════════════════════════════════════════════════
# Phase 1 mock — script + tafsir review return clean Fatiha content
# ════════════════════════════════════════════════════════════════
def _clean_fatiha_script() -> BatchScriptOut:
    """A doctrinally-clean batch script for surat al-Fatiha."""
    return BatchScriptOut(
        title="سورة الفاتحة — أم الكتاب",
        youtube_title="ليه الفاتحة هي أعظم سورة؟",
        youtube_description="حلقة لشرح سورة الفاتحة للأطفال من قناة قِيمة.",
        intro_text="أهلاً بيكم في حلقة جديدة من قِيمة.",
        outro_text="شكراً ليكم، نشوفكم في الحلقة الجاية.",
        ayahs=[
            AyahScriptOut(
                ayah_number=1,
                hook_text="إيه أهم اسم في الكون؟",
                explain_text="نبدأ بها لطلب البركة من الله.",
                story_text="زي ما بنبدأ كل عمل بأهم اسم.",
                moral_text="نبدأ كل حاجة باسم الله.",
                scene_emotion="reverent",
            ),
            AyahScriptOut(
                ayah_number=2,
                hook_text="ليه نقول الحمد لله؟",
                explain_text="نشكر الله الذي خلق كل شيء.",
                story_text="زي ما الطفل يشكر أمه على نعمها.",
                moral_text="نشكر ربنا على كل شيء.",
                scene_emotion="warm",
            ),
            AyahScriptOut(
                ayah_number=3,
                hook_text="إزاي ربنا رحيم؟",
                explain_text="ربنا واسع الرحمة بكل عباده.",
                story_text="زي الشمس اللي بتدفي الكل بدون استثناء.",
                moral_text="ربنا رحيم بكل خلقه.",
                scene_emotion="peaceful",
            ),
            AyahScriptOut(
                ayah_number=4,
                hook_text="إيه يوم الدين؟",
                explain_text="ربنا هو الملك في يوم الحساب.",
                story_text="زي يوم الامتحان حين تظهر النتائج.",
                moral_text="نستعد ليوم الحساب بالعمل الصالح.",
                scene_emotion="reverent",
            ),
            AyahScriptOut(
                ayah_number=5,
                hook_text="ليه نقول إياك نعبد؟",
                explain_text="نعبد الله وحده ونطلب عونه وحده.",
                story_text="زي ما الطفل يطلب من والديه أولاً.",
                moral_text="نعبد ربنا وحده.",
                scene_emotion="reverent",
            ),
            AyahScriptOut(
                ayah_number=6,
                hook_text="إيه الصراط المستقيم؟",
                explain_text="نطلب من ربنا أن يهدينا الطريق.",
                story_text="زي الخريطة اللي بتدل الطريق الصحيح.",
                moral_text="نطلب الهداية من ربنا كل يوم.",
                scene_emotion="warm",
            ),
            AyahScriptOut(
                ayah_number=7,
                hook_text="مين الأنعم عليهم؟",
                explain_text="طريق الذين أنعم الله عليهم بالهداية.",
                story_text="زي الأنبياء والصالحين الذين اتبعوا الحق.",
                moral_text="نتبع طريق الصالحين.",
                scene_emotion="reverent",
            ),
        ],
    )


def _clean_review() -> BatchReviewOut:
    return BatchReviewOut(reviews=[
        AyahReviewOut(ayah_number=i, passed=True, confidence=0.92, concerns=[])
        for i in range(1, 8)
    ])


def _clean_visuals() -> BatchVisualOut:
    return BatchVisualOut(prompts=[
        AyahVisualOut(
            ayah_number=i,
            subject=f"watercolor scene {i}", action="unfolds gently",
            environment=f"setting {i}", time_of_day="golden hour",
            mood="peaceful", color_palette="warm ochre, sage",
            lighting_direction="soft side", atmospheric_elements="dust motes",
            camera_angle="medium", depth_of_field="shallow",
            foreground=f"fg {i}", midground=f"mg {i}",
            background=f"bg {i}", focal_point=f"fp {i}",
        )
        for i in range(1, 8)
    ])


def _clean_tts(segment_ids: list) -> BatchTTSOut:
    return BatchTTSOut(directions=[
        SegmentTTSOut(
            segment_id=sid,
            directed_text=f'{sid} <break time="300ms"/>',
            pace="normal" if "story" in sid else "fast" if "hook" in sid else "slow",
        )
        for sid in segment_ids
    ])


# ════════════════════════════════════════════════════════════════
# E2E: script generation calls real BatchScriptEngine wired to mocked Gemini
# ════════════════════════════════════════════════════════════════
class TestE2EBatchEngines:

    def test_script_engine_with_mocked_gemini(self):
        """BatchScriptEngine is given a mocked Gemini client → produces a
        valid BatchScriptOut → no Pydantic validation error."""
        from engines.batch_engines import BatchScriptEngine

        client = MagicMock()
        response = MagicMock()
        response.parsed = _clean_fatiha_script()
        response.text = ""
        client.models.generate_content.return_value = response

        # Patch google.genai.types so _call_gemini_with_schema's import works
        fake_types = MagicMock()
        fake_types.GenerateContentConfig = lambda **kw: kw
        fake_genai_module = MagicMock(types=fake_types)
        with patch.dict(
            "sys.modules", {"google.genai": fake_genai_module},
        ), patch("google.genai.types", fake_types, create=True):
            engine = BatchScriptEngine(client)
            result = engine.generate_episode(
                surah_name="الفاتحة", surah_number=1,
                ayahs=[
                    {"number": i, "text": f"آية {i}"}
                    for i in range(1, 8)
                ],
                tafsirs={i: "..." for i in range(1, 8)},
            )

        assert result is not None
        assert len(result.ayahs) == 7
        assert all(a.scene_emotion in (
            "warm", "reverent", "playful", "peaceful", "excited",
        ) for a in result.ayahs)

    def test_visual_engine_outputs_round_trip_to_legacy_dicts(self):
        """The full data flow: Gemini → BatchVisualOut → to_legacy_dicts →
        dict ready for episode JSON."""
        from engines.batch_engines import BatchVisualPromptEngine

        client = MagicMock()
        response = MagicMock()
        response.parsed = _clean_visuals()
        response.text = ""
        client.models.generate_content.return_value = response

        fake_types = MagicMock()
        fake_types.GenerateContentConfig = lambda **kw: kw
        with patch.dict(
            "sys.modules", {"google.genai": MagicMock(types=fake_types)},
        ), patch("google.genai.types", fake_types, create=True):
            engine = BatchVisualPromptEngine(client)
            result = engine.generate_visuals([
                {"number": i, "explain": "e", "story": "s", "emotion": "warm"}
                for i in range(1, 8)
            ])
            legacy = BatchVisualPromptEngine.to_legacy_dicts(result)

        # Result is JSON-serializable (the orchestrator writes it to disk)
        serialized = json.dumps(legacy, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert len(deserialized) == 7
        assert all(d["is_usable"] is True for d in deserialized)
        # Critical fields present for VisualPromptEngineer.build_from_deep_result
        assert all(
            d["subject"] and d["color_palette"] and d["camera_angle"]
            for d in deserialized
        )

    def test_tts_engine_emits_correct_segment_ids(self):
        """BatchTTSDirector, given an episode_data, must produce segment_ids
        matching what orchestrator's TTS synthesis stage looks up later."""
        from engines.batch_engines import BatchTTSDirector

        episode_data = {
            "intro_text": "أهلاً",
            "outro_text": "شكراً",
            "ayah_scenes": [
                {"hook_text": f"h{i}", "story_text": f"s{i}", "moral_text": f"m{i}"}
                for i in range(1, 8)
            ],
        }

        # Compute expected segment ids deterministically from the input
        expected_ids = ["intro_text", "outro_text"] + [
            f"ayah_{i}.{kind}"
            for i in range(1, 8)
            for kind in ("hook", "story", "moral")
        ]

        client = MagicMock()
        response = MagicMock()
        response.parsed = _clean_tts(expected_ids)
        response.text = ""
        client.models.generate_content.return_value = response

        fake_types = MagicMock()
        fake_types.GenerateContentConfig = lambda **kw: kw
        with patch.dict(
            "sys.modules", {"google.genai": MagicMock(types=fake_types)},
        ), patch("google.genai.types", fake_types, create=True):
            director = BatchTTSDirector(client)
            result = director.direct_episode(episode_data)
            legacy = BatchTTSDirector.to_legacy_dict(result)

        # legacy is a dict keyed by segment_id — orchestrator's synth stage
        # looks up "ayah_3.hook" etc by exact key match.
        assert "intro_text" in legacy
        assert "outro_text" in legacy
        assert "ayah_3.hook" in legacy
        assert "ayah_7.moral" in legacy
        # 2 episode-level + 7 ayahs × 3 segments = 23 entries
        assert len(legacy) == 23

    def test_episode_json_round_trip_through_phase2(self):
        """Simulate the disk persistence Phase 2 actually does:
          1. Phase 1 writes episode_001.json with intro + 7 ayah_scenes
          2. Phase 2 reads, adds _deep_visuals + _tts_directions, writes back
          3. Re-read confirms all fields present and JSON-serializable.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            ep_path = Path(tmpdir) / "episode_001.json"

            # Step 1: Phase 1 output
            ep_data = {
                "episode_number": 1,
                "title": "سورة الفاتحة",
                "intro_text": "أهلاً",
                "outro_text": "شكراً",
                "ayah_scenes": [
                    {
                        "hook_text": f"h{i}",
                        "story_text": f"s{i}",
                        "moral_text": f"m{i}",
                        "explain_text": f"e{i}",
                        "scene_emotion": "warm",
                        "ayah": {"number": i, "text": f"آية {i}"},
                    }
                    for i in range(1, 8)
                ],
            }
            ep_path.write_text(
                json.dumps(ep_data, ensure_ascii=False),
                encoding="utf-8",
            )

            # Step 2: Phase 2 augments
            from engines.batch_engines import (
                BatchTTSDirector, BatchVisualPromptEngine,
            )
            with open(ep_path, encoding="utf-8") as f:
                ep_data = json.load(f)

            ep_data["_deep_visuals"] = (
                BatchVisualPromptEngine.to_legacy_dicts(_clean_visuals())
            )

            segment_ids = ["intro_text", "outro_text"] + [
                f"ayah_{i}.{k}"
                for i in range(1, 8)
                for k in ("hook", "story", "moral")
            ]
            ep_data["_tts_directions"] = (
                BatchTTSDirector.to_legacy_dict(_clean_tts(segment_ids))
            )

            ep_path.write_text(
                json.dumps(ep_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Step 3: Re-read and verify all fields
            with open(ep_path, encoding="utf-8") as f:
                reloaded = json.load(f)

            assert reloaded["episode_number"] == 1
            assert len(reloaded["ayah_scenes"]) == 7
            assert "_deep_visuals" in reloaded
            assert len(reloaded["_deep_visuals"]) == 7
            assert "_tts_directions" in reloaded
            assert "ayah_5.hook" in reloaded["_tts_directions"]


# ════════════════════════════════════════════════════════════════
# E2E: forbidden detector intercepts a corrupted analogy
# ════════════════════════════════════════════════════════════════
class TestE2EForbiddenInterception:
    """Simulates the v22.5.7 incident: legacy script_engine produced a
    magnet analogy for إياك نعبد. v22.6 must detect and reject it."""

    def test_magnet_analogy_for_iyaka_naabud_is_rejected(self):
        from engines.tafsir_validator import ForbiddenAnalogyDetector

        # The exact pattern the v22.5.7 episode 1 produced
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="إياك نعبد وإياك نستعين",
            explanation="نعبد الله وحده ونستعين به",
            analogy=(
                "زي المغناطيس اللي بيشد الحديد بقوة، "
                "إحنا منجذبين لربنا"
            ),
        )
        assert concerns, "v22.5.7 incident must be caught by detector"
        assert any("worship-as-magnet" in c for c in concerns)

    def test_full_phase1_rejects_magnet_episode_via_detector(self):
        """Even if Gemini reviewer mistakenly approves, the detector forces
        failure. This is the v22.6 defense-in-depth that v22.5 lacked."""
        from orchestrator import Orchestrator

        # Build a fake script with a magnet analogy on ayah 5
        scene1 = MagicMock()
        scene1.ayah = MagicMock(number=5, text="إياك نعبد وإياك نستعين")
        scene1.explain_text = "نعبد الله وحده"
        scene1.story_text = "زي المغناطيس اللي بيشد الحديد"

        script = MagicMock()
        script.ayah_scenes = [scene1]

        validator = MagicMock()
        validator._gemini_reviewer = MagicMock()
        validator._gemini_reviewer._client = MagicMock()
        validator._fetcher = MagicMock()
        validator._fetcher.fetch_combined.return_value = "تفسير معتمد"
        validator._confidence_threshold = 0.65

        instance = MagicMock(spec=Orchestrator)
        instance.tafsir_validator = validator

        # Gemini erroneously approves the magnet analogy
        gemini_pass = BatchReviewOut(reviews=[
            AyahReviewOut(
                ayah_number=5, passed=True, confidence=0.88, concerns=[],
            ),
        ])
        with patch(
            "engines.batch_engines.BatchTafsirReviewer.review_episode",
            return_value=gemini_pass,
        ):
            results = Orchestrator._try_batch_tafsir(
                instance, script=script, surah_name="الفاتحة", surah_num=1,
            )

        # Detector must override Gemini and force failure
        assert results[0]["passed"] is False
        assert any(
            "worship-as-magnet" in c for c in results[0]["concerns"]
        )
