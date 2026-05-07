"""
tests/test_camb_ai_provider.py — VALUE / QEEMA v22.5

Verifies CambAIProvider behavior against mocked HTTP calls. We do NOT
make real network calls in tests — instead we patch requests.get/post
and inject the response sequence the CAMB API would normally send.

[Why these tests exist]
The CAMB.AI integration is the second-tier TTS fallback. If it has
broken retry logic, broken auth handling, hangs on a stuck task, or
incorrectly parses the audio response, the whole pipeline can stall
when ElevenLabs runs out of quota.

[Critical contract notes — verified against docs.camb.ai 2026-05-07]
- Submit returns {"task_id": "<string>"}
- Poll returns {"status": "...", "run_id": <int>}
- tts-result returns RAW AUDIO BYTES (audio/flac), NOT JSON
- Language IDs are integers; auto-discovered via /source-languages
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import (
    AudioGenerationError,
    AuthenticationError,
    NetworkError,
    RateLimitError,
    TransientError,
)
from core.interfaces import TTSProvider, TTSRequest
from infrastructure import tts_providers as tp_module
from infrastructure.tts_providers import CambAIProvider


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════
def _make_response(status_code: int, json_data=None, content: bytes = b"",
                   content_type: str = "application/json"):
    """Build a mock requests.Response."""
    r = MagicMock()
    r.status_code = status_code
    r.text = "" if status_code == 200 else f"HTTP {status_code} body"
    if json_data is not None:
        r.json.return_value = json_data
    r.content = content
    r.iter_content = lambda chunk_size=8192: [content] if content else []
    r.headers = {"Content-Type": content_type}
    return r


@pytest.fixture
def output_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "out.flac")


@pytest.fixture
def fake_audio_bytes():
    return b"fLaC" + b"\x00" * 1500


@pytest.fixture(autouse=True)
def reset_language_cache():
    CambAIProvider._LANGUAGE_CACHE.clear()
    yield
    CambAIProvider._LANGUAGE_CACHE.clear()


# ════════════════════════════════════════════════════════════════
# Construction
# ════════════════════════════════════════════════════════════════
class TestCambAIConstruction:
    def test_inherits_from_tts_provider(self):
        p = CambAIProvider(api_key="k", voice_id=1)
        assert isinstance(p, TTSProvider)
        assert p.name == "camb_ai"

    def test_voice_id_stored_as_str_and_int(self):
        p = CambAIProvider(api_key="k", voice_id=12345)
        assert p.voice_id == "12345"
        assert p._voice_id_int == 12345

    def test_empty_api_key_rejected(self):
        with pytest.raises(ValueError, match="non-empty api_key"):
            CambAIProvider(api_key="", voice_id=1)

    def test_zero_voice_id_rejected(self):
        with pytest.raises(ValueError, match="voice_id"):
            CambAIProvider(api_key="k", voice_id=0)

    def test_language_id_override_skips_discovery(self):
        p = CambAIProvider(api_key="k", voice_id=1, language_id_override=42)
        assert p._resolved_language_id == 42


# ════════════════════════════════════════════════════════════════
# Successful flow
# ════════════════════════════════════════════════════════════════
class TestCambAISynthesizeSuccess:
    def test_full_flow_with_explicit_language_id(
        self, output_path, fake_audio_bytes,
    ):
        submit_resp = _make_response(200, {"task_id": "task_xyz"})
        poll_resp = _make_response(200, {"status": "SUCCESS", "run_id": 999})
        download_resp = _make_response(
            200, content=fake_audio_bytes, content_type="audio/flac",
        )
        call_log = []

        def mock_get(url, **kwargs):
            call_log.append(("GET", url))
            if "source-languages" in url:
                raise AssertionError("Should NOT call /source-languages with override")
            if "tts-result" in url:
                return download_resp
            if "/tts/" in url:
                return poll_resp
            return _make_response(404)

        def mock_post(url, **kwargs):
            call_log.append(("POST", url))
            return submit_resp

        with patch.object(tp_module, "validate_audio_file", return_value=True), \
             patch.object(tp_module, "get_audio_duration", return_value=3.5), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.post", side_effect=mock_post):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=78,
            )
            result = prov.synthesize(TTSRequest(
                text="السلام عليكم", output_path=output_path,
            ))

        assert result.provider == "camb_ai"
        assert result.duration_sec == 3.5
        assert Path(output_path).exists()
        assert call_log[0] == ("POST", "https://client.camb.ai/apis/tts")
        assert "/tts/task_xyz" in call_log[1][1]
        assert "/tts-result/999" in call_log[2][1]
        assert len(call_log) == 3

    def test_run_id_as_integer_in_url(self, output_path, fake_audio_bytes):
        submit_resp = _make_response(200, {"task_id": "t"})
        poll_resp = _make_response(200, {"status": "SUCCESS", "run_id": 42})
        download_resp = _make_response(
            200, content=fake_audio_bytes, content_type="audio/flac",
        )
        result_url_called = []

        def mock_get(url, **kwargs):
            if "tts-result" in url:
                result_url_called.append(url)
                return download_resp
            if "/tts/" in url:
                return poll_resp
            return _make_response(404)

        with patch.object(tp_module, "validate_audio_file", return_value=True), \
             patch.object(tp_module, "get_audio_duration", return_value=2.0), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(api_key="fake", voice_id=1, language_id_override=1)
            prov.synthesize(TTSRequest(text="hi", output_path=output_path))

        assert len(result_url_called) == 1
        assert result_url_called[0].endswith("/tts-result/42")

    def test_payload_uses_correct_language_id(
        self, output_path, fake_audio_bytes,
    ):
        submit_resp = _make_response(200, {"task_id": "t"})
        poll_resp = _make_response(200, {"status": "SUCCESS", "run_id": 1})
        download_resp = _make_response(
            200, content=fake_audio_bytes, content_type="audio/flac",
        )
        captured_payload = {}

        def mock_post(url, json=None, **kwargs):
            captured_payload.update(json or {})
            return submit_resp

        def mock_get(url, **kwargs):
            if "tts-result" in url:
                return download_resp
            return poll_resp

        with patch.object(tp_module, "validate_audio_file", return_value=True), \
             patch.object(tp_module, "get_audio_duration", return_value=2.0), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.post", side_effect=mock_post):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=78,
            )
            prov.synthesize(TTSRequest(text="hi", output_path=output_path))

        assert captured_payload["voice_id"] == 12345
        assert captured_payload["language"] == 78
        assert "VALUE-QEEMA" in captured_payload["project_name"]
        assert 3 <= len(captured_payload["project_name"]) <= 255
        assert 3 <= len(captured_payload["project_description"]) <= 5000


# ════════════════════════════════════════════════════════════════
# Language auto-discovery
# ════════════════════════════════════════════════════════════════
class TestCambAILanguageDiscovery:
    def test_egyptian_arabic_preferred(self):
        lang_resp = _make_response(200, [
            {"id": 1, "language": "English"},
            {"id": 78, "language": "Modern Standard Arabic"},
            {"id": 91, "language": "Egyptian Arabic"},
            {"id": 92, "language": "Levantine Arabic"},
        ])
        with patch("requests.get", return_value=lang_resp):
            prov = CambAIProvider(api_key="fake", voice_id=1)
            assert prov._get_language_id() == 91

    def test_msa_preferred_when_no_egyptian(self):
        lang_resp = _make_response(200, [
            {"id": 1, "language": "English"},
            {"id": 78, "language": "Modern Standard Arabic"},
            {"id": 92, "language": "Levantine Arabic"},
        ])
        with patch("requests.get", return_value=lang_resp):
            prov = CambAIProvider(api_key="fake", voice_id=1)
            assert prov._get_language_id() == 78

    def test_any_arabic_when_no_msa_or_egyptian(self):
        lang_resp = _make_response(200, [
            {"id": 1, "language": "English"},
            {"id": 92, "language": "Levantine Arabic"},
        ])
        with patch("requests.get", return_value=lang_resp):
            prov = CambAIProvider(api_key="fake", voice_id=1)
            assert prov._get_language_id() == 92

    def test_no_arabic_raises_loud_error(self):
        lang_resp = _make_response(200, [
            {"id": 1, "language": "English"},
            {"id": 2, "language": "Spanish"},
        ])
        with patch("requests.get", return_value=lang_resp):
            prov = CambAIProvider(api_key="fake", voice_id=1)
            with pytest.raises(AudioGenerationError, match="no Arabic"):
                prov._get_language_id()

    def test_language_id_cached_per_process(self):
        lang_resp = _make_response(200, [
            {"id": 78, "language": "Modern Standard Arabic"},
        ])
        call_count = [0]
        def mock_get(url, **kwargs):
            call_count[0] += 1
            return lang_resp

        with patch("requests.get", side_effect=mock_get):
            prov1 = CambAIProvider(api_key="fake-key-A", voice_id=1)
            prov1._get_language_id()
            prov2 = CambAIProvider(api_key="fake-key-A", voice_id=2)
            prov2._get_language_id()
        assert call_count[0] == 1

    def test_language_endpoint_401_raises_auth_error(self):
        with patch("requests.get", return_value=_make_response(401)):
            prov = CambAIProvider(api_key="fake", voice_id=1)
            with pytest.raises(AuthenticationError):
                prov._get_language_id()

    def test_language_endpoint_500_raises_transient(self):
        with patch("requests.get", return_value=_make_response(500)):
            prov = CambAIProvider(api_key="fake", voice_id=1)
            with pytest.raises(TransientError):
                prov._get_language_id()


# ════════════════════════════════════════════════════════════════
# Error handling
# ════════════════════════════════════════════════════════════════
class TestCambAIErrorHandling:
    def test_auth_401_does_not_retry(self, output_path):
        post_count = [0]
        def mock_post(url, **kwargs):
            post_count[0] += 1
            return _make_response(401)

        with patch("requests.post", side_effect=mock_post):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=1,
            )
            with pytest.raises(AuthenticationError):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))
        assert post_count[0] == 1

    def test_422_validation_does_not_retry(self, output_path):
        post_count = [0]
        def mock_post(url, **kwargs):
            post_count[0] += 1
            r = _make_response(422)
            r.json.return_value = {"detail": "voice not available"}
            return r

        with patch("requests.post", side_effect=mock_post):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="validation"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))
        assert post_count[0] == 1

    def test_500_retries_up_to_3_times(self, output_path):
        post_count = [0]
        def mock_post(url, **kwargs):
            post_count[0] += 1
            return _make_response(500)

        with patch("requests.post", side_effect=mock_post), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=1,
            )
            with pytest.raises((TransientError, AudioGenerationError)):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))
        assert post_count[0] == 3

    def test_429_retries_then_fails(self, output_path):
        post_count = [0]
        def mock_post(url, **kwargs):
            post_count[0] += 1
            return _make_response(429)

        with patch("requests.post", side_effect=mock_post), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=1,
            )
            with pytest.raises((RateLimitError, AudioGenerationError)):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))
        assert post_count[0] == 3

    def test_invalid_json_response_raises(self, output_path):
        bad_resp = _make_response(200)
        bad_resp.json.side_effect = ValueError("not json")

        with patch("requests.post", return_value=bad_resp):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="invalid JSON"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))

    def test_response_missing_task_id_raises(self, output_path):
        bad_resp = _make_response(200, {})

        with patch("requests.post", return_value=bad_resp):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="missing task_id"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))


# ════════════════════════════════════════════════════════════════
# Polling
# ════════════════════════════════════════════════════════════════
class TestCambAIPolling:
    def test_pending_forever_times_out_bounded(self, output_path):
        submit_resp = _make_response(200, {"task_id": "t"})
        pending_resp = _make_response(200, {"status": "PENDING"})

        with patch("requests.get", return_value=pending_resp), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345,
                max_poll_attempts=3, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="did not complete"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))

    def test_failed_status_surfaces_error_reason(self, output_path):
        submit_resp = _make_response(200, {"task_id": "t"})
        failed_resp = _make_response(200, {
            "status": "FAILED",
            "error": "voice not available for language",
        })

        with patch("requests.get", return_value=failed_resp), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=12345, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError) as exc_info:
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))
            err = str(exc_info.value)
            assert "FAILED" in err or "voice not available" in err

    def test_run_id_string_coerced_to_int(self, output_path, fake_audio_bytes):
        submit_resp = _make_response(200, {"task_id": "t"})
        success_resp = _make_response(200, {"status": "SUCCESS", "run_id": "42"})
        download_resp = _make_response(
            200, content=fake_audio_bytes, content_type="audio/flac",
        )
        captured_urls = []
        def mock_get(url, **kwargs):
            captured_urls.append(url)
            if "tts-result" in url:
                return download_resp
            return success_resp

        with patch.object(tp_module, "validate_audio_file", return_value=True), \
             patch.object(tp_module, "get_audio_duration", return_value=2.0), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=1, language_id_override=1,
            )
            prov.synthesize(TTSRequest(text="hi", output_path=output_path))

        assert any(u.endswith("/tts-result/42") for u in captured_urls)

    def test_run_id_unparseable_raises(self, output_path):
        submit_resp = _make_response(200, {"task_id": "t"})
        weird_resp = _make_response(200, {"status": "SUCCESS", "run_id": [1, 2]})

        with patch("requests.get", return_value=weird_resp), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=1, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="not coercible"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))


# ════════════════════════════════════════════════════════════════
# Audio download (binary)
# ════════════════════════════════════════════════════════════════
class TestCambAIAudioDownload:
    def test_audio_returned_directly_as_bytes(
        self, output_path, fake_audio_bytes,
    ):
        submit_resp = _make_response(200, {"task_id": "t"})
        poll_resp = _make_response(200, {"status": "SUCCESS", "run_id": 1})
        download_resp = _make_response(
            200, content=fake_audio_bytes, content_type="audio/flac",
        )

        def mock_get(url, **kwargs):
            if "tts-result" in url:
                return download_resp
            return poll_resp

        with patch.object(tp_module, "validate_audio_file", return_value=True), \
             patch.object(tp_module, "get_audio_duration", return_value=2.0), \
             patch("requests.get", side_effect=mock_get), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=1, language_id_override=1,
            )
            prov.synthesize(TTSRequest(text="hi", output_path=output_path))

        saved = Path(output_path).read_bytes()
        assert saved == fake_audio_bytes

    def test_tiny_payload_rejected(self, output_path):
        submit_resp = _make_response(200, {"task_id": "t"})
        poll_resp = _make_response(200, {"status": "SUCCESS", "run_id": 1})
        tiny_resp = _make_response(
            200, content=b"\x00" * 100, content_type="audio/flac",
        )

        def mock_get(url, **kwargs):
            if "tts-result" in url:
                return tiny_resp
            return poll_resp

        with patch("requests.get", side_effect=mock_get), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=1, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="100 bytes"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))

    def test_json_response_when_audio_expected_raises(self, output_path):
        submit_resp = _make_response(200, {"task_id": "t"})
        poll_resp = _make_response(200, {"status": "SUCCESS", "run_id": 1})
        json_resp = _make_response(
            200, json_data={"error": "internal"},
            content=b'{"error":"internal"}',
            content_type="application/json",
        )

        def mock_get(url, **kwargs):
            if "tts-result" in url:
                return json_resp
            return poll_resp

        with patch("requests.get", side_effect=mock_get), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=1, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="JSON when audio"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))

    def test_404_run_id_not_found(self, output_path):
        submit_resp = _make_response(200, {"task_id": "t"})
        poll_resp = _make_response(200, {"status": "SUCCESS", "run_id": 999})
        not_found_resp = _make_response(404)

        def mock_get(url, **kwargs):
            if "tts-result" in url:
                return not_found_resp
            return poll_resp

        with patch("requests.get", side_effect=mock_get), \
             patch("requests.post", return_value=submit_resp), \
             patch("time.sleep"):
            prov = CambAIProvider(
                api_key="fake", voice_id=1, language_id_override=1,
            )
            with pytest.raises(AudioGenerationError, match="run_id 999 not found"):
                prov.synthesize(TTSRequest(text="hi", output_path=output_path))


# ════════════════════════════════════════════════════════════════
# Health check
# ════════════════════════════════════════════════════════════════
class TestCambAIHealthCheck:
    def test_health_check_calls_list_voices(self):
        captured = []
        def mock_get(url, **kwargs):
            captured.append(url)
            return _make_response(200, {"voices": []})

        with patch("requests.get", side_effect=mock_get):
            prov = CambAIProvider(api_key="fake", voice_id=12345)
            assert prov.health_check() is True
        assert len(captured) == 1
        assert "list-voices" in captured[0]

    def test_health_check_401_returns_false(self):
        with patch("requests.get", return_value=_make_response(401)):
            prov = CambAIProvider(api_key="fake", voice_id=12345)
            assert prov.health_check() is False

    def test_health_check_network_error_returns_false(self):
        import requests as req
        with patch("requests.get", side_effect=req.ConnectionError("no net")):
            prov = CambAIProvider(api_key="fake", voice_id=12345)
            assert prov.health_check() is False
