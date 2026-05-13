"""
assets_engines/gemini_client.py
====================================================================
Wrapper around the google-genai SDK with:
  - Multi-key rotation (3 free-tier keys → 60 calls/day)
  - Rate limiting (min 15s between calls per key = 4 RPM)
  - Structured output via response_json_schema
  - Retry on 503 / quota errors

This is the single Gemini interface; never call google-genai directly
elsewhere in the codebase.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, ValidationError

from core.config import get_api_keys, get_pipeline_config


log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Errors
# ════════════════════════════════════════════════════════════════════

class GeminiError(Exception):
    """Base error for Gemini operations."""

class GeminiQuotaExhausted(GeminiError):
    """All keys hit quota for the day."""

class GeminiSchemaError(GeminiError):
    """Output didn't match the expected schema."""


# ════════════════════════════════════════════════════════════════════
# Key pool with rate limiting
# ════════════════════════════════════════════════════════════════════

@dataclass
class _KeyState:
    label: str          # e.g. "gemini-1"
    api_key: str
    last_call_at: float = 0.0
    consecutive_failures: int = 0
    quota_exhausted: bool = False


class GeminiClient:
    """
    Multi-key Gemini client. Handles:
      - Key selection (least-recently-used among healthy keys)
      - Rate limiting (15s minimum between calls per key)
      - Structured output (via response_json_schema)
      - Retry on 503/429 with exponential backoff
      - Quota tracking (marks keys exhausted on 429)
    """

    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, model: Optional[str] = None) -> None:
        keys = get_api_keys()
        self.cfg = get_pipeline_config()
        self.model = model or self.DEFAULT_MODEL

        # Build the key pool
        self._keys: List[_KeyState] = []
        for i, k in enumerate(keys.gemini_keys_list(), start=1):
            self._keys.append(_KeyState(label=f"gemini-{i}", api_key=k))

        if not self._keys:
            raise GeminiError("No Gemini API keys configured")

        log.info("GeminiClient initialized with %d keys", len(self._keys))

        # Lazy-import the SDK so the rest of the codebase still
        # imports even if google-genai isn't installed yet
        from google import genai  # type: ignore
        self._genai = genai

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[BaseModel],
        temperature: float = 0.7,
        max_retries_per_key: int = 2,
    ) -> Tuple[BaseModel, str]:
        """
        Generate a structured response that validates against `response_schema`.

        Returns: (parsed_pydantic_model, key_label_used)
        Raises: GeminiQuotaExhausted | GeminiSchemaError | GeminiError
        """
        last_error: Optional[Exception] = None

        # Try each healthy key
        for key in self._iter_healthy_keys():
            for attempt in range(1, max_retries_per_key + 1):
                self._wait_for_rate_limit(key)
                try:
                    raw_text = self._call(
                        api_key=key.api_key,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        response_schema=response_schema,
                        temperature=temperature,
                    )
                    # Parse with Pydantic
                    parsed = response_schema.model_validate_json(raw_text)
                    key.consecutive_failures = 0
                    log.info(
                        "✓ Gemini call OK on %s (attempt %d)",
                        key.label, attempt,
                    )
                    return parsed, key.label

                except ValidationError as e:
                    last_error = GeminiSchemaError(
                        f"Schema validation failed: {e}"
                    )
                    log.warning(
                        "Schema validation failed on %s (attempt %d): %s",
                        key.label, attempt, str(e)[:200],
                    )
                    # Try once more with the same key (LLM might give better output)
                    continue

                except Exception as e:
                    last_error = e
                    msg = str(e).lower()
                    if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                        log.warning(
                            "Quota hit on %s; marking exhausted", key.label,
                        )
                        key.quota_exhausted = True
                        break  # try next key
                    if "503" in msg or "unavailable" in msg or "timeout" in msg:
                        sleep_sec = min(2 ** attempt * 5, 30)
                        log.warning(
                            "Transient error on %s (attempt %d): %s; "
                            "retrying in %ds",
                            key.label, attempt, str(e)[:100], sleep_sec,
                        )
                        time.sleep(sleep_sec)
                        continue
                    # Unknown error — try next key
                    log.warning(
                        "Unknown error on %s: %s", key.label, str(e)[:200],
                    )
                    key.consecutive_failures += 1
                    break

        # Exhausted all keys & retries
        healthy = [k for k in self._keys if not k.quota_exhausted]
        if not healthy:
            raise GeminiQuotaExhausted(
                f"All {len(self._keys)} keys exhausted. Last error: {last_error}"
            )
        raise GeminiError(
            f"All retries failed. Last error: {last_error}"
        )

    # ─────────────────────────────────────────────────────────────
    # Internal: single call
    # ─────────────────────────────────────────────────────────────

    def _call(
        self,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[BaseModel],
        temperature: float,
    ) -> str:
        """Single Gemini call. Returns raw JSON text."""
        client = self._genai.Client(api_key=api_key)

        # Get the JSON schema and pass it as response_json_schema
        schema = response_schema.model_json_schema()

        config: Dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_json_schema": schema,
            "temperature": temperature,
            "system_instruction": system_prompt,
        }

        response = client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=config,
        )

        text = (response.text or "").strip()
        if not text:
            raise GeminiError("Empty response from Gemini")
        return text

    # ─────────────────────────────────────────────────────────────
    # Internal: key selection + rate limit
    # ─────────────────────────────────────────────────────────────

    def _iter_healthy_keys(self):
        """Yield keys in LRU order, skipping quota-exhausted ones."""
        healthy = [k for k in self._keys if not k.quota_exhausted]
        healthy.sort(key=lambda k: k.last_call_at)  # LRU
        for k in healthy:
            yield k

    def _wait_for_rate_limit(self, key: _KeyState) -> None:
        """Sleep if needed to honor the per-key minimum interval."""
        now = time.time()
        elapsed = now - key.last_call_at
        min_interval = self.cfg.gemini_min_interval_sec
        if elapsed < min_interval:
            sleep_sec = min_interval - elapsed
            log.debug(
                "Rate limit on %s: sleeping %.1fs",
                key.label, sleep_sec,
            )
            time.sleep(sleep_sec)
        key.last_call_at = time.time()
