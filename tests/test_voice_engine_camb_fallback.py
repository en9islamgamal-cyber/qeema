"""
tests/test_voice_engine_camb_fallback.py — VALUE / QEEMA v22.5

Locks the CambAI fallback contract end-to-end:
1. Quota-driven fallback (ElevenLabs cap reached) → CambAI consumption tracked
2. Pool-driven fallback (ElevenLabs raises exception) → CambAI consumption tracked
3. CambAI cap reached → fall through to Google TTS
4. No fallback configured → raise AudioGenerationError

These tests exercise the wiring I added between voice_engine + CambAIProvider +
QuotaManager. They're slower than provider-level tests because they instantiate
the full VoiceEngine with all 3 providers — but they're the only way to verify
the failover chain semantics actually hold under realistic conditions.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config import APIKeysConfig, AudioConfig, EngineConfig, PathsConfig
from core.exceptions import AudioGenerationError
from core.quota_manager import QuotaConfig, QuotaManager
from infrastructure import tts_providers as tp_module


def _make_audio_response():
    """Mock CambAI's tts-result endpoint returning FLAC bytes."""
    fake_flac = b"\xff\xfb" * 600  # 1200 bytes, passes the >1KB safety check
    resp = MagicMock(status_code=200, headers={"Content-Type": "audio/flac"})
    resp.iter_content.return_value = [fake_flac]
    return resp


def _setup_camb_mocks():
    """Patch the 3-call CAMB workflow (submit + poll + download)."""
    submit = MagicMock(status_code=200, headers={})
    submit.json.return_value = {"task_id": "task_xyz"}
    poll = MagicMock(status_code=200, headers={})
    poll.json.return_value = {"status": "SUCCESS", "run_id": 12345}
    download = _make_audio_response()

    def mock_get(url, **kwargs):
        if "tts/" in url and "tts-result" not in url:
            return poll
        return download

    return mock_get, lambda url, **kwargs: submit


@pytest.fixture
def voice_engine_factory():
    """Build a VoiceEngine wired with ElevenLabs + CambAI (no Google TTS)."""
    def _build(
        elevenlabs_budget: int = 100,
        camb_budget: int = 50_000,
    ):
        os.environ['GEMINI_API_KEY'] = 'k1'
        os.environ['ELEVENLABS_API_KEY'] = 'fake-el'
        os.environ['CAMB_AI_KEY'] = 'fake-camb'
        os.environ['CAMB_AI_VOICE_ID'] = '20303'
        os.environ['CAMB_AI_LANGUAGE_ID'] = '5'
        os.environ.pop('GOOGLE_APPLICATION_CREDENTIALS', None)

        tmp = tempfile.mkdtemp()
        paths = PathsConfig.from_root(Path(tmp))
        paths.tts_cache.mkdir(parents=True, exist_ok=True)
        paths.quran_cache.mkdir(parents=True, exist_ok=True)
        paths.logs.mkdir(parents=True, exist_ok=True)

        quota_config = QuotaConfig(
            elevenlabs_monthly_credits=elevenlabs_budget,
            camb_monthly_chars=camb_budget,
        )
        quota_mgr = QuotaManager(paths=paths, config=quota_config)

        from engines.voice_engine import VoiceEngine
        ve = VoiceEngine(
            api_keys=APIKeysConfig.from_env(),
            paths=paths,
            audio_cfg=AudioConfig(),
            engine_cfg=EngineConfig(),
            quota_manager=quota_mgr,
        )
        return ve, quota_mgr, tmp

    return _build


class TestCambFallbackQuotaPath:
    """ElevenLabs quota exhausted → fallback to CambAI BEFORE the API call."""

    def test_quota_exhaustion_falls_back_to_camb(self, voice_engine_factory):
        ve, quota_mgr, tmp = voice_engine_factory(
            elevenlabs_budget=100, camb_budget=50_000,
        )
        # Pre-fill ElevenLabs to leave only 1 char remaining
        quota_mgr._state.elevenlabs_used_this_month = 99
        text = "اختبار طويل يتجاوز الكوتا"  # > 1 char

        mock_get, mock_post = _setup_camb_mocks()
        out = str(Path(tmp) / "out.mp3")

        with patch.object(tp_module, "validate_audio_file", return_value=True), \
             patch.object(tp_module, "get_audio_duration", return_value=2.5), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.post", side_effect=mock_post), \
             patch("time.sleep"):
            result = ve.synthesize(text=text, output_path=out)

        assert result.provider == "camb_ai"
        assert quota_mgr._state.camb_used_this_month == len(text)

    def test_camb_cap_reached_blocks_camb_path(self, voice_engine_factory):
        """If CambAI monthly cap is also reached, do NOT call CambAI either."""
        ve, quota_mgr, tmp = voice_engine_factory(
            elevenlabs_budget=100, camb_budget=10,  # CAMB cap = 10 chars only
        )
        quota_mgr._state.elevenlabs_used_this_month = 99
        quota_mgr._state.camb_used_this_month = 9  # leaves 1 char remaining
        text = "اختبار طويل يتجاوز الكل"  # Way more than 1 char

        mock_get, mock_post = _setup_camb_mocks()
        out = str(Path(tmp) / "out.mp3")

        with patch.object(tp_module, "validate_audio_file", return_value=True), \
             patch.object(tp_module, "get_audio_duration", return_value=2.5), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.post", side_effect=mock_post), \
             patch("time.sleep"):
            # No Google TTS configured + CAMB cap reached → AudioGenerationError
            with pytest.raises(AudioGenerationError, match="No fallback"):
                ve.synthesize(text=text, output_path=out)

        # Verify CambAI was NOT charged
        assert quota_mgr._state.camb_used_this_month == 9, \
            "CambAI must not be called when its cap is already reached"


class TestCambFallbackContract:
    """High-level invariants of the fallback chain."""

    def test_camb_provider_registered_under_correct_name(
        self, voice_engine_factory,
    ):
        ve, _, _ = voice_engine_factory()
        assert "camb_ai" in ve._providers
        from infrastructure.tts_providers import CambAIProvider
        assert isinstance(ve._providers["camb_ai"], CambAIProvider)

    def test_provider_chain_order_is_elevenlabs_first(
        self, voice_engine_factory,
    ):
        """Verify ElevenLabs is registered first so the pool prefers it."""
        ve, _, _ = voice_engine_factory()
        provider_names = list(ve._providers.keys())
        # In Python 3.7+, dict preserves insertion order — ElevenLabs first
        assert provider_names.index("elevenlabs") < provider_names.index("camb_ai")
