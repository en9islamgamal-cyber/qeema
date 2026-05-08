"""
infrastructure/llm_adapters.py — VALUE / QEEMA v22.5 — Gemini + Groq LLM adapters
====================================================================
Concrete LLM adapters that satisfy core.interfaces.LLMProvider.

[Providers]
- GeminiJsonAdapter  : Google Gemini (response_mime_type=json)
- GroqJsonAdapter    : Groq's OpenAI-compatible API (response_format=json_object)

[Error mapping]
Each adapter wraps its native SDK errors into our exception hierarchy:
- 429 / quota → RateLimitError
- network / 5xx → NetworkError
- 401 / 403  → AuthenticationError
- everything else → TransientError (so we retry/failover)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from core.exceptions import (
    AuthenticationError,
    NetworkError,
    RateLimitError,
    TransientError,
    ValidationError,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Robust JSON extraction
# ════════════════════════════════════════════════════════════════
_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)
_TRAILING_FENCE_RE = re.compile(r"\s*```")
_TRAILING_COMMA_OBJ = re.compile(r",\s*}")
_TRAILING_COMMA_ARR = re.compile(r",\s*]")


def extract_json_strict(text: str) -> Dict[str, Any]:
    """
    Extract a JSON object from messy LLM output.
    Handles: markdown fences, surrounding prose, trailing commas, and
    Arabic content edge cases (apostrophes inside strings, smart quotes).

    v22.5.5: Enhanced to handle Arabic content where Gemini sometimes
    embeds single quotes inside strings (e.g., 'رب العالمين') which can
    break naive JSON.loads. Uses progressive salvage: clean → smart-quote
    normalize → escape-fix → field-extraction.

    Raises ValidationError if no parseable JSON found.
    """
    if not text or not text.strip():
        raise ValidationError("Empty LLM response")

    cleaned = _FENCE_RE.sub("", text)
    cleaned = _TRAILING_FENCE_RE.sub("", cleaned).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValidationError(
            f"No JSON object found in LLM output: {text[:200]!r}"
        )

    json_str = cleaned[start : end + 1]

    # v22.5.5: Arabic-content normalization (smart quotes → ASCII)
    # Gemini sometimes mixes Arabic punctuation with JSON syntax
    json_str = (json_str
        .replace("\u201c", '"').replace("\u201d", '"')  # smart double
        .replace("\u2018", "'").replace("\u2019", "'")  # smart single
        .replace("\u00ab", '"').replace("\u00bb", '"')  # guillemets
    )

    json_str = _TRAILING_COMMA_OBJ.sub("}", json_str)
    json_str = _TRAILING_COMMA_ARR.sub("]", json_str)

    # Layer 1: clean parse
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        first_err = e

    # Layer 2: try escaping common offenders inside string values
    # (Gemini occasionally produces unescaped newlines inside Arabic strings)
    try:
        # Replace literal newlines within string values with \n
        # This is a best-effort heuristic for malformed responses
        salvage_attempt = re.sub(
            r'(?<="[^"]{0,1000})\n(?=[^"]{0,1000}")',
            '\\\\n',
            json_str,
            flags=re.DOTALL,
        )
        return json.loads(salvage_attempt)
    except (json.JSONDecodeError, re.error):
        pass

    # Layer 3: completely failed — raise with original error
    raise ValidationError(
        f"Invalid JSON: {first_err.msg} at pos {first_err.pos}",
        cause=first_err,
    ) from first_err


# ════════════════════════════════════════════════════════════════
# Error classification helpers
# ════════════════════════════════════════════════════════════════
_RATE_LIMIT_KEYWORDS = ("rate", "quota", "429", "resource_exhausted", "too many")
_NETWORK_KEYWORDS = (
    "connection", "timeout", "network", "503", "502", "504",
    "unavailable", "econn", "dns", "socket",
)
_AUTH_KEYWORDS = ("401", "403", "permission", "invalid_api_key", "unauthorized")


def _classify_error(exc: Exception, provider: str) -> Exception:
    """Map a raw provider exception to our hierarchy."""
    msg = str(exc).lower()
    if any(k in msg for k in _AUTH_KEYWORDS):
        return AuthenticationError(f"{provider} auth: {exc}", cause=exc)
    if any(k in msg for k in _RATE_LIMIT_KEYWORDS):
        return RateLimitError(f"{provider} rate limit: {exc}", cause=exc)
    if any(k in msg for k in _NETWORK_KEYWORDS):
        return NetworkError(f"{provider} network: {exc}", cause=exc)
    return TransientError(f"{provider} error: {exc}", cause=exc)


# ════════════════════════════════════════════════════════════════
# GeminiJsonAdapter
# ════════════════════════════════════════════════════════════════
class GeminiJsonAdapter:
    """Google Gemini with native JSON mode.

    [v22.5 — Per-key rate limiting]
    Every adapter created with the same api_key shares a sliding-window
    rate limiter (4 req/min, 60s window). This means ScriptEngine and the
    Phase 2 deep-visuals adapter on the same key can't combine to exceed
    Gemini's 5 RPM ceiling. The limiter `acquire()` is called before EVERY
    `generate_json()` call.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        instance_name: Optional[str] = None,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiJsonAdapter requires non-empty api_key")
        self.name: str = instance_name or f"gemini:{model}"
        self.model: str = model
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise RuntimeError("google-genai not installed") from e
        self._genai = genai
        self._client = genai.Client(api_key=api_key)

        # v22.5: per-key shared rate limiter (4 RPM sliding window)
        from core.gemini_rate_limiter import limiter_for_key
        self._rate_limiter = limiter_for_key(
            api_key, label_hint=instance_name or "gemini-adapter",
        )
        logger.info(f"✅ Gemini adapter ready: {self.name}")

    def generate_json(
        self,
        prompt: str,
        system_instruction: str = "",
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        try:
            from google.genai import types as gtypes  # type: ignore
        except ImportError as e:
            raise RuntimeError("google-genai types not available") from e

        # v22.5: respect 4 RPM per-key sliding window before EVERY call.
        # Blocks if we're at the cap.
        self._rate_limiter.acquire()

        try:
            cfg = gtypes.GenerateContentConfig(
                system_instruction=system_instruction or None,
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            )
            resp = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=cfg,
            )
            text = getattr(resp, "text", "") or ""
            return extract_json_strict(text)
        except (ValidationError, AuthenticationError):
            raise
        except Exception as e:
            raise _classify_error(e, self.name) from e


# ════════════════════════════════════════════════════════════════
# GroqJsonAdapter
# ════════════════════════════════════════════════════════════════
class GroqJsonAdapter:
    """Groq (free, fast Llama-3.3) with OpenAI-compatible JSON mode."""

    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        instance_name: Optional[str] = None,
    ) -> None:
        if not api_key:
            raise ValueError("GroqJsonAdapter requires non-empty api_key")
        if not api_key.startswith("gsk_"):
            logger.warning(
                "⚠️ Groq key doesn't start with 'gsk_'; verify it's correct"
            )
        self.name: str = instance_name or f"groq:{model}"
        self.model: str = model
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError("openai package not installed") from e
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)
        logger.info(f"✅ Groq adapter ready: {self.name}")

    def generate_json(
        self,
        prompt: str,
        system_instruction: str = "",
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
            return extract_json_strict(content)
        except (ValidationError, AuthenticationError):
            raise
        except Exception as e:
            raise _classify_error(e, self.name) from e
